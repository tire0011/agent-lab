import os
import sqlite3
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
    # check_same_thread=False: long-lived connections may be used from FastAPI's
    # threadpool; callers sharing one across threads must serialize access.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_durability(conn)
    return conn


# Production keeps SQLite's default durability (every commit fsynced). Test
# databases are temporary, so WAL + synchronous=NORMAL is used there: fsync is
# the biggest cost of the suite on the VM.
_DURABILITY_ENV_VAR = "NEXUS_DB_DURABILITY"


def _apply_durability(conn: sqlite3.Connection) -> None:
    mode = (os.environ.get(_DURABILITY_ENV_VAR) or "").strip().lower()
    if mode != "relaxed":
        return
    # journal_mode is persistent in the file; synchronous is per connection,
    # so both are set on every connect rather than once at creation.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")


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
            provider TEXT NULL,
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
            implementation_provider TEXT NULL,
            complexity TEXT NULL,
            source_type TEXT NULL,
            source_id TEXT NULL,
            implementation_started_at TEXT NULL,
            implementation_ended_at TEXT NULL,
            ended_at TEXT NULL,
            cancelled_at TEXT NULL,
            cancelled_by TEXT NULL,
            worker_pgid INTEGER NULL,
            failure_phase TEXT NULL,
            failure_type TEXT NULL,
            plan_json TEXT NULL,
            worktree_path TEXT NULL,
            branch_name TEXT NULL,
            base_commit_sha TEXT NULL,
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
            quality_evaluation_json TEXT NULL,
            baseline_test_classifications_json TEXT NULL
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

        CREATE TABLE IF NOT EXISTS verification_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL,
            iteration INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            status TEXT NOT NULL,
            commit_sha TEXT NULL,
            command_json TEXT NULL,
            exit_code INTEGER NULL,
            output TEXT NULL,
            error TEXT NULL,
            failure_fingerprint TEXT NULL,
            FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_verification_runs_experiment
            ON verification_runs(experiment_id, iteration);

        CREATE TABLE IF NOT EXISTS service_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS issue_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            status TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            source_mailbox_msg_id INTEGER,
            decided_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(repository, issue_number)
        );

        CREATE INDEX IF NOT EXISTS idx_issue_approvals_lookup
            ON issue_approvals(repository, issue_number);
        """
    )
    _migrate_experiments_columns(conn)
    _migrate_codex_tasks_columns(conn)
    _migrate_mailbox_messages_task_body(conn)
    _migrate_mailbox_messages_chain_columns(conn)
    _migrate_mailbox_task_approvals(conn)
    _migrate_experiment_adoptions(conn)
    _migrate_operations_audit(conn)
    _migrate_operation_chains(conn)
    _migrate_operation_log(conn)
    _migrate_failure_analyses(conn)
    _migrate_router_tables(conn)
    _migrate_benchmark_runs(conn)
    conn.commit()
    return conn


# Columns an older ``experiments`` table may lack; ``CREATE TABLE IF NOT
# EXISTS`` never adds columns, so they are added with ``ALTER TABLE``.
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
    ("baseline_test_classifications_json", "TEXT"),
    # Which coding agent ran the implementation phase (codex | claude).
    ("implementation_provider", "TEXT"),
    ("complexity", "TEXT"),
    ("source_type", "TEXT"),
    ("source_id", "TEXT"),
    ("cancelled_at", "TEXT"),
    ("cancelled_by", "TEXT"),
    ("base_commit_sha", "TEXT"),
    # Process group of the live worker, so the stall sweep (#400) can stop it
    # from another process; NULL while no worker runs.
    ("worker_pgid", "INTEGER"),
    # Where and how an unhandled pipeline exception happened (#410); NULL otherwise.
    ("failure_phase", "TEXT"),
    ("failure_type", "TEXT"),
]

# Same additive treatment for ``codex_tasks``: the table predates the second
# coding agent, so rows written before this column existed keep provider NULL
# and are read back as the historical default (codex).
_CODEX_TASKS_ADDITIVE_COLUMNS = [
    ("provider", "TEXT"),
]


def _migrate_experiments_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(experiments)")}
    for column_name, column_type in _EXPERIMENTS_ADDITIVE_COLUMNS:
        if column_name not in existing:
            conn.execute(
                f"ALTER TABLE experiments ADD COLUMN {column_name} {column_type} NULL"
            )


def _migrate_codex_tasks_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(codex_tasks)")}
    for column_name, column_type in _CODEX_TASKS_ADDITIVE_COLUMNS:
        if column_name not in existing:
            conn.execute(
                f"ALTER TABLE codex_tasks ADD COLUMN {column_name} {column_type} NULL"
            )


def _migrate_mailbox_messages_task_body(conn: sqlite3.Connection) -> None:
    """Add task_body column to mailbox_messages if not already present."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(mailbox_messages)")}
    if "task_body" not in existing:
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN task_body TEXT NULL")


