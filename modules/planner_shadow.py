"""Durable observations from the non-blocking local issue planner (#671)."""

from __future__ import annotations

import sqlite3
import statistics
from typing import Any


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
            error TEXT
        )
        """
    )


def record(db_path: str, **values: Any) -> None:
    with sqlite3.connect(db_path, timeout=30) as conn:
        ensure_table(conn)
        conn.execute(
            """INSERT INTO planner_shadow
               (tick_id, paid_issue, local_issue, local_valid, prompt_tokens, seconds, error)
               VALUES (:tick_id, :paid_issue, :local_issue, :local_valid,
                       :prompt_tokens, :seconds, :error)""",
            values,
        )


def list_shadow(db_path: str, limit: int = 20) -> dict[str, Any]:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        ensure_table(conn)
        rows = [dict(row) for row in conn.execute(
            "SELECT tick_id, paid_issue, local_issue, local_valid, prompt_tokens, seconds, error "
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
