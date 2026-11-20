"""Durable observations from the non-blocking local issue planner (#671).

A shadow observation is written in two steps (#770): ``begin`` stores a
``pending`` row before the local model is called, and ``finish`` fills in the
outcome. A process that dies in between (the tick's oneshot unit ended, a
crash, a reboot) leaves the pending row behind; the next ``begin`` marks such
stale rows ``interrupted`` so every attempt ends with a recorded outcome.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

# created_at (#716); prompt_variant, cached_prompt_tokens and status (#770).
_LATER_COLUMNS = (
    ("created_at", "TEXT"),
    ("prompt_variant", "TEXT"),
    ("cached_prompt_tokens", "INTEGER"),
    ("status", "TEXT"),
)

STATUS_PENDING = "pending"
STATUS_DONE = "done"
INTERRUPTED_ERROR = "interrupted: the process ended before the shadow answered"
# Longer than any shadow may run (timeout cap 240 s plus join grace).
STALE_PENDING_SECONDS = 300.0

# The rollout window of the compact local prompt (#770) and its targets.
ROLLOUT_VARIANT = "compact_v1"
ROLLOUT_WINDOW = 20
ROLLOUT_TARGETS = {"valid_share": 0.8, "median_seconds": 120.0, "agreement_followup": 0.6}

_COLUMNS = ("tick_id", "paid_issue", "local_issue", "local_valid", "prompt_tokens", "seconds",
            "error", "created_at", "prompt_variant", "cached_prompt_tokens", "status")
_OUTCOME = ("local_issue", "local_valid", "prompt_tokens", "seconds", "error",
            "cached_prompt_tokens")
_NOT_PENDING = "(status IS NULL OR status != 'pending')"


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS planner_shadow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_id TEXT NOT NULL,
            paid_issue INTEGER NOT NULL,
            local_issue INTEGER,
            local_valid INTEGER NOT NULL CHECK (local_valid IN (0, 1)),
            prompt_tokens INTEGER NOT NULL,
            seconds REAL NOT NULL,
            error TEXT,
            created_at TEXT,
            prompt_variant TEXT,
            cached_prompt_tokens INTEGER,
            status TEXT
        )
        """
    )


def _missing_columns(conn: sqlite3.Connection) -> list[tuple[str, str]] | None:
    """Columns still to add, or ``None`` when the table does not exist yet."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
    if not present:
        return None
    return [(name, kind) for name, kind in _LATER_COLUMNS if name not in present]


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create or upgrade the table; the caller must hold a write lock.

    Inspection and migration happen inside that lock, so two connections
    cannot both see a missing column and add it twice.
    """
    missing = _missing_columns(conn)
    if missing is None:
        _create(conn)
        missing = _missing_columns(conn) or []
    # Columns added after the first rows were written: nullable, added in place.
    for name, kind in missing:
        conn.execute(f"ALTER TABLE planner_shadow ADD COLUMN {name} {kind}")


