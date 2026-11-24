import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .prices import WILDCARD_MODEL


def upsert_local_localization(
    conn: sqlite3.Connection,
    *,
    created_at: str,
    commit_sha: str,
    issue_number: int | None,
    predicted: list[str],
    actual: list[str],
    recall_at_5: float,
    precision_at_5: float,
    seconds: float,
) -> None:
    """Persist one #696 replay result, replacing a prior run for the commit."""
    conn.execute(
        """INSERT INTO local_localizations
           (created_at, commit_sha, issue_number, predicted_json, actual_json,
            recall_at_5, precision_at_5, seconds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(commit_sha) DO UPDATE SET
             created_at=excluded.created_at, issue_number=excluded.issue_number,
             predicted_json=excluded.predicted_json, actual_json=excluded.actual_json,
             recall_at_5=excluded.recall_at_5, precision_at_5=excluded.precision_at_5,
             seconds=excluded.seconds""",
        (created_at, commit_sha, issue_number, json.dumps(predicted), json.dumps(actual),
         recall_at_5, precision_at_5, seconds),
    )
    conn.commit()


def list_local_localizations(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Read recent #696 localization replay results, newest first."""
    rows = conn.execute(
        """SELECT id, created_at, commit_sha, issue_number, predicted_json, actual_json,
                  recall_at_5, precision_at_5, seconds
           FROM local_localizations ORDER BY created_at DESC, id DESC LIMIT ?""",
        (max(0, int(limit)),),
    ).fetchall()

    def paths(raw: Any) -> list[str]:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    return [
        {"id": row[0], "created_at": row[1], "commit_sha": row[2],
         "issue_number": row[3], "predicted": paths(row[4]),
         "actual": paths(row[5]), "recall_at_5": row[6],
         "precision_at_5": row[7], "seconds": row[8]}
        for row in rows
    ]


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
            source, cache_read_rate, cache_write_rate
        FROM model_prices
        WHERE provider = ? AND model = ? AND julianday(valid_from) <= julianday(?)
        ORDER BY julianday(valid_from) DESC
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
        "cache_read_rate": row[7],
        "cache_write_rate": row[8],
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


def get_effective_price(
    conn: sqlite3.Connection,
    provider: str,
    model: str,
    at_timestamp: str,
) -> dict[str, Any] | None:
    """Resolve exact prices and explicitly supported model aliases."""
    price = get_applicable_price(conn, provider, model, at_timestamp)
    if model == WILDCARD_MODEL:
        return None
    if price:
        return price
    aliases = {
        "claude": {
            "claude-sonnet-4-5-20250929": "claude-sonnet-4-5",
            "claude-haiku-4-5-20251001": "claude-haiku-4-5",
            "claude-opus-4-5-20251101": "claude-opus-4-5",
            "claude-opus-4-1-20250805": "claude-opus-4-1",
        },
        "codex": {
            "gpt-5.4-2026-03-05": "gpt-5.4",
            "gpt-5.5-2026-04-23": "gpt-5.5",
        },
    }
    alias = aliases.get(provider, {}).get(model)
    return get_applicable_price(conn, provider, alias, at_timestamp) if alias else None


#: Cost statuses a run row can carry. ``actual`` = the tool reported a money
#: amount (Vibe's export.json cost_usd);
#: ``estimated`` = computed from usage and the price table; ``subscription`` =
#: billed by a subscription, no per-token price applies (#810); ``free`` =
#: genuinely free (local model, free API tier); ``unknown`` = no usage and no
#: price, nothing honest to store.
COST_STATUS_ACTUAL = "actual"
COST_STATUS_ESTIMATED = "estimated"
COST_STATUS_SUBSCRIPTION = "subscription"
COST_STATUS_FREE = "free"
COST_STATUS_UNKNOWN = "unknown"

#: Statuses that count as "this run has a cost value or a subscription mark"
#: for the #810 completeness metric.
COSTED_STATUSES = (COST_STATUS_SUBSCRIPTION,)


def _token_cost(provider: str, usage: dict[str, int], price: dict[str, Any]) -> float | None:
    if not isinstance(usage, dict) or not {"input_tokens", "output_tokens"} <= usage.keys():
        return None
    if any(type(value) is not int or not 0 <= value < 2**63 for value in usage.values()):
        return None
    input_tokens = usage["input_tokens"]
    input_rate = price["input_price_per_million_tokens"]
    cache_cost = 0.0
    if provider == "codex":
        cached = usage.get("cached_input_tokens", 0)
        if price["model"] in {"gpt-5.4", "gpt-5.5"} and input_tokens > 272_000:
            # The aggregate cannot distinguish many short prompts from a long one.
            return None
        if (cached > input_tokens or cached and price["cache_read_rate"] is None
                or usage.get("cache_write_input_tokens", 0)):
            return None
        input_tokens -= cached
        cache_cost = cached * (price["cache_read_rate"] or 0)
    elif provider == "claude":
        cached = usage.get("cache_read_input_tokens", 0)
        created = usage.get("cache_creation_input_tokens", 0)
        long_lived = usage.get("cache_creation_1h_input_tokens", 0)
        if (cached and price["cache_read_rate"] is None
                or created and price["cache_write_rate"] is None or long_lived > created):
            return None
        cache_cost = (
            cached * (price["cache_read_rate"] or 0)
            + (created - long_lived) * (price["cache_write_rate"] or 0)
            + long_lived * input_rate * 2
        )
    return calculate_estimated_cost(
        input_tokens, usage["output_tokens"], input_rate,
        price["output_price_per_million_tokens"],
    ) + cache_cost / 1_000_000


def resolve_run_cost(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    timestamp: str,
    usage: dict[str, int] | None = None,
    model_usage: dict[str, dict[str, int]] | None = None,
    actual_cost: float | None = None,
    subscription: bool = False,
) -> dict[str, Any]:
    """Resolve the cost from the billing mode and captured provider evidence."""
    fields = dict(estimated_cost=None, actual_cost=None, cost_status="unknown", cost_currency=None)
    if subscription:
        return dict(fields, cost_status="subscription")
    if type(actual_cost) in (int, float) and 0 <= actual_cost < float("inf"):
        return dict(fields, actual_cost=actual_cost, cost_status="actual", cost_currency="USD")
    usages = model_usage if model_usage is not None else ({model: usage} if usage else {})
    if not usages or provider == "vibe":
        return fields
    total = 0.0
    currency = None
    for model_id, counters in usages.items():
        price = get_effective_price(conn, provider, model_id, timestamp)
        if price is None or currency is not None and price["currency"] != currency:
            return fields
        cost = _token_cost(provider, counters, price)
        if cost is None:
            return fields
        currency = price["currency"]
        total += cost
    return dict(fields, estimated_cost=total, cost_status="estimated", cost_currency=currency)


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
    cost_currency: str | None = None,
    run_kind: str | None = None,
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
            cost_currency,
            run_kind,
            success,
            error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            cost_currency,
            run_kind,
            1 if success else 0,
            error,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


#: #810: the one query that gives the cost per provider per week. Cost-bearing
#: runs contribute ``actual_cost`` (tool-reported) or ``estimated_cost``
#: (usage x price table); subscription runs are counted but add no money.
WEEKLY_COST_BY_PROVIDER_SQL = """
SELECT
    date(timestamp, '-6 days', 'weekday 1') AS week,
    provider,
    COUNT(*) AS runs,
    SUM(CASE WHEN actual_cost IS NOT NULL OR estimated_cost IS NOT NULL
                  OR cost_status = 'subscription' THEN 1 ELSE 0 END)
        AS runs_with_cost,
    SUM(CASE WHEN cost_status = 'subscription' THEN 1 ELSE 0 END) AS subscription_runs,
    SUM(COALESCE(actual_cost, estimated_cost)) AS total_cost,
    cost_currency AS currency
FROM runs
WHERE julianday(timestamp) >= julianday(?)
GROUP BY week, provider, currency
ORDER BY week DESC, provider, currency
"""


def weekly_cost_by_provider(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    days: int = 7,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Cost per provider per week (#810). *since* defaults to *days* before *now*."""
    if since is None:
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(days=days)).isoformat()
    rows = conn.execute(WEEKLY_COST_BY_PROVIDER_SQL, (since,)).fetchall()
    return [
        {
            "week": row[0],
            "provider": row[1],
            "runs": int(row[2]),
            "runs_with_cost": int(row[3] or 0),
            "subscription_runs": int(row[4] or 0),
            "total_cost": row[5],
            "currency": row[6],
        }
        for row in rows
    ]


def backfill_run_costs(conn: sqlite3.Connection, *, after_id: int = 0) -> int:
    """Backfill legacy Azure usage with a known deployment and applicable price.

    Codex and Claude aggregates omit cache, model, or billing-mode evidence.
    The caller commits the estimates and migration checkpoint together.
    """
    rows = conn.execute(
        """
        SELECT id, provider, model, timestamp, input_tokens, output_tokens
        FROM runs
        WHERE id > ?
          AND provider = 'azure_openai' AND model = 'gpt-4o' AND cost_status = 'unknown'
          AND estimated_cost IS NULL AND actual_cost IS NULL
          AND (input_tokens > 0 OR output_tokens > 0)
        ORDER BY id
        """,
        (after_id,),
    ).fetchall()
    upto = after_id
    for run_id, provider, model, timestamp, input_tokens, output_tokens in rows:
        upto = max(upto, int(run_id))
        price = get_effective_price(conn, str(provider), str(model), str(timestamp))
        if price is None:
            continue
        cost = calculate_estimated_cost(
            int(input_tokens),
            int(output_tokens),
            price["input_price_per_million_tokens"],
            price["output_price_per_million_tokens"],
        )
        conn.execute(
            """
            UPDATE runs
            SET estimated_cost = ?, cost_status = 'estimated', cost_currency = ?
            WHERE id = ? AND cost_status = 'unknown'
            """,
            (cost, price["currency"], run_id),
        )
    return upto


def run_cost_completeness(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    now: datetime | None = None,
    providers: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """#810 done-criterion: the share of runs in the last *days* days that have a
    cost value or a subscription mark. With *providers*, only those providers
    are counted (e.g. the coding agents); by default every run counts."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    sql = (
        "SELECT COUNT(*), "
        "SUM(CASE WHEN actual_cost IS NOT NULL OR estimated_cost IS NOT NULL "
        "          OR cost_status = 'subscription' THEN 1 ELSE 0 END) "
        "FROM runs WHERE julianday(timestamp) BETWEEN julianday(?) AND julianday(?)"
    )
    params: list[Any] = [since, now.isoformat()]
    if providers:
        placeholders = ", ".join("?" for _ in providers)
        sql += f" AND provider IN ({placeholders})"
        params.extend(providers)
    total, with_cost = conn.execute(sql, params).fetchone()
    total = int(total)
    with_cost = int(with_cost or 0)
    return {
        "since": since,
        "days": days,
        "providers": list(providers) if providers else None,
        "runs": total,
        "runs_with_cost_or_subscription": with_cost,
        "share": (with_cost / total) if total else None,
    }


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
