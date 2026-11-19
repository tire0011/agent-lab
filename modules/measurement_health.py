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
from modules.redaction import sanitized_reason

_NUMBERS = re.compile(r"[+-]?\d+(?:[.,]\d+)*")
_LABELS = {
    "local_replays": "local replays",
    "local_replay_units": "local hunk replays",
    "planner_shadow": "planner shadow",
    "local_model_bakeoffs": "local model bake-offs",
}


def check_pipelines(
    db: sqlite3.Connection, now: datetime, n: int = 5, max_age_hours: float = 24,
) -> dict[str, dict[str, Any]]:
    """Report the latest consecutive failure streak, or freshness if none qualifies."""
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
        hunk_query = (
            "SELECT id, status, error, COALESCE(finished_at, created_at) AS measured_at "
            "FROM local_replay_units WHERE status != 'running'"
        )
    else:
        hunk_query = "SELECT id, 'scored', NULL, created_at AS measured_at FROM local_replay_units"

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
         "SELECT id, status, error, created_at AS measured_at FROM local_model_bakeoffs"),
    )
    results = {}
    for name, enabled, scheduled, success, query in pipelines:
        # Older and experiment-only stores may not support this pipeline.
        enabled = enabled and name in tables
        result = {"status": "ok", "enabled": enabled, "latest_at": None}
        results[name] = result
        if not enabled:
            continue
        rows = db.execute(
            f"SELECT * FROM ({query}) "
            "WHERE measured_at IS NULL OR julianday(measured_at) <= julianday(?) "
            "ORDER BY julianday(measured_at) DESC, id DESC",
            (now.isoformat(),),
        )
        with closing(rows):
            _check_rows(rows, result, success, cutoff, n, scheduled)
    return results


def _reason_text(error: str | None) -> str:
    return " ".join((sanitized_reason(error) or "").split())


def _normalize(reason: str) -> str:
    return " ".join(_NUMBERS.sub("", reason).casefold().split())


def _check_rows(
    rows: sqlite3.Cursor, result: dict[str, Any], success: str,
    cutoff: datetime, n: int, scheduled: bool,
) -> None:
    first = rows.fetchone()
    if first is not None:
        row_id, outcome, error, timestamp = first
        result["latest_at"] = timestamp
        if outcome != success:
            reason = _reason_text(error)
            normalized = _normalize(reason)
            ids = [row_id]
            for row_id, next_outcome, next_error, _ in rows:
                if next_outcome != outcome:
                    break
                if _normalize(_reason_text(next_error)) != normalized:
                    break
                ids.append(row_id)
            if len(ids) >= n:
                result.update(status="stuck", outcome=outcome, reason=reason,
                              normalized_reason=normalized, count=len(ids), row_ids=ids)
                return
    timestamp = result["latest_at"]
    if scheduled and (timestamp is None or _older_than(timestamp, cutoff)):
        result["status"] = "stale"


def _older_than(timestamp: str, cutoff: datetime) -> bool:
    measured = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return measured.replace(tzinfo=measured.tzinfo or timezone.utc) < cutoff


def read_health(db_path: str | Path, now: datetime) -> dict[str, dict[str, Any]]:
    """Inspect an existing store without creating or migrating it."""
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            return check_pipelines(db, now)
    except (OSError, sqlite3.Error):
        return {}


def health_summary(name: str, result: dict[str, Any]) -> str:
    label = _LABELS[name]
    if result["status"] == "stuck":
        summary = f"{label}: {result['count']}x {result['outcome']}"
        reason = result["reason"].removeprefix(f"{result['outcome']}: ").strip()
        return f"{summary}: {reason}" if reason else summary
    if result["status"] == "stale":
        return f"{label}: no measurement in the last 24 hours"
    return f"{label}: {'ok' if result['enabled'] else 'disabled'}"