def _connect(db_path: str) -> sqlite3.Connection:
    # Autocommit mode: every transaction below is explicit.
    return sqlite3.connect(db_path, timeout=30, isolation_level=None)


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade the schema when needed, serialized across connections."""
    if _missing_columns(conn) == []:
        return  # already current: no write lock for readers
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_table(conn)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _finish_stale(conn: sqlite3.Connection, now: datetime, older_than: float) -> int:
    cutoff = (now - timedelta(seconds=older_than)).isoformat()
    cursor = conn.execute(
        "UPDATE planner_shadow SET status = ?, error = ?, local_valid = 0 "
        "WHERE status = ? AND (created_at IS NULL OR julianday(created_at) <= julianday(?))",
        (STATUS_DONE, INTERRUPTED_ERROR, STATUS_PENDING, cutoff),
    )
    return cursor.rowcount


def finish_stale(db_path: str, *, now: datetime | None = None,
                 older_than: float = STALE_PENDING_SECONDS) -> int:
    """Mark pending rows whose process can no longer answer as interrupted."""
    with _closing(_connect(db_path)) as conn:
        _migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = _finish_stale(conn, now or _now(), older_than)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return count


class _closing:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self.conn

    def __exit__(self, *exc: Any) -> None:
        self.conn.close()


def _insert(db_path: str, values: dict[str, Any], *, stale_first: bool = False) -> int:
    row = {name: values.get(name) for name in _COLUMNS}
    if row["created_at"] is None:
        row["created_at"] = _now().isoformat()
    with _closing(_connect(db_path)) as conn:
        _migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if stale_first:
                _finish_stale(conn, _now(), STALE_PENDING_SECONDS)
            cursor = conn.execute(
                f"INSERT INTO planner_shadow ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join(':' + name for name in _COLUMNS)})",
                row,
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return int(cursor.lastrowid)


def record(db_path: str, **values: Any) -> int:
    """Store one finished observation."""
    values.setdefault("status", STATUS_DONE)
    return _insert(db_path, values)


def begin(db_path: str, **values: Any) -> int:
    """Store a pending observation before the local call; returns its ID.

    Earlier pending rows that are older than any shadow may run are marked
    interrupted first.
    """
    values.update(status=STATUS_PENDING, local_valid=0, local_issue=None,
                  seconds=0.0, error=None)
    return _insert(db_path, values, stale_first=True)


def finish(db_path: str, row_id: int, **outcome: Any) -> bool:
    """Fill in the outcome of a pending row. Only the first call wins, so a
    repeated finish or one after the row was marked interrupted changes nothing."""
    unknown = set(outcome) - set(_OUTCOME)
    if unknown:
        raise ValueError(f"unknown outcome fields: {sorted(unknown)}")
    sets = ", ".join(f"{name} = :{name}" for name in outcome)
    with _closing(_connect(db_path)) as conn:
        _migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                f"UPDATE planner_shadow SET {sets}{', ' if sets else ''}status = :status "
                "WHERE id = :id AND status = :pending",
                {**outcome, "status": STATUS_DONE, "id": row_id, "pending": STATUS_PENDING},
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return cursor.rowcount == 1


def _summary(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    """``rows`` are ``(paid_issue, local_issue, local_valid, seconds)``."""
    total = len(rows)
    valid = [row for row in rows if row[2]]
    return {
        "total": total,
        "agreement": (sum(row[0] == row[1] for row in valid) / len(valid)) if valid else None,
        "valid_share": (len(valid) / total) if total else None,
        "median_seconds": statistics.median(row[3] for row in rows) if rows else None,
    }


def _rollout(conn: sqlite3.Connection) -> dict[str, Any]:
    """The latest compact-prompt observations, judged against #770's targets."""
    rows = conn.execute(
        "SELECT paid_issue, local_issue, local_valid, seconds FROM planner_shadow "
        f"WHERE prompt_variant = ? AND {_NOT_PENDING} ORDER BY id DESC LIMIT ?",
        (ROLLOUT_VARIANT, ROLLOUT_WINDOW),
    ).fetchall()
    summary = _summary([tuple(row) for row in rows])
    complete = summary["total"] >= ROLLOUT_WINDOW
    targets = ROLLOUT_TARGETS
    return {
        "prompt_variant": ROLLOUT_VARIANT,
        "window": ROLLOUT_WINDOW,
        **summary,
        "targets": dict(targets),
        "window_complete": complete,
        "meets_targets": bool(
            complete and summary["valid_share"] is not None
            and summary["valid_share"] >= targets["valid_share"]
            and summary["median_seconds"] is not None
            and summary["median_seconds"] < targets["median_seconds"]
        ),
        "followup_ready": bool(
            complete and summary["agreement"] is not None
            and summary["agreement"] >= targets["agreement_followup"]
        ),
    }


def list_shadow(db_path: str, limit: int = 20) -> dict[str, Any]:
    """Recent finished rows, the all-history summary and the compact rollout.

    ``summary`` covers every finished row, including the old full-prompt
    timeouts; ``rollout`` covers only the latest 20 ``compact_v1`` rows.
    """
    with _closing(_connect(db_path)) as conn:
        _migrate(conn)
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            "SELECT tick_id, paid_issue, local_issue, local_valid, prompt_tokens, seconds, error, "
            "prompt_variant, cached_prompt_tokens "
            f"FROM planner_shadow WHERE {_NOT_PENDING} ORDER BY id DESC LIMIT ?", (limit,)
        )]
        all_rows = [tuple(row) for row in conn.execute(
            "SELECT paid_issue, local_issue, local_valid, seconds FROM planner_shadow "
            f"WHERE {_NOT_PENDING}"
        )]
        pending = conn.execute(
            "SELECT COUNT(*) FROM planner_shadow WHERE status = ?", (STATUS_PENDING,)
        ).fetchone()[0]
        rollout = _rollout(conn)
    return {
        "count": len(rows),
        "rows": rows,
        "summary": {**_summary(all_rows), "pending": int(pending)},
        "rollout": rollout,
    }