def _migrate_mailbox_messages_chain_columns(conn: sqlite3.Connection) -> None:
    """Add chain_request_id and content_hash columns for operation_chain idempotency."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(mailbox_messages)")}
    if "chain_request_id" not in existing:
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN chain_request_id TEXT NULL")
    if "content_hash" not in existing:
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN content_hash TEXT NULL")


_MAILBOX_TASK_APPROVALS_ADDITIVE_COLUMNS = [
    ("publish_status", "TEXT"),
    ("publish_commit_sha", "TEXT"),
    ("publish_pushed_at", "TEXT"),
    ("publish_retry_count", "INTEGER NOT NULL DEFAULT 0"),
    ("publish_last_error", "TEXT"),
    ("outbox_content", "TEXT"),
    ("idempotency_key", "TEXT"),
    ("merge_forbidden", "INTEGER NOT NULL DEFAULT 0"),
    ("merge_constraint_reason", "TEXT"),
]


def record_merge_constraint(conn: sqlite3.Connection, approval_id: str, task_body: str | None) -> None:
    """Persist a task's explicit no-merge instruction on its approval (#185)."""
    from modules.work_directives import parse_work_directives

    directives = parse_work_directives(task_body)
    if directives.forbid_merge:
        conn.execute(
            """UPDATE mailbox_task_approvals
               SET merge_forbidden=1, merge_constraint_reason=? WHERE id=?""",
            (directives.merge_constraint_reason, approval_id),
        )


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
            outbox_content TEXT NULL,
            publish_status TEXT NULL,
            publish_commit_sha TEXT NULL,
            publish_pushed_at TEXT NULL,
            publish_retry_count INTEGER NOT NULL DEFAULT 0,
            publish_last_error TEXT NULL,
            result_summary TEXT NULL,
            error TEXT NULL,
            merge_forbidden INTEGER NOT NULL DEFAULT 0,
            merge_constraint_reason TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mta_status ON mailbox_task_approvals(status);
        CREATE INDEX IF NOT EXISTS idx_mta_mailbox_msg_id ON mailbox_task_approvals(mailbox_msg_id);
        CREATE INDEX IF NOT EXISTS idx_mta_experiment_id ON mailbox_task_approvals(experiment_id);
        """
    )
    # Additive migration: add publish columns to pre-existing tables that were
    # created before these columns were introduced.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(mailbox_task_approvals)")}
    for column_name, column_type in _MAILBOX_TASK_APPROVALS_ADDITIVE_COLUMNS:
        if column_name not in existing:
            conn.execute(
                f"ALTER TABLE mailbox_task_approvals ADD COLUMN {column_name} {column_type}"
            )

    # Idempotency keys (#378): older rows get the key their message would derive
    # today, so a redelivery is still recognised; only the oldest row per
    # message gets it (duplicates would break the unique index).
    conn.execute(
        """
        UPDATE mailbox_task_approvals
        SET idempotency_key = 'mailbox-msg:' || mailbox_msg_id
        WHERE idempotency_key IS NULL
          AND rowid IN (
              SELECT MIN(rowid) FROM mailbox_task_approvals
              WHERE idempotency_key IS NULL
              GROUP BY mailbox_msg_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM mailbox_task_approvals b
              WHERE b.idempotency_key = 'mailbox-msg:' || mailbox_task_approvals.mailbox_msg_id
          )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mta_idempotency_key "
        "ON mailbox_task_approvals(idempotency_key)"
    )

    # The approval step is gone (#264). An approved task was only waiting for
    # the worker, which now takes queued ones. Old pending_approval and
    # rejected rows stay as they are: readable, never run.
    conn.execute(
        "UPDATE mailbox_task_approvals SET status='queued' WHERE status='approved'"
    )

    # Backfill: create task records for any task messages that don't already
    # have one. A task whose body was not stored is not handed to Nexus.
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
        status, error = "queued", None
        if not task_body:
            fallback = (
                "[Task body was not stored at processing time."
                + (f" See outbox: {outbox_file}" if outbox_file else "")
                + "]"
            )
            task_body = fallback
            status, error = "needs_review", "task body was not stored; not handed to Nexus"
        approval_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO mailbox_task_approvals
                (id, mailbox_msg_id, task_body, sender, file_path, received_at,
                 status, error, idempotency_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (approval_id, msg_id, task_body, created_by or "", file_path or "",
             received_at or now, status, error, f"mailbox-msg:{msg_id}", now, now),
        )

    # Rows created before the structured constraint, and the orphans above,
    # keep their original body: a restart must not forget an older
    # "do not merge" instruction.
    candidates = conn.execute(
        "SELECT id, task_body FROM mailbox_task_approvals WHERE merge_forbidden=0"
    ).fetchall()
    for approval_id, task_body in candidates:
        record_merge_constraint(conn, approval_id, task_body)


