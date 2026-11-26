"""Durable observations from the non-blocking local issue planner (#671)."""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timezone
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
            error TEXT,
            created_at TEXT,
            prompt_variant TEXT,
            cached_prompt_tokens INTEGER
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
    if "created_at" not in columns:
        conn.execute("ALTER TABLE planner_shadow ADD COLUMN created_at TEXT")
    if "prompt_variant" not in columns:
        conn.execute("ALTER TABLE planner_shadow ADD COLUMN prompt_variant TEXT")
    if "cached_prompt_tokens" not in columns:
        conn.execute("ALTER TABLE planner_shadow ADD COLUMN cached_prompt_tokens INTEGER")


def record(db_path: str, **values: Any) -> None:
    """Keep the first completed observation for each tick, including retries."""
    cached = values.get("cached_prompt_tokens")
    # Check the range before converting. The integer comparison also rejects
    # NaN and infinities without converting large Python integers to floats.
    values["cached_prompt_tokens"] = (
        int(cached)
        if (type(cached) is int or (type(cached) is float and cached.is_integer()))
        and 0 <= cached <= 2**63 - 1
        else None
    )
    values.setdefault("prompt_variant", None)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_table(conn)
        values.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        conn.execute(
            """INSERT INTO planner_shadow
               (tick_id, paid_issue, local_issue, local_valid, prompt_tokens, seconds, error,
                created_at, prompt_variant, cached_prompt_tokens)
               SELECT :tick_id, :paid_issue, :local_issue, :local_valid,
                      :prompt_tokens, :seconds, :error, :created_at, :prompt_variant,
                      :cached_prompt_tokens
               WHERE NOT EXISTS (SELECT 1 FROM planner_shadow WHERE tick_id = :tick_id)""",
            values,
        )


def list_shadow(db_path: str, limit: int = 20) -> dict[str, Any]:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
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
