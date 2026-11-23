"""On-demand, read-only health checks for local measurement rows."""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from modules.local_shadow_replay import ENV_UNIT, enabled_for_settings

_NUMBERS = re.compile(r"[+-]?\d+(?:[.,]\d+)*")
_TOKEN_BUDGET = re.compile(
    r"(?:local_too_slow: )?the time budget allows only ([0-9]{1,9}) output tokens\b"
)
_SETUP_REASONS = (
    "the merged commit is not available in the local repository",
    "the merged diff changes no non-test Python code",
)
_OUTCOMES = {
    "local_too_slow", "no_tests", "no_units", "error", "timeout",
    "invalid", "interrupted", "switch_failed", "lease_busy",
}
_LABELS = {
    "local_replays": "local replays",
    "local_replay_units": "local hunk replays",
    "planner_shadow": "planner shadow",
    "local_model_bakeoffs": "local model bake-offs",
}


def check_pipelines(
    db: sqlite3.Connection, now: datetime, n: int = 5, max_age_hours: float = 24,
) -> dict[str, dict[str, Any]]:
    """Report terminal failure streaks and check freshness independently."""
    if n < 1 or max_age_hours <= 0:
        raise ValueError("n and max_age_hours must be positive")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)

    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def setting(key: str) -> str | None:
        if "service_settings" in tables:
            row = db.execute("SELECT value FROM service_settings WHERE key=?", (key,)).fetchone()
            if row and row[0] is not None:
                return str(row[0]).strip()
        return None

    planner_time = "NULL"
    planner_join = ""
    if "planner_shadow" in tables:
        columns = {row[1] for row in db.execute("PRAGMA table_info(planner_shadow)")}
        if "created_at" in columns:
            planner_time = "p.created_at"
        if "nexus_initiative_log" in tables:
            # Legacy shadow rows share the initiative tick ID.
            planner_join = "LEFT JOIN nexus_initiative_log AS t ON t.tick_id = p.tick_id"
            planner_time = f"COALESCE({planner_time}, t.created_at)"

    replay_enabled = enabled_for_settings(setting)
    replay_unit = os.environ.get(ENV_UNIT, "whole").strip().lower()
    unit_columns = {row[1] for row in db.execute("PRAGMA table_info(local_replay_units)")}
    if "status" in unit_columns:
        unit_error = "error" if "error" in unit_columns else "NULL"
        unit_time = "COALESCE(finished_at, created_at)" if "finished_at" in unit_columns else "created_at"
        hunk_query = (
            f"SELECT id, status, {unit_error}, {unit_time} AS measured_at "
            "FROM local_replay_units WHERE status != 'running'"
        )
    else:
        hunk_query = "SELECT id, 'scored', NULL, created_at AS measured_at FROM local_replay_units"

    # Run completions and unit completions have independent streaks. A done
    # run can contain failed units, so it must not reset their streak.
    hunk_run_query = (
        "SELECT id, status, error, COALESCE(finished_at, created_at) AS measured_at "
        "FROM local_replay_unit_runs WHERE status IN ('done', 'no_units', 'error')"
    )
    paused = setting("nexus.paused") == "1"
    pipelines = (
        ("local_replays", replay_enabled, replay_unit == "whole", "scored",
         "SELECT id, status, error, COALESCE(finished_at, created_at) AS measured_at "
         "FROM local_replays WHERE status != 'pending'"),
        ("local_replay_units", replay_enabled, replay_unit == "hunk", "scored", hunk_query),
        ("planner_shadow", not paused and os.environ.get("NEXUS_PLANNER_SHADOW", "1").strip() != "0",
         True, "valid",
         "SELECT p.id, CASE p.local_valid WHEN 1 THEN 'valid' ELSE 'invalid' END, "
         f"p.error, {planner_time} AS measured_at FROM planner_shadow AS p {planner_join}"),
        # Bake-offs are run manually and have no expected cadence.
        ("local_model_bakeoffs", True, False, "completed",
         "SELECT id, status, error, created_at AS measured_at FROM local_model_bakeoffs WHERE status != 'not_run'"),
    )
    results = {}
    for name, enabled, scheduled, success, query in pipelines:
        # Older and experiment-only stores may not support this pipeline.
        sources = [(name, success, query)] if name in tables else []
        if name == "local_replay_units" and "local_replay_unit_runs" in tables:
            sources.append(("local_replay_unit_runs", "done", hunk_run_query))
        enabled = enabled and bool(sources)
        result = {"status": "ok", "enabled": enabled, "latest_at": None}
        results[name] = result
        if not enabled:
            continue
        observations = []
        for table, success, query in sources:
            with closing(db.execute(
                f"SELECT * FROM ({query}) "
                "WHERE measured_at IS NULL OR julianday(measured_at) <= julianday(?) "
                "ORDER BY julianday(measured_at) DESC, id DESC",
                (now.isoformat(),),
            )) as rows:
                observations.append(_check_rows(rows, success, n, table))
        latest = max((timestamp for timestamp, _ in observations), key=_timestamp)
        result["latest_at"] = latest
        streaks = [streak for _, streak in observations if streak]
        if streaks:
            result.update(max(streaks, key=lambda streak: _timestamp(streak["stuck_at"])))
            result["status"] = "stuck"
        if scheduled and _timestamp(latest) < cutoff:
            result.update(status="stale", max_age_hours=max_age_hours)
    return results