def _migrate_experiment_adoptions(conn: sqlite3.Connection) -> None:
    """Create experiment_adoptions table (idempotent additive migration)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS experiment_adoptions (
            id TEXT PRIMARY KEY,
            experiment_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NULL,
            ended_at TEXT NULL,
            experiment_commit_sha TEXT NULL,
            integrated_commit_sha TEXT NULL,
            target_branch TEXT NOT NULL DEFAULT 'main',
            expected_files_json TEXT NULL,
            actual_files_json TEXT NULL,
            test_exit_code INTEGER NULL,
            test_output TEXT NULL,
            reason TEXT NULL,
            local_sync_pending INTEGER NOT NULL DEFAULT 0,
            diagnostic_worktree_path TEXT NULL,
            review_blocker TEXT NULL,
            recovery_attempts INTEGER NOT NULL DEFAULT 0,
            stale_retry_attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(experiment_id)
        );

        CREATE INDEX IF NOT EXISTS idx_experiment_adoptions_status
            ON experiment_adoptions(status);
        CREATE INDEX IF NOT EXISTS idx_experiment_adoptions_experiment_id
            ON experiment_adoptions(experiment_id);

        -- Durable attempt history. An adoption row is reused across
        -- retries (there is exactly one per experiment), so a superseded
        -- failure would otherwise be overwritten and lost. Each retry
        -- appends the failure it replaced here, which is what keeps the
        -- original reason auditable after the row moves on.
        CREATE TABLE IF NOT EXISTS experiment_adoption_failures (
            id TEXT PRIMARY KEY,
            adoption_id TEXT NOT NULL,
            experiment_id TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NULL,
            test_exit_code INTEGER NULL,
            test_output TEXT NULL,
            experiment_commit_sha TEXT NULL,
            started_at TEXT NULL,
            ended_at TEXT NULL,
            superseded_at TEXT NOT NULL,
            superseded_by TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_experiment_adoption_failures_experiment_id
            ON experiment_adoption_failures(experiment_id, superseded_at);
        """
    )
    # CREATE TABLE IF NOT EXISTS never adds columns to a table an older
    # version already created, so the attempt counters are migrated in
    # explicitly.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(experiment_adoptions)")}
    if "recovery_attempts" not in existing:
        conn.execute(
            "ALTER TABLE experiment_adoptions ADD COLUMN recovery_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "stale_retry_attempts" not in existing:
        conn.execute(
            "ALTER TABLE experiment_adoptions ADD COLUMN stale_retry_attempts INTEGER NOT NULL DEFAULT 0"
        )
    # Which re-evaluable condition produced a `needs_review` row (#171).
    # The reason text alone cannot be re-checked; this names the blocker so
    # a later attempt can ask whether it still applies.
    if "review_blocker" not in existing:
        conn.execute("ALTER TABLE experiment_adoptions ADD COLUMN review_blocker TEXT NULL")


def _migrate_operations_audit(conn: sqlite3.Connection) -> None:
    """Create operations_audit table (idempotent additive migration)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS operations_audit (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            operation TEXT NOT NULL,
            args_json TEXT NULL,
            success INTEGER NOT NULL CHECK (success IN (0, 1)),
            duration_ms INTEGER NOT NULL,
            error_summary TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_operations_audit_timestamp
            ON operations_audit(timestamp);
        CREATE INDEX IF NOT EXISTS idx_operations_audit_operation
            ON operations_audit(operation);
        """
    )


