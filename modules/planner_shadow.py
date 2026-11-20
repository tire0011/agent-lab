"""Durable observations from the non-blocking local issue planner (#671)."""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Any


# created_at (#716); prompt_variant and cached_prompt_tokens (#770).
_LATER_COLUMNS = (
    ("created_at", "TEXT"),
    ("prompt_variant", "TEXT"),
    ("cached_prompt_tokens", "INTEGER"),
)


def ensure_table(conn: sqlite3.Connection) -> None:
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
    # Columns added after the first rows were written: nullable, added in place.
    present = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
    for name, kind in _LATER_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE planner_shadow ADD COLUMN {name} {kind}")


def record(db_path: str, **values: Any) -> None:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_table(conn)
        values.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        values.setdefault("prompt_variant", None)
        values.setdefault("cached_prompt_tokens", None)
        conn.execute(
            """INSERT INTO planner_shadow
               (tick_id, paid_issue, local_issue, local_valid, prompt_tokens, seconds, error,
                created_at, prompt_variant, cached_prompt_tokens)
               VALUES (:tick_id, :paid_issue, :local_issue, :local_valid,
                       :prompt_tokens, :seconds, :error, :created_at,
                       :prompt_variant, :cached_prompt_tokens)""",
            values,
        )


def list_shadow(db_path: str, limit: int = 20) -> dict[str, Any]:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        ensure_table(conn)
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
