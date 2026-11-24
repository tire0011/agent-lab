"""Durable observations from the non-blocking local issue planner (#671).

Each row is one shadow call of one tick: which issue the paid planner chose,
what the local model answered, whether that answer was one of the allowed
issues, how long the call took and what went wrong. Since #799 the row also
records the prompt variant (``prompt_variant``, e.g. ``compact_v1``) and the
prompt tokens llama-server reused from its cache (``cached_prompt_tokens``,
NULL when the shadow callable did not report them). Both columns are nullable
and are added to older tables with ``ALTER TABLE``, exactly like
``created_at``.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

# Columns an older planner_shadow table may lack; added with ALTER TABLE,
# in the same way created_at has always been migrated (#799).
_MIGRATED_COLUMNS = (
    ("created_at", "TEXT"),
    ("prompt_variant", "TEXT"),
    ("cached_prompt_tokens", "INTEGER"),
)

# SQLite stores INTEGER as a signed 64-bit value; a larger Python int raises
# OverflowError when it is bound, which would lose the whole row (#799).
_SQLITE_MAX_INTEGER = 2**63 - 1


def cached_token_count(value: Any) -> int | None:
    """``value`` as a storable cached-prompt-token count, or None.

    The cache count is optional telemetry: it must never invalidate a planner
    answer or keep its row from being stored. Booleans, non-numbers, NaN,
    infinities, negative values, non-integral floats and values beyond SQLite's
    signed 64-bit range all become None (stored as NULL). Never raises.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if value < 0 or value > _SQLITE_MAX_INTEGER:
        return None
    return int(value)


def _missing_columns(conn: sqlite3.Connection) -> list[tuple[str, str]] | None:
    """The migrated columns the table lacks; None when the table is missing."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
    if not columns:
        return None
    return [(name, decl) for name, decl in _MIGRATED_COLUMNS if name not in columns]


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create or migrate planner_shadow.

    The caller must hold the write lock (``BEGIN IMMEDIATE``) so that the
    column check and the ALTER TABLE statements are one serialized step: two
    connections migrating at once would otherwise both see the column missing
    and the second ALTER would fail with "duplicate column name" (#799).
    """
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
            cached_prompt_tokens INTEGER
        )
        """
    )
    for name, declaration in _missing_columns(conn) or []:
        conn.execute(f"ALTER TABLE planner_shadow ADD COLUMN {name} {declaration}")


def _ensure_table_locked(conn: sqlite3.Connection) -> None:
    """Migrate under ``BEGIN IMMEDIATE`` only when something is missing.

    The unlocked check is just a fast path for readers of an up-to-date table;
    ensure_table re-checks under the write lock, so a reader racing another
    migration finds the columns already there and alters nothing. SQLite DDL
    is transactional: an interrupted migration rolls back completely and the
    next call simply runs it again.
    """
    if _missing_columns(conn) == []:
        return
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_table(conn)


def record(db_path: str, **values: Any) -> None:
    """Keep the first completed observation for each tick, including retries."""
    with closing(sqlite3.connect(db_path, timeout=30)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_table(conn)
        values.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        values.setdefault("prompt_variant", None)
        values["cached_prompt_tokens"] = cached_token_count(values.get("cached_prompt_tokens"))
        conn.execute(
            """INSERT INTO planner_shadow
               (tick_id, paid_issue, local_issue, local_valid, prompt_tokens, seconds, error,
                created_at, prompt_variant, cached_prompt_tokens)
               SELECT :tick_id, :paid_issue, :local_issue, :local_valid,
                      :prompt_tokens, :seconds, :error, :created_at,
                      :prompt_variant, :cached_prompt_tokens
               WHERE NOT EXISTS (SELECT 1 FROM planner_shadow WHERE tick_id = :tick_id)""",
            values,
        )


def list_shadow(db_path: str, limit: int = 20) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_table_locked(conn)
        rows = [dict(row) for row in conn.execute(
            "SELECT tick_id, paid_issue, local_issue, local_valid, prompt_tokens, seconds, error, "
            "prompt_variant, cached_prompt_tokens "
            "FROM planner_shadow ORDER BY id DESC LIMIT ?", (limit,)
        )]
        all_rows = conn.execute(
            "SELECT paid_issue, local_issue, local_valid, seconds FROM planner_shadow"
        ).fetchall()
    total = len(all_rows)
    valid = [row for row in all_rows if row[2]]
    return {
        "count": len(rows),
        "rows": rows,
        "summary": {
            "total": total,
            "agreement": (sum(row[0] == row[1] for row in valid) / len(valid)) if valid else None,
            "valid_share": (len(valid) / total) if total else None,
            "median_seconds": statistics.median(row[3] for row in all_rows) if all_rows else None,
        },
    }