# Columns an older ``operation_chains`` table may lack; added with ``ALTER TABLE``.
_OPERATION_CHAINS_ADDITIVE_COLUMNS = [
    ("outbox_file", "TEXT"),
    ("publish_status", "TEXT"),
    ("publish_commit_sha", "TEXT"),
    ("publish_pushed_at", "TEXT"),
    ("publish_retry_count", "INTEGER NOT NULL DEFAULT 0"),
    ("publish_last_error", "TEXT"),
    ("outbox_content", "TEXT"),
]


def _migrate_operation_chains(conn: sqlite3.Connection) -> None:
    """Create operation_chains table (idempotent additive migration)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS operation_chains (
            id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT NULL,
            ended_at TEXT NULL,
            request_json TEXT NOT NULL,
            result_json TEXT NULL,
            steps_successful INTEGER DEFAULT 0,
            steps_failed INTEGER DEFAULT 0,
            outbox_file TEXT NULL,
            outbox_content TEXT NULL,
            publish_status TEXT NULL,
            publish_commit_sha TEXT NULL,
            publish_pushed_at TEXT NULL,
            publish_retry_count INTEGER NOT NULL DEFAULT 0,
            publish_last_error TEXT NULL,
            error TEXT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_operation_chains_status
            ON operation_chains(status);
        CREATE INDEX IF NOT EXISTS idx_operation_chains_request_id
            ON operation_chains(request_id);
        CREATE INDEX IF NOT EXISTS idx_operation_chains_created_at
            ON operation_chains(created_at);
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(operation_chains)")}
    for column_name, column_type in _OPERATION_CHAINS_ADDITIVE_COLUMNS:
        if column_name not in existing:
            conn.execute(
                f"ALTER TABLE operation_chains ADD COLUMN {column_name} {column_type}"
            )


# Additive migration (#148): correlation ids + the shared outbox publish
# lifecycle columns (see modules.outbox_publisher / modules.failure_analysis_service).
_FAILURE_ANALYSES_ADDITIVE_COLUMNS = [
    ("experiment_id", "TEXT"),
    ("mailbox_task_id", "TEXT"),
    ("outbox_file", "TEXT"),
    ("outbox_content", "TEXT"),
    ("publish_status", "TEXT"),
    ("publish_commit_sha", "TEXT"),
    ("publish_pushed_at", "TEXT"),
    ("publish_retry_count", "INTEGER NOT NULL DEFAULT 0"),
    ("publish_last_error", "TEXT"),
    ("updated_at", "TEXT"),
]


def _migrate_failure_analyses(conn: sqlite3.Connection) -> None:
    """Create failure_analyses table (idempotent additive migration).

    Owned here so every init_db() caller - including the outbox publisher,
    which never builds a FailureAnalysisService - can rely on the table.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS failure_analyses (
            id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT,
            category TEXT NOT NULL,
            confidence REAL NOT NULL,
            can_self_recover INTEGER NOT NULL DEFAULT 0,
            needs_external_model INTEGER NOT NULL DEFAULT 0,
            needs_human INTEGER NOT NULL DEFAULT 0,
            analysis_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_failure_analyses_subject
            ON failure_analyses(subject_type, subject_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_failure_analyses_recent
            ON failure_analyses(created_at);
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(failure_analyses)")}
    for column_name, column_type in _FAILURE_ANALYSES_ADDITIVE_COLUMNS:
        if column_name not in existing:
            conn.execute(
                f"ALTER TABLE failure_analyses ADD COLUMN {column_name} {column_type}"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_failure_analyses_mailbox_task "
        "ON failure_analyses(mailbox_task_id)"
    )


def _migrate_router_tables(conn: sqlite3.Connection) -> None:
    """Create router_decisions, provider_usage, and model_consultations tables."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS router_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            task TEXT NOT NULL,
            decision TEXT NOT NULL,
            provider TEXT NULL,
            role TEXT NULL,
            confidence REAL NOT NULL,
            reasoning TEXT NOT NULL,
            consult_query TEXT NULL,
            sub_tasks_json TEXT NULL,
            raw_response TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_router_decisions_timestamp
            ON router_decisions(timestamp);
        CREATE INDEX IF NOT EXISTS idx_router_decisions_decision
            ON router_decisions(decision);
        CREATE INDEX IF NOT EXISTS idx_router_decisions_provider
            ON router_decisions(provider);

        CREATE TABLE IF NOT EXISTS provider_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            task TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_actually_used TEXT NULL,
            sub_task TEXT NOT NULL,
            summary TEXT NULL,
            input_tokens INTEGER NULL,
            output_tokens INTEGER NULL,
            runtime_ms INTEGER NULL
        );

        CREATE INDEX IF NOT EXISTS idx_provider_usage_timestamp
            ON provider_usage(timestamp);
        CREATE INDEX IF NOT EXISTS idx_provider_usage_provider
            ON provider_usage(provider);
        CREATE INDEX IF NOT EXISTS idx_provider_usage_provider_actually_used
            ON provider_usage(provider_actually_used);

        CREATE TABLE IF NOT EXISTS model_consultations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            parent_task TEXT NOT NULL,
            query TEXT NOT NULL,
            reply TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_model_consultations_timestamp
            ON model_consultations(timestamp);
        """
    )
    # Additive migrations for existing databases.
    _migrate_router_decisions_columns(conn)
    _migrate_provider_usage_columns(conn)


def _migrate_router_decisions_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(router_decisions)")}
    for col, coltype in [("provider", "TEXT"), ("role", "TEXT")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE router_decisions ADD COLUMN {col} {coltype} NULL")


def _migrate_provider_usage_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(provider_usage)")}
    for col, coltype in [
        ("provider_actually_used", "TEXT"),
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("runtime_ms", "INTEGER"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE provider_usage ADD COLUMN {col} {coltype} NULL")


def _migrate_operation_log(conn: sqlite3.Connection) -> None:
    """Create operation_log table (idempotent additive migration)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS operation_log (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            operation TEXT NOT NULL,
            parameters TEXT,
            success INTEGER NOT NULL CHECK (success IN (0, 1)),
            result TEXT,
            error TEXT,
            duration_ms INTEGER,
            executor TEXT,
            invocation_id TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_operation_log_timestamp
            ON operation_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_operation_log_operation
            ON operation_log(operation);
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(operation_log)")}
    if "invocation_id" not in existing:
        conn.execute("ALTER TABLE operation_log ADD COLUMN invocation_id TEXT NULL")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_operation_log_invocation_id "
        "ON operation_log(invocation_id) WHERE invocation_id IS NOT NULL"
    )


def _migrate_benchmark_runs(conn: sqlite3.Connection) -> None:
    """Create benchmark_runs table (#343), one row per task of a benchmark run."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            task_sha256 TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            provider TEXT NULL,
            model TEXT NULL,
            prompt_name TEXT NOT NULL,
            prompt_sha256 TEXT NOT NULL,
            experiment_id TEXT NULL,
            status TEXT NOT NULL,
            score REAL NOT NULL,
            tests_green INTEGER NOT NULL CHECK (tests_green IN (0, 1)),
            expected_files_touched INTEGER NOT NULL,
            expected_files_total INTEGER NOT NULL,
            evaluation_outcome TEXT NULL,
            input_tokens INTEGER NULL,
            output_tokens INTEGER NULL,
            runtime_ms INTEGER NOT NULL,
            estimated_cost REAL NULL,
            currency TEXT NULL,
            peak_memory_bytes INTEGER NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            error TEXT NULL,
            PRIMARY KEY (run_id, task_id)
        );

        CREATE INDEX IF NOT EXISTS idx_benchmark_runs_started_at
            ON benchmark_runs(started_at);
        """
    )
