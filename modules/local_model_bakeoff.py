"""Persistence and selection rules for local GGUF model bake-offs (#690)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from modules.run_store import DEFAULT_DB_PATH, _connect


def passing_hunks_per_hour(row: dict[str, Any]) -> float:
    seconds = float(row.get("seconds") or 0)
    return (float(row.get("passing_hunks") or 0) * 3600 / seconds) if seconds > 0 else 0.0


def choose_winner(rows: list[dict[str, Any]], current_model: str) -> dict[str, Any]:
    """Return winner and conservative switch recommendation.

    Failed switches are ineligible. Ranking is throughput first and the number
    of passing hunks second. A switch requires strict improvement on both
    measures and no more than 20% admission failures.
    """
    eligible = [r for r in rows if r.get("status") == "completed"]
    winner = max(eligible, key=lambda r: (float(r.get("passing_hunks_per_hour") or 0),
                                           int(r.get("passing_hunks") or 0)), default=None)
    current = next((r for r in eligible if r.get("model") == current_model), None)
    recommend = False
    if winner is not None and current is not None and winner is not current:
        total = int(winner.get("hunks_total") or 0)
        slow_share = int(winner.get("local_too_slow") or 0) / total if total else 1.0
        recommend = (
            float(winner.get("passing_hunks_per_hour") or 0)
            > float(current.get("passing_hunks_per_hour") or 0)
            and int(winner.get("passing_hunks") or 0) > int(current.get("passing_hunks") or 0)
            and slow_share <= 0.20
        )
    return {"winner": winner, "recommend_switch": recommend, "current": current}


def insert_bakeoff_row(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    columns = (
        "run_id", "created_at", "model", "is_current", "status",
        "prompt_tokens_per_second", "output_tokens_per_second", "hunks_total",
        "hunks_applied", "hunks_compiled", "passing_hunks", "local_too_slow",
        "seconds", "passing_hunks_per_hour", "winner", "recommend_switch",
        "details_json", "error",
    )
    values = [row.get(c) for c in columns]
    cur = conn.execute(
        f"INSERT INTO local_model_bakeoffs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        values,
    )
    conn.commit()
    return int(cur.lastrowid)


def list_bakeoffs(limit: int = 50, *, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM local_model_bakeoffs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    result = []
    for stored in rows:
        row = dict(stored)
        for key in ("is_current", "winner", "recommend_switch"):
            row[key] = bool(row[key])
        try:
            row["details"] = json.loads(row.pop("details_json")) if row.get("details_json") else None
        except (TypeError, ValueError):
            row["details"] = None
            row.pop("details_json", None)
        result.append(row)
    return result
