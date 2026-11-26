"""Read finished experiments from the run store for the tick replay (#755).

The only I/O of the replay: one read-only SQLite query. :mod:`modules.tick_replay`
itself stays pure.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from modules import tick_replay

DEFAULT_DAYS = 30
# Rows this far before ``since`` still form the history: a weekly-limit reset
# or a 6 h loop-guard pause that began before the window still counts.
HISTORY_LOOKBACK = timedelta(days=7)

_MERGED = ", ".join(f"'{s}'" for s in tick_replay.MERGED_ADOPTION_STATUSES)


def parse_since(value: Any, *, now: datetime | None = None) -> datetime:
    """``since`` as UTC; a date means its midnight, empty means 30 days ago."""
    if value is None or str(value).strip() == "":
        return (now or datetime.now(timezone.utc)) - timedelta(days=DEFAULT_DAYS)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"since must be an ISO date or time, got {text!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _connect(db: Any) -> tuple[sqlite3.Connection, bool]:
    if isinstance(db, sqlite3.Connection):
        return db, False
    path = Path(str(db)).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True), True


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def load_rows(db: Any, since: datetime, until: datetime | None = None) -> list[dict[str, Any]]:
    """Finished experiments that ended after ``since - HISTORY_LOOKBACK``.

    The issue comes from the GitHub link tables, else from a ``github_issue``
    source id; ``merged`` from an adopted/succeeded adoption.
    """
    conn, owned = _connect(db)
    try:
        conn.row_factory = sqlite3.Row
        experiments = _columns(conn, "experiments")
        if not experiments:
            return []
        links = [t for t in ("github_experiment_links", "github_work_links") if _columns(conn, t)]
        issue_parts = [
            f"(SELECT l.issue_number FROM {t} l WHERE l.experiment_id = e.id LIMIT 1)" for t in links
        ]
        issue_parts.append(
            "CASE WHEN e.source_type IN ('github_issue', 'issue') THEN e.source_id END"
            if {"source_type", "source_id"} <= experiments else "NULL"
        )
        issue_parts.append("NULL")  # COALESCE needs two arguments
        merged = (
            f"EXISTS (SELECT 1 FROM experiment_adoptions a WHERE a.experiment_id = e.id "
            f"AND a.status IN ({_MERGED}))"
            if _columns(conn, "experiment_adoptions") else "0"
        )
        def col(name: str) -> str:
            return f"e.{name}" if name in experiments else "NULL"

        query = f"""
            SELECT e.id AS id,
                   COALESCE({col('implementation_started_at')}, e.created_at) AS started_at,
                   e.ended_at AS ended_at,
                   COALESCE({', '.join(issue_parts)}) AS issue,
                   {col('implementation_provider')} AS provider,
                   e.status AS status,
                   {col('error')} AS error,
                   {col('implementation_usage_json')} AS implementation_usage_json,
                   {col('implementation_runtime_ms')} AS runtime_ms,
                   COALESCE({col('diff_text')}, '') != '' AS has_diff,
                   {merged} AS merged
            FROM experiments e
            WHERE e.ended_at IS NOT NULL AND e.ended_at >= ?
              {"AND COALESCE(" + col('implementation_started_at') + ", e.created_at) < ?" if until else ""}
        """
        params: list[Any] = [(since - HISTORY_LOOKBACK).isoformat()]
        if until is not None:
            params.append(until.isoformat())
        rows = conn.execute(query, params).fetchall()
    finally:
        if owned:
            conn.close()
    out = []
    for row in rows:
        item = dict(row)
        runtime_ms = item.pop("runtime_ms")
        item["duration"] = runtime_ms / 1000.0 if runtime_ms is not None else None
        item["has_diff"] = bool(item["has_diff"])
        item["merged"] = bool(item["merged"])
        out.append(item)
    return out


def report_from_db(
    db: Any, since: Any = None, policies: Any = None, *, until: Any = None,
    history: str = "actual",
) -> tuple[tick_replay.Report, float]:
    """The replay report for experiments started since *since*, and the seconds it took."""
    started = datetime.now(timezone.utc)
    chosen = tick_replay.resolve_policies(policies)
    since_at = parse_since(since)
    until_at = parse_since(until) if until else None
    rows = load_rows(db, since_at, until_at)
    report = tick_replay.replay(rows, chosen, since_at, until_at, history=history)
    return report, (datetime.now(timezone.utc) - started).total_seconds()


def report_dict(db: Any, since: Any = None, policies: Any = None, **kwargs: Any) -> dict[str, Any]:
    report, seconds = report_from_db(db, since, policies, **kwargs)
    data = report.to_dict()
    data["seconds"] = round(seconds, 3)
    return data


__all__ = ["DEFAULT_DAYS", "load_rows", "parse_since", "report_dict", "report_from_db"]
