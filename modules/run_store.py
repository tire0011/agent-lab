import sqlite3
import os
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = os.environ.get("AGENT_DB_PATH", "/var/lib/nexus/agent.db")
LEGACY_DB_PATH = "/agent/state/agent.db"


def _migrate_legacy_db_if_needed(target_path: Path) -> None:
    legacy_path = Path(LEGACY_DB_PATH)
    default_target = Path(DEFAULT_DB_PATH)
    if target_path != default_target:
        return
    if target_path == legacy_path:
        return
    if target_path.exists() or not legacy_path.exists():
        return

    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(".migrating")
    if tmp_path.exists():
        tmp_path.unlink()

    source_conn = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True)
    target_conn = sqlite3.connect(tmp_path)
    try:
        source_conn.backup(target_conn)
        target_conn.commit()
    finally:
        target_conn.close()
        source_conn.close()
    tmp_path.replace(target_path)


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    _migrate_legacy_db_if_needed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: callers that keep a long-lived connection
    # (e.g. CodexTaskService) may be invoked from FastAPI's sync-route
    # threadpool, where a given connection can legitimately be used from a
    # different thread on each call. Callers that share a connection across
    # threads remain responsible for serializing concurrent access.
    conn = sqlite3.connect(path, check_same_thread=False)
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

        CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            selection_started_at TEXT NULL,
            selection_ended_at TEXT NULL,
            research_task_id TEXT NULL,
            research_started_at TEXT NULL,
            research_ended_at TEXT NULL,
            research_runtime_ms INTEGER NULL,
            research_usage_json TEXT NULL,
            research_raw_summary TEXT NULL,
            research_diagnostic_log TEXT NULL,
            implementation_started_at TEXT NULL,
            implementation_ended_at TEXT NULL,
            ended_at TEXT NULL,
            plan_json TEXT NULL,
            worktree_path TEXT NULL,
            branch_name TEXT NULL,
            selection_task_id TEXT NULL,
            reformat_retry_task_id TEXT NULL,
            reformat_retry_runtime_ms INTEGER NULL,
            reformat_retry_usage_json TEXT NULL,
            policy_replan_task_id TEXT NULL,
            policy_replan_runtime_ms INTEGER NULL,
            policy_replan_usage_json TEXT NULL,
            selection_raw_message TEXT NULL,
            reformat_raw_message TEXT NULL,
            policy_replan_raw_message TEXT NULL,
            selection_diagnostic_log TEXT NULL,
            reformat_diagnostic_log TEXT NULL,
            policy_replan_diagnostic_log TEXT NULL,
            implementation_task_id TEXT NULL,
            selection_runtime_ms INTEGER NULL,
            implementation_runtime_ms INTEGER NULL,
            selection_usage_json TEXT NULL,
            implementation_usage_json TEXT NULL,
            test_command_json TEXT NULL,
            test_exit_code INTEGER NULL,
            test_output TEXT NULL,
            git_status TEXT NULL,
            diff_stat TEXT NULL,
            diff_text TEXT NULL,
            error TEXT NULL,
            policy_rule TEXT NULL,
            quality_evaluation_json TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs(timestamp);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_run_id ON tool_calls(run_id);
        CREATE INDEX IF NOT EXISTS idx_model_prices_lookup ON model_prices(provider, model, valid_from);
        CREATE INDEX IF NOT EXISTS idx_codex_tasks_created_at ON codex_tasks(created_at);
        CREATE INDEX IF NOT EXISTS idx_codex_tasks_status ON codex_tasks(status);
        CREATE INDEX IF NOT EXISTS idx_experiments_created_at ON experiments(created_at);
        CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);

        CREATE TABLE IF NOT EXISTS mailbox_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            source_commit_sha TEXT NOT NULL,
            msg_type TEXT NOT NULL,
            created_by TEXT NOT NULL,
            status TEXT NOT NULL,
            received_at TEXT NOT NULL,
            processed_at TEXT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NULL,
            idea_id TEXT NULL,
            outbox_file TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mailbox_file_path ON mailbox_messages(file_path);
        CREATE INDEX IF NOT EXISTS idx_mailbox_status ON mailbox_messages(status);
        CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category);

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            event TEXT NOT NULL,
            from_status TEXT NULL,
            to_status TEXT NULL,
            message TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_activity_log_timestamp ON activity_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_activity_log_entity ON activity_log(entity_type, entity_id);
        """
    )
    _migrate_experiments_columns(conn)
    _migrate_mailbox_messages_task_body(conn)
    _migrate_mailbox_task_approvals(conn)
    conn.commit()
    return conn


# Columns that may be missing on an already-existing ``experiments`` table
# (created by an older version of this module). ``CREATE TABLE IF NOT
# EXISTS`` never retroactively adds columns, so any new, additive columns
# must be migrated in explicitly via ``ALTER TABLE ... ADD COLUMN``.
_EXPERIMENTS_ADDITIVE_COLUMNS = [
    ("research_task_id", "TEXT"),
    ("research_started_at", "TEXT"),
    ("research_ended_at", "TEXT"),
    ("research_runtime_ms", "INTEGER"),
    ("research_usage_json", "TEXT"),
    ("research_raw_summary", "TEXT"),
    ("research_diagnostic_log", "TEXT"),
    ("policy_replan_task_id", "TEXT"),
    ("policy_replan_runtime_ms", "INTEGER"),
    ("policy_replan_usage_json", "TEXT"),
    ("policy_replan_raw_message", "TEXT"),
    ("policy_replan_diagnostic_log", "TEXT"),
    ("policy_rule", "TEXT"),
    ("quality_evaluation_json", "TEXT"),
]


def _migrate_experiments_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(experiments)")}
    for column_name, column_type in _EXPERIMENTS_ADDITIVE_COLUMNS:
        if column_name not in existing:
            conn.execute(
                f"ALTER TABLE experiments ADD COLUMN {column_name} {column_type} NULL"
            )


def _migrate_mailbox_messages_task_body(conn: sqlite3.Connection) -> None:
    """Add task_body column to mailbox_messages if not already present."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(mailbox_messages)")}
    if "task_body" not in existing:
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN task_body TEXT NULL")


