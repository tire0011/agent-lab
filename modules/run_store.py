import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "/agent/state/agent.db"


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            input_description TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            runtime_ms INTEGER NOT NULL,
            estimated_cost REAL NULL,
            actual_cost REAL NULL,
            cost_status TEXT NOT NULL,
            success INTEGER NOT NULL CHECK (success IN (0, 1)),
            error TEXT NULL
        );

        CREATE TABLE IF NOT EXISTS model_prices (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            input_price_per_million_tokens REAL NOT NULL,
            output_price_per_million_tokens REAL NOT NULL,
            currency TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (provider, model, valid_from)
        );

        CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            runtime_ms INTEGER NOT NULL,
            success INTEGER NOT NULL CHECK (success IN (0, 1)),
            error TEXT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            run_id INTEGER NULL,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL NULL,
            source TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS codex_tasks (
            id TEXT PRIMARY KEY,
            prompt TEXT NOT NULL,
            working_dir TEXT NOT NULL,
            sandbox TEXT NOT NULL,
            timeout_seconds INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT NULL,
            ended_at TEXT NULL,
            thread_id TEXT NULL,
            exit_code INTEGER NULL,
            runtime_ms INTEGER NULL,
            timed_out INTEGER NULL CHECK (timed_out IN (0, 1) OR timed_out IS NULL),
            usage_json TEXT NULL,
            final_message TEXT NULL,
            error TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs(timestamp);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_run_id ON tool_calls(run_id);
        CREATE INDEX IF NOT EXISTS idx_model_prices_lookup ON model_prices(provider, model, valid_from);
        CREATE INDEX IF NOT EXISTS idx_codex_tasks_created_at ON codex_tasks(created_at);
        CREATE INDEX IF NOT EXISTS idx_codex_tasks_status ON codex_tasks(status);
        """
    )
    conn.commit()
    return conn


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