def _diagnostic(outcome: str, error: str) -> str:
    """Render only fixed diagnostic text and bounded numeric measurements."""
    if outcome == "local_too_slow" and (match := _TOKEN_BUDGET.match(error)):
        return f"the time budget allows only {int(match[1])} output tokens"
    if error in _SETUP_REASONS:
        return error
    return ""


# #772: the bounded rate-age / re-probe suffix of a local_too_slow refusal. It
# varies between rows of the same stuck streak, so it is not compared.
_REPROBE_SUFFIX = re.compile(r"\s*\[rates: [^\]]*\]\s*$")


def _normalize(reason: str) -> str:
    reason = _REPROBE_SUFFIX.sub("", reason)
    return " ".join(_NUMBERS.sub("", reason).casefold().split())


def _check_rows(
    rows: sqlite3.Cursor, success: str, n: int, table: str,
) -> tuple[str | None, dict[str, Any]]:
    first = rows.fetchone()
    if first is None:
        return None, {}
    row_id, outcome, error, timestamp = first
    if outcome == success:
        return timestamp, {}
    # Compare stored text locally. Only the controlled diagnostic leaves this module.
    normalized = _normalize(error or "")
    ids = [row_id]
    for row_id, next_outcome, next_error, _ in rows:
        if next_outcome != outcome or _normalize(next_error or "") != normalized:
            break
        ids.append(row_id)
    if len(ids) < n:
        return timestamp, {}
    reason = _diagnostic(outcome, error or "")
    return timestamp, {
        "outcome": outcome if outcome in _OUTCOMES else "unknown_failure",
        "reason": reason,
        "normalized_reason": _normalize(reason),
        "count": len(ids),
        "row_ids": ids,
        "row_table": table,
        "stuck_at": timestamp,
    }


def _timestamp(value: str | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    measured = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return measured.replace(tzinfo=measured.tzinfo or timezone.utc)


def read_health(db_path: str | Path, now: datetime) -> dict[str, dict[str, Any]]:
    """Inspect an existing store without creating or migrating it."""
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.execute("BEGIN")
            return check_pipelines(db, now)
    except (OSError, sqlite3.Error):
        return {}


def health_summary(name: str, result: dict[str, Any]) -> str:
    label = _LABELS[name]
    streak = ""
    if "count" in result:
        streak = f"{result['count']}x {result['outcome']}"
        if result["reason"]:
            streak += f": {result['reason']}"
    if result["status"] == "stale":
        summary = f"{label}: no measurement in the last {result['max_age_hours']:g} hours"
        return f"{summary} (last streak: {streak})" if streak else summary
    if streak:
        return f"{label}: {streak}"
    return f"{label}: {'ok' if result['enabled'] else 'disabled'}"