def _migrate_mailbox_task_approvals(conn: sqlite3.Connection) -> None:
    """Create mailbox_task_approvals table and backfill from existing task messages."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mailbox_task_approvals (
            id TEXT PRIMARY KEY,
            mailbox_msg_id INTEGER NOT NULL REFERENCES mailbox_messages(id),
            task_body TEXT NOT NULL,
            sender TEXT NOT NULL,
            file_path TEXT NOT NULL,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL,
            approved_at TEXT NULL,
            rejected_at TEXT NULL,
            rejection_reason TEXT NULL,
            experiment_id TEXT NULL,
            outbox_file TEXT NULL,
            result_summary TEXT NULL,
            error TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mta_status ON mailbox_task_approvals(status);
        CREATE INDEX IF NOT EXISTS idx_mta_mailbox_msg_id ON mailbox_task_approvals(mailbox_msg_id);
        CREATE INDEX IF NOT EXISTS idx_mta_experiment_id ON mailbox_task_approvals(experiment_id);
        """
    )

    # Backfill: create pending_approval records for any task messages that
    # don't already have one.
    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    orphan_tasks = conn.execute(
        """
        SELECT m.id, m.created_by, m.file_path, m.received_at, m.task_body, m.outbox_file
        FROM mailbox_messages m
        WHERE m.msg_type = 'task'
          AND NOT EXISTS (
              SELECT 1 FROM mailbox_task_approvals a
              WHERE a.mailbox_msg_id = m.id
          )
        """
    ).fetchall()

    for row in orphan_tasks:
        msg_id, created_by, file_path, received_at, task_body, outbox_file = row
        if not task_body:
            fallback = (
                "[Task body was not stored at processing time."
                + (f" See outbox: {outbox_file}" if outbox_file else "")
                + "]"
            )
            task_body = fallback
        approval_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO mailbox_task_approvals
                (id, mailbox_msg_id, task_body, sender, file_path, received_at,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?)
            """,
            (approval_id, msg_id, task_body, created_by or "", file_path or "",
             received_at or now, now, now),
        )


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
