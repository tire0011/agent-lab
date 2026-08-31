import sqlite3
from typing import Any


def get_applicable_price(
    conn: sqlite3.Connection,
    provider: str,
    model: str,
    at_timestamp: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            provider,
            model,
            input_price_per_million_tokens,
            output_price_per_million_tokens,
            currency,
            valid_from,
            source
        FROM model_prices
        WHERE provider = ? AND model = ? AND valid_from <= ?
        ORDER BY valid_from DESC
        LIMIT 1
        """,
        (provider, model, at_timestamp),
    ).fetchone()

    if row is None:
        return None

    return {
        "provider": row[0],
        "model": row[1],
        "input_price_per_million_tokens": float(row[2]),
        "output_price_per_million_tokens": float(row[3]),
        "currency": row[4],
        "valid_from": row[5],
        "source": row[6],
    }


def calculate_estimated_cost(
    input_tokens: int,
    output_tokens: int,
    input_price_per_million_tokens: float,
    output_price_per_million_tokens: float,
) -> float:
    input_cost = (input_tokens / 1_000_000) * input_price_per_million_tokens
    output_cost = (output_tokens / 1_000_000) * output_price_per_million_tokens
    return input_cost + output_cost


def insert_run(
    conn: sqlite3.Connection,
    *,
    timestamp: str,
    provider: str,
    model: str,
    input_description: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    runtime_ms: int,
    estimated_cost: float | None,
    actual_cost: float | None,
    cost_status: str,
    success: bool,
    error: str | None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO runs (
            timestamp,
            provider,
            model,
            input_description,
            input_tokens,
            output_tokens,
            total_tokens,
            runtime_ms,
            estimated_cost,
            actual_cost,
            cost_status,
            success,
            error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp,
            provider,
            model,
            input_description,
            input_tokens,
            output_tokens,
            total_tokens,
            runtime_ms,
            estimated_cost,
            actual_cost,
            cost_status,
            1 if success else 0,
            error,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_tool_calls(
    conn: sqlite3.Connection,
    run_id: int,
    started_at: str,
    tool_trace: list[dict[str, Any]],
) -> None:
    rows: list[tuple[Any, ...]] = []
    for item in tool_trace:
        has_result = "result" in item
        result_text = str(item.get("result", ""))
        tool_success = has_result and not result_text.startswith("ERROR")
        error_text = None if tool_success else "missing tool output"
        rows.append(
            (
                run_id,
                str(item.get("tool_name", "unknown")),
                started_at,
                0,
                1 if tool_success else 0,
                error_text,
            )
        )

    if not rows:
        return

    conn.executemany(
        """
        INSERT INTO tool_calls (
            run_id,
            tool_name,
            started_at,
            runtime_ms,
            success,
            error
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def insert_activity(
    conn: sqlite3.Connection,
    *,
    timestamp: str,
    entity_type: str,
    entity_id: str,
    event: str,
    from_status: str | None = None,
    to_status: str | None = None,
    message: str | None = None,
) -> int:
    """Append one persistent activity log entry (e.g. an experiment lifecycle event).

    Callers are responsible for their own commit/locking semantics when
    inserting as part of a larger transaction (e.g. ``ExperimentService``
    inserts activity rows inside the same ``with self._conn:`` block used
    for the state transition itself, so a failed transition never leaves an
    orphaned activity row and vice versa).
    """
    cursor = conn.execute(
        """
        INSERT INTO activity_log (
            timestamp,
            entity_type,
            entity_id,
            event,
            from_status,
            to_status,
            message
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (timestamp, entity_type, entity_id, event, from_status, to_status, message),
    )
    return int(cursor.lastrowid)


def list_activity(
    conn: sqlite3.Connection,
    limit: int = 50,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent activity log entries, newest first."""
    query = "SELECT id, timestamp, entity_type, entity_id, event, from_status, to_status, message FROM activity_log"
    conditions: list[str] = []
    params: list[Any] = []
    if entity_type is not None:
        conditions.append("entity_type = ?")
        params.append(entity_type)
    if entity_id is not None:
        conditions.append("entity_id = ?")
        params.append(entity_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row[0],
            "timestamp": row[1],
            "entity_type": row[2],
            "entity_id": row[3],
            "event": row[4],
            "from_status": row[5],
            "to_status": row[6],
            "message": row[7],
        }
        for row in rows
    ]


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the persisted value for ``key`` from ``service_settings``, or
    ``None`` if it has never been set. Used for small, durable pieces of
    adaptive service state (e.g. the auto-tuned verification test timeout)
    that must be reused across process restarts, not just within a single
    long-lived connection."""
    row = conn.execute("SELECT value FROM service_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else None


def set_setting(conn: sqlite3.Connection, key: str, value: str, *, updated_at: str) -> None:
    """Upsert a single ``service_settings`` key/value pair."""
    conn.execute(
        """
        INSERT INTO service_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, updated_at),
    )
    conn.commit()


