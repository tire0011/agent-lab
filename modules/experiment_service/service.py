from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from modules.codex_worker import CodexResult, CodexWorker
from modules.experiment_evaluation import parse_git_status_short
from modules.experiment_logger import DEFAULT_LOG_DIR, ExperimentLogger, sanitize_command
from modules.redaction import sanitized_reason
from modules.run_store import (
    DEFAULT_DB_PATH,
    get_setting,
    init_db,
    insert_activity,
    list_activity,
    set_setting,
)
from modules.verification_runner import run_verification_test

from .git_utils import _run_git
from .models import (
    Experiment,
    ExperimentNotFoundError,
    ExperimentNotRunnableError,
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_REJECTED,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
    STATUS_VERIFYING,
    _row_to_experiment,
)
from .policy import _check_goal_policy, _policy_rule_for_reason
from .prompts import _build_direct_implementation_prompt
from .text_utils import _truncate_text, _utcnow_iso

logger = logging.getLogger("modules.experiment_service")

# Statuses that represent a problem worth surfacing at WARNING level in the
# journal (as opposed to ordinary forward progress, logged at INFO). Kept
# as a small fixed set here so lifecycle logging in ``_transition``/
# ``_force_status`` stays centralized and consistent.
_WARNING_STATUSES = frozenset(
    {"rejected", "failed", "timed_out", "needs_review"}
)

# Subset of the above that represents an actual failure (as opposed to a
# policy rejection or a review handoff) — logged at ERROR in the
# structured per-experiment JSONL log (journald keeps the coarser
# WARNING/INFO split above for backward compatibility).
_ERROR_STATUSES = frozenset({"failed", "timed_out"})

_DEFAULT_MAX_TEST_OUTPUT_BYTES = 32 * 1024
_DEFAULT_MAX_DIFF_BYTES = 64 * 1024
_DEFAULT_MAX_RAW_MESSAGE_BYTES = 16 * 1024
_DEFAULT_MAX_DIAGNOSTIC_LOG_BYTES = 64 * 1024
_DEFAULT_RESEARCH_TIMEOUT_SECONDS = 120
_DEFAULT_PLAN_TIMEOUT_SECONDS = 90
_REFORMAT_RETRY_TIMEOUT_SECONDS = 60
_DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS = 1200  # 20 minutes

# Adaptive verification test timeout. The subprocess running the test
# command starts out bounded by ``_DEFAULT_TEST_TIMEOUT_SECONDS``; each time
# a full verification run ends with ``test_timeout`` (the suite did not
# finish in time, as opposed to a normal test failure), the timeout used for
# the *next* run is multiplied by ``_TEST_TIMEOUT_BACKOFF_FACTOR`` and capped
# at ``_MAX_TEST_TIMEOUT_SECONDS`` so a hanging test can never grow the
# timeout without bound. The learned value is persisted in the
# ``service_settings`` table (see ``modules.run_store``) so it survives
# process restarts and is reused by later verification runs, not just later
# calls on the same ``ExperimentService`` instance.
_DEFAULT_TEST_TIMEOUT_SECONDS = 300  # 5 minutes
_MAX_TEST_TIMEOUT_SECONDS = 1800  # 30 minutes
_TEST_TIMEOUT_BACKOFF_FACTOR = 1.5
_TEST_TIMEOUT_SETTING_KEY = "verification_test_timeout_seconds"

# Built-in default test command, keyed by a fixed identifier. The plan/LLM output
# never supplies a raw shell string; the service only ever chooses one of
# these fixed argv templates. "{python}" is substituted with the
# configured test interpreter (see ExperimentService.test_python_executable)
# so that worktrees without their own virtualenv can still run tests using
# the main repository's already-installed interpreter/site-packages,
# without copying a .venv or installing any package into the worktree.
_TEST_COMMANDS: dict[str, list[str]] = {
    "unittest-discover": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-q"],
}
_DEFAULT_TEST_COMMAND_KEY = "unittest-discover"

# Default interpreter used to run tests inside an experiment worktree.
# Using the main repository's own virtualenv interpreter lets a fresh git
# worktree (which has no virtualenv of its own) run the existing test
# suite without copying any files or installing any packages — the
# worktree only needs to be used as the *working directory* (``cwd``) for
# ``python -m unittest``, since Python resolves ``tests/`` relative to cwd
# while the interpreter and its installed packages come from the venv path.
_DEFAULT_TEST_PYTHON_EXECUTABLE = "/agent/.venv/bin/python"

class ExperimentService:
    """Orchestrates one bounded self-development experiment at a time.

    Reuses ``CodexWorker`` for both the read-only selection phase and the
    isolated workspace-write implementation phase; no worker/subprocess
    logic is duplicated here beyond git worktree management and the fixed
    verification commands.
    """

    def __init__(
        self,
        selection_worker: CodexWorker | None = None,
        worker_factory: Callable[[str], CodexWorker] | None = None,
        plan_worker_factory: Callable[[str], CodexWorker] | None = None,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        worktrees_root: str | None = None,
        max_test_output_bytes: int = _DEFAULT_MAX_TEST_OUTPUT_BYTES,
        max_diff_bytes: int = _DEFAULT_MAX_DIFF_BYTES,
        max_raw_message_bytes: int = _DEFAULT_MAX_RAW_MESSAGE_BYTES,
        max_diagnostic_log_bytes: int = _DEFAULT_MAX_DIAGNOSTIC_LOG_BYTES,
        test_python_executable: str = _DEFAULT_TEST_PYTHON_EXECUTABLE,
        experiment_log_dir: str | None = None,
        default_test_timeout_seconds: int = _DEFAULT_TEST_TIMEOUT_SECONDS,
        max_test_timeout_seconds: int = _MAX_TEST_TIMEOUT_SECONDS,
        test_timeout_backoff_factor: float = _TEST_TIMEOUT_BACKOFF_FACTOR,
    ) -> None:
        """
        Parameters
        ----------
        selection_worker:
            ``CodexWorker`` used for the read-only *research* phase (the
            only phase that ever investigates the live repository).
            Defaults to a real worker allowed only inside ``repo_root``.
        worker_factory:
            Callable ``(worktree_path) -> CodexWorker`` used to build the
            implementation-phase worker, scoped (legacy worker root configuration) to that
            single worktree path only. Defaults to constructing a real
            ``CodexWorker()``.
        plan_worker_factory:
            Callable ``(scratch_dir) -> CodexWorker`` used to build the
            *plan*-phase worker (and its one reformat retry), scoped
            (legacy worker root configuration) to a fresh, empty scratch directory created
            fresh for each experiment and removed afterwards. This worker
            never sees ``repo_root`` and is given only the goal plus the
            research summary as input — it cannot perform any further
            repository research even if it tried to run a shell command,
            since its working directory is empty. Defaults to constructing
            a real ``CodexWorker()``.
        repo_root:
            Root of the main repository (default ``/agent``). Never
            modified by this service.
        worktrees_root:
            Parent directory under which per-experiment worktrees (and
            plan-phase scratch directories) are created. Defaults to
            ``~/agent-worktrees`` since ``/`` itself is typically not
            writable by the agent user.
        test_python_executable:
            Path to the Python interpreter used to run the verification
            test command inside an experiment worktree. Defaults to the
            main repository's own virtualenv interpreter
            (``/agent/.venv/bin/python``) so a fresh git worktree — which
            has no virtualenv of its own — can still run the existing test
            suite: the worktree is used only as the test command's
            *working directory*, never as the source of the interpreter
            or its installed packages. No package is installed and no
            ``.venv`` is copied into the worktree.
        experiment_log_dir:
            Directory holding one durable, append-only JSONL log per
            experiment (``<experiment_log_dir>/<experiment_id>.jsonl``),
            written via ``modules.experiment_logger.ExperimentLogger``.
            Defaults to ``<repo_root>/state/experiment-logs``.
        default_test_timeout_seconds:
            Starting timeout (seconds) for the verification test subprocess
            before any automatic increase has ever been learned/persisted.
        max_test_timeout_seconds:
            Hard cap on the automatically-increased verification test
            timeout, so a hanging test can never grow the timeout without
            bound.
        test_timeout_backoff_factor:
            Multiplier applied to the current verification test timeout
            each time a full run ends with ``test_timeout``.
        """
        self.selection_worker = selection_worker if selection_worker is not None else CodexWorker(
            
        )
        self.worker_factory = worker_factory or (
            lambda worktree_path: CodexWorker()
        )
        self.plan_worker_factory = plan_worker_factory or (
            lambda scratch_dir: CodexWorker()
        )
        self.db_path = db_path
        self.repo_root = repo_root
        self.worktrees_root = worktrees_root or str(Path.home() / "agent-worktrees")
        self.max_test_output_bytes = max_test_output_bytes
        self.max_diff_bytes = max_diff_bytes
        self.max_raw_message_bytes = max_raw_message_bytes
        self.max_diagnostic_log_bytes = max_diagnostic_log_bytes
        self.test_python_executable = test_python_executable
        self.default_test_timeout_seconds = default_test_timeout_seconds
        self.max_test_timeout_seconds = max_test_timeout_seconds
        self.test_timeout_backoff_factor = test_timeout_backoff_factor
        # Deliberately *not* derived from ``repo_root``: tests (and some
        # deployments) point ``repo_root`` at a throwaway git checkout whose
        # cleanliness is asserted directly, so the log directory must never
        # live inside it (matching ``modules.run_log``'s plain
        # ``state/runs`` default, which is likewise independent of any
        # injected repo path). In production this resolves relative to the
        # service's working directory (``/agent``).
        self.experiment_log_dir = experiment_log_dir or DEFAULT_LOG_DIR

        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # adaptive verification test timeout
    # ------------------------------------------------------------------

    def _get_test_timeout_seconds(self) -> int:
        """Return the timeout (seconds) to use for the *next* verification
        test subprocess run, reusing any previously learned/persisted value
        so a suite that has already required more time does not keep
        hitting the same limit on later runs."""
        with self._lock:
            raw = get_setting(self._conn, _TEST_TIMEOUT_SETTING_KEY)
        if raw is None:
            return self.default_test_timeout_seconds
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return self.default_test_timeout_seconds
        if value <= 0:
            return self.default_test_timeout_seconds
        return value

    def _increase_test_timeout_seconds(self, current_timeout: int) -> int:
        """Persist a larger timeout after a ``test_timeout`` and return it.

        ``next_timeout = min(current_timeout * factor, max_timeout)``. The
        new value is stored in ``service_settings`` so subsequent
        verification runs (including in later processes) start from it
        instead of the original default.
        """
        next_timeout = min(
            int(current_timeout * self.test_timeout_backoff_factor),
            self.max_test_timeout_seconds,
        )
        next_timeout = max(next_timeout, current_timeout)
        with self._lock:
            set_setting(
                self._conn,
                _TEST_TIMEOUT_SETTING_KEY,
                str(next_timeout),
                updated_at=_utcnow_iso(),
            )
        return next_timeout

    # ------------------------------------------------------------------
    # create_experiment
    # ------------------------------------------------------------------

    def create_experiment(
        self,
        goal: str,
        timeout_seconds: int = _DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS,
    ) -> Experiment:
        if not goal or not goal.strip():
            raise ValueError("goal must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        experiment_id = uuid.uuid4().hex
        created_at = _utcnow_iso()

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO experiments (id, goal, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (experiment_id, goal.strip(), STATUS_QUEUED, created_at),
            )
            insert_activity(
                self._conn,
                timestamp=created_at,
                entity_type="experiment",
                entity_id=experiment_id,
                event="created",
                to_status=STATUS_QUEUED,
                message=goal.strip(),
            )

        self._implementation_timeout_seconds = timeout_seconds
        logger.info("experiment %s created (status=%s)", experiment_id, STATUS_QUEUED)
        self.get_experiment_logger(experiment_id).info(
            STATUS_QUEUED, "experiment_created",
            f"Experiment created: {goal.strip()}",
            timeout_seconds=timeout_seconds,
        )
        return self.get_experiment(experiment_id)

    # ------------------------------------------------------------------
    # get_experiment
    # ------------------------------------------------------------------

    def get_experiment(self, experiment_id: str) -> Experiment:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
        if row is None:
            raise ExperimentNotFoundError(f"no experiment with id {experiment_id!r}")
        return _row_to_experiment(row)

    def _resolve_by_prefix(self, experiment_id: str) -> Experiment:
        """Resolve an exact id or an unambiguous >=8 hex-char id prefix.

        Shared by both the public and the authenticated single-experiment
        lookups so an operator can use the same short id (e.g. copied from
        ``/activity/public`` or a log line) against either endpoint.
        """
        if re.fullmatch(r"[0-9a-f]{8,64}", experiment_id) is None:
            raise ExperimentNotFoundError("invalid experiment id")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiments WHERE id LIKE ? ORDER BY id LIMIT 2",
                (experiment_id + "%",),
            ).fetchall()
        if len(rows) != 1:
            raise ExperimentNotFoundError("experiment id is missing or ambiguous")
        return _row_to_experiment(rows[0])

    def resolve_public_experiment(self, experiment_id: str) -> Experiment:
        """Resolve an exact id or a unique public prefix of at least 8 hex chars."""
        return self._resolve_by_prefix(experiment_id)

    def resolve_experiment(self, experiment_id: str) -> Experiment:
        """Authenticated equivalent of ``resolve_public_experiment``.

        Accepts either a full experiment id or an unambiguous >=8 hex-char
        prefix, exactly like the public endpoint, so an operator holding
        only a short id (e.g. from ``/activity/public``) can still look up
        full authenticated detail without first resolving it elsewhere.
        Falls back to an exact-id lookup for ids that don't look like a
        valid hex prefix (e.g. legacy/non-hex ids), preserving the previous
        ``get_experiment`` behavior for exact matches.
        """
        if re.fullmatch(r"[0-9a-f]{8,64}", experiment_id) is not None:
            return self._resolve_by_prefix(experiment_id)
        return self.get_experiment(experiment_id)

    def list_experiments(self, limit: int = 20) -> list[Experiment]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiments ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_experiment(row) for row in rows]

    # ------------------------------------------------------------------
    # list_activity — persistent activity log (experiment lifecycle events)
    # ------------------------------------------------------------------

    def list_activity(
        self, limit: int = 50, experiment_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return recent persisted activity log entries, newest first.

        Reuses ``modules.run_store.list_activity`` against the same
        connection/lock used for all other experiment reads/writes.
        """
        with self._lock:
            return list_activity(
                self._conn,
                limit=limit,
                entity_type="experiment",
                entity_id=experiment_id,
            )

    def mailbox_task_id_for_experiment(self, experiment_id: str) -> str | None:
        """Return the approval id linked to an experiment, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM mailbox_task_approvals WHERE experiment_id = ? LIMIT 1",
                (experiment_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def get_experiment_logger(
        self, experiment_id: str, mailbox_task_id: str | None = None
    ) -> ExperimentLogger:
        """Build the (cheap, stateless) structured JSONL logger for one experiment.

        Every caller anywhere in the pipeline — this service, the mailbox
        approval bridge, or a test — goes through this single factory so
        every log entry for a given experiment lands in exactly one file
        (``<experiment_log_dir>/<experiment_id>.jsonl``), regardless of
        which thread is writing.
        """
        if mailbox_task_id is None:
            mailbox_task_id = self.mailbox_task_id_for_experiment(experiment_id)
        return ExperimentLogger(
            experiment_id,
            mailbox_task_id=mailbox_task_id,
            log_dir=self.experiment_log_dir,
        )

    def experiment_log_path(self, experiment_id: str) -> Path:
        """Path to an experiment's JSONL log file (may not exist yet)."""
        return Path(self.experiment_log_dir) / f"{experiment_id}.jsonl"

    # ------------------------------------------------------------------
    # Internal: status transition helper
    # ------------------------------------------------------------------

    def _transition(self, experiment_id: str, from_status: str, to_status: str, **fields: Any) -> bool:
        """Atomically move ``from_status`` -> ``to_status`` iff still ``from_status``.

        Returns True if the transition happened. Extra ``fields`` are set in
        the same UPDATE. Unknown/disallowed transitions are refused by the
        caller (checked via ``_STATE_TRANSITIONS`` before calling this).
        """
        set_clauses = ["status = ?"]
        values: list[Any] = [to_status]
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.extend([experiment_id, from_status])

        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"UPDATE experiments SET {', '.join(set_clauses)} "
                "WHERE id = ? AND status = ?",
                values,
            )
            moved = cursor.rowcount == 1
            if moved:
                insert_activity(
                    self._conn,
                    timestamp=_utcnow_iso(),
                    entity_type="experiment",
                    entity_id=experiment_id,
                    event="status_changed",
                    from_status=from_status,
                    to_status=to_status,
                    message=sanitized_reason(str(fields["error"])) if fields.get("error") else None,
                )
        if moved:
            level = logging.WARNING if to_status in _WARNING_STATUSES else logging.INFO
            logger.log(
                level,
                "experiment %s: %s -> %s%s",
                experiment_id,
                from_status,
                to_status,
                f" (policy_rule={fields['policy_rule']})" if fields.get("policy_rule") else "",
            )
            elog = self.get_experiment_logger(experiment_id)
            elog_method = (
                elog.error if to_status in _ERROR_STATUSES
                else elog.warning if to_status in _WARNING_STATUSES
                else elog.info
            )
            elog_method(
                to_status, "status_changed",
                f"Experiment transitioned {from_status} -> {to_status}",
                from_status=from_status,
                to_status=to_status,
                policy_rule=fields.get("policy_rule"),
                reason=sanitized_reason(str(fields["error"])) if fields.get("error") else None,
            )
        return moved

    def _force_status(self, experiment_id: str, status: str, **fields: Any) -> None:
        """Set status unconditionally (used for terminal/failure bookkeeping)."""
        set_clauses = ["status = ?"]
        values: list[Any] = [status]
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.append(experiment_id)

        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE experiments SET {', '.join(set_clauses)} WHERE id = ?",
                values,
            )
            insert_activity(
                self._conn,
                timestamp=_utcnow_iso(),
                entity_type="experiment",
                entity_id=experiment_id,
                event="status_forced",
                to_status=status,
                message=sanitized_reason(str(fields["error"])) if fields.get("error") else None,
            )
        level = logging.WARNING if status in _WARNING_STATUSES else logging.INFO
        logger.log(
            level,
            "experiment %s: forced -> %s%s",
            experiment_id,
            status,
            f" (policy_rule={fields['policy_rule']})" if fields.get("policy_rule") else "",
        )
        elog = self.get_experiment_logger(experiment_id)
        elog_method = (
            elog.error if status in _ERROR_STATUSES
            else elog.warning if status in _WARNING_STATUSES
            else elog.info
        )
        elog_method(
            status, "status_forced",
            f"Experiment status forced to {status}",
            to_status=status,
            policy_rule=fields.get("policy_rule"),
            reason=sanitized_reason(str(fields["error"])) if fields.get("error") else None,
        )

    # ------------------------------------------------------------------
    # run_experiment — the whole pipeline, single entry point
    # ------------------------------------------------------------------

    def run_experiment(self, experiment_id: str, github_work_tracking: Any | None = None) -> Experiment:
        experiment = self.get_experiment(experiment_id)  # raises if missing
        elog = self.get_experiment_logger(experiment_id)

        if experiment.status != STATUS_QUEUED:
            elog.warning(
                experiment.status, "run_experiment_rejected",
                f"run_experiment called but experiment is not queued (status={experiment.status})",
            )
            raise ExperimentNotRunnableError(
                f"experiment {experiment_id!r} cannot be started from status "
                f"{experiment.status!r} (only {STATUS_QUEUED!r} experiments can run)"
            )

        elog.info(STATUS_QUEUED, "run_experiment_started", "Experiment pipeline starting")

        moved = self._transition(
            experiment_id, STATUS_QUEUED, STATUS_IMPLEMENTING,
            implementation_started_at=_utcnow_iso(),
        )
        if not moved:
            # Raced with another start attempt; refuse rather than double-run.
            current = self.get_experiment(experiment_id)
            elog.warning(
                current.status, "run_experiment_race_lost",
                f"run_experiment lost a race; experiment already status={current.status}",
            )
            raise ExperimentNotRunnableError(
                f"experiment {experiment_id!r} cannot be started from status "
                f"{current.status!r} (only {STATUS_QUEUED!r} experiments can run)"
            )

        try:
            return self._run_pipeline(experiment_id, github_work_tracking=github_work_tracking)
        except Exception as exc:  # noqa: BLE001 - must never leave an active status
            elog.error(
                "pipeline", "unhandled_exception",
                "Unhandled exception in experiment pipeline; forcing status=failed",
                exc=exc,
            )
            self._force_status(
                experiment_id,
                STATUS_FAILED,
                ended_at=_utcnow_iso(),
                error=f"unhandled exception in experiment pipeline: {exc}",
            )
            return self.get_experiment(experiment_id)

    def _run_pipeline(self, experiment_id: str, *, github_work_tracking: Any | None = None) -> Experiment:
        """Single-call implementation pipeline.

        Phases (in order):
          1. Goal security check (deterministic, no LLM call).
          2. Isolated git worktree creation.
          3. One Codex call — the worker analyses the repo, plans, and
             implements the goal directly inside the worktree.
          4. Verification — git diff + tests + deterministic quality gate.

        The injected GitHub lifecycle publishes a checkpoint through a
        temporary index before verification; it never changes this worktree's
        HEAD/index and never merges or deploys from this service.
        Reuses ``modules.codex_worker.CodexWorker`` (no duplicated
        subprocess/worker logic) and the existing SQLite store.
        """
        experiment = self.get_experiment(experiment_id)
        elog = self.get_experiment_logger(experiment_id)

        # ---- Security check: apply safety denylist to the raw goal --------
        rejection_reason = _check_goal_policy(experiment.goal)
        if rejection_reason is not None:
            elog.warning(
                "implementing", "goal_policy_rejected",
                f"Goal rejected by safety policy before any LLM call: {rejection_reason}",
                policy_rule=_policy_rule_for_reason(rejection_reason),
            )
            self._force_status(
                experiment_id, STATUS_REJECTED,
                ended_at=_utcnow_iso(),
                error=rejection_reason,
                policy_rule=_policy_rule_for_reason(rejection_reason),
            )
            return self.get_experiment(experiment_id)

        # ---- Isolated worktree --------------------------------------------
        branch_name = f"experiment/{experiment_id}"
        worktree_path = str(Path(self.worktrees_root) / experiment_id)

        elog.debug(
            "implementation", "worktree_check",
            f"Creating isolated git worktree at branch {branch_name}",
        )
        try:
            Path(self.worktrees_root).mkdir(parents=True, exist_ok=True)
            create_proc = _run_git(
                ["worktree", "add", "-b", branch_name, worktree_path, "HEAD"],
                cwd=self.repo_root,
                timeout=60,
                elog=elog,
                phase="implementation",
            )
            if create_proc.returncode != 0:
                self._force_status(
                    experiment_id, STATUS_FAILED,
                    ended_at=_utcnow_iso(),
                    implementation_ended_at=_utcnow_iso(),
                    worktree_path=worktree_path,
                    branch_name=branch_name,
                    error=f"git worktree add failed: {create_proc.stderr.strip()[:2000]}",
                )
                return self.get_experiment(experiment_id)
        except (OSError, subprocess.SubprocessError) as exc:
            elog.error(
                "implementation", "worktree_creation_exception",
                "Exception while creating git worktree", exc=exc,
            )
            self._force_status(
                experiment_id, STATUS_FAILED,
                ended_at=_utcnow_iso(),
                implementation_ended_at=_utcnow_iso(),
                error=f"failed to create worktree: {exc}",
            )
            return self.get_experiment(experiment_id)

        elog.info(
            "implementation", "worktree_created",
            f"Git worktree created at {branch_name}",
        )
        self._force_status(
            experiment_id, STATUS_IMPLEMENTING,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

        # ---- Single Codex implementation call -----------------------------
        elog.info(
            "implementation", "implementation_started",
            "Starting implementation (single Codex call: analyse, plan, implement)",
        )
        implementation_prompt = _build_direct_implementation_prompt(experiment.goal)
        implementation_task_id = uuid.uuid4().hex
        worker = self.worker_factory(worktree_path)

        implementation_timeout = getattr(
            self, "_implementation_timeout_seconds", _DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS
        )

        implementation_result: CodexResult = worker.run(
            task=implementation_prompt,
            working_dir=worktree_path,
            sandbox="workspace-write",
            timeout_seconds=implementation_timeout,
            experiment_logger=elog,
            phase="implementation",
        )

        if implementation_result.timed_out:
            checkpoint_error = self._checkpoint_interrupted_work(
                experiment_id, worktree_path, github_work_tracking, elog
            )
            self._force_status(
                experiment_id, STATUS_TIMED_OUT,
                ended_at=_utcnow_iso(),
                implementation_ended_at=_utcnow_iso(),
                implementation_task_id=implementation_task_id,
                implementation_runtime_ms=implementation_result.runtime_ms,
                implementation_usage_json=json.dumps(implementation_result.usage)
                if implementation_result.usage else None,
                error="implementation phase timed out" + checkpoint_error,
            )
            return self.get_experiment(experiment_id)

        if implementation_result.error:
            checkpoint_error = self._checkpoint_interrupted_work(
                experiment_id, worktree_path, github_work_tracking, elog
            )
            self._force_status(
                experiment_id, STATUS_FAILED,
                ended_at=_utcnow_iso(),
                implementation_ended_at=_utcnow_iso(),
                implementation_task_id=implementation_task_id,
                implementation_runtime_ms=implementation_result.runtime_ms,
                implementation_usage_json=json.dumps(implementation_result.usage)
                if implementation_result.usage else None,
                error=implementation_result.error + checkpoint_error,
            )
            return self.get_experiment(experiment_id)

        self._transition(
            experiment_id, STATUS_IMPLEMENTING, STATUS_VERIFYING,
            implementation_ended_at=_utcnow_iso(),
            implementation_task_id=implementation_task_id,
            implementation_runtime_ms=implementation_result.runtime_ms,
            implementation_usage_json=json.dumps(implementation_result.usage)
            if implementation_result.usage else None,
        )

        # ---- Verification -------------------------------------------------
        elog.info("verifying", "verification_started", "Starting verification phase (git diff + tests)")
        return self._verify(
            experiment_id,
            worktree_path,
            elog,
            github_work_tracking=github_work_tracking,
        )

    def _checkpoint_interrupted_work(
        self,
        experiment_id: str,
        worktree_path: str,
        github_work_tracking: Any | None,
        elog: ExperimentLogger,
    ) -> str:
        if github_work_tracking is None:
            return ""
        try:
            github_work_tracking.checkpoint_experiment(experiment_id, worktree_path)
            elog.info(
                "implementation", "github_checkpoint_published",
                "Published interrupted implementation checkpoint to Draft PR",
            )
            return ""
        except Exception as exc:  # noqa: BLE001 - preserve original terminal state
            elog.error(
                "implementation", "github_checkpoint_failed",
                "Could not publish interrupted implementation checkpoint", exc=exc,
            )
            return f"; GitHub checkpoint failed: {str(exc)[:1000]}"

    def _verify(
        self,
        experiment_id: str,
        worktree_path: str,
        elog: ExperimentLogger | None = None,
        *,
        github_work_tracking: Any | None = None,
    ) -> Experiment:
        if elog is None:
            elog = self.get_experiment_logger(experiment_id)
        if github_work_tracking is not None:
            try:
                ref = github_work_tracking.checkpoint_experiment(
                    experiment_id, worktree_path
                )
                elog.info(
                    "verifying", "github_checkpoint_published",
                    f"Checkpoint published to {ref.branch_name} before verification",
                )
            except Exception as exc:  # noqa: BLE001 - never verify invisible work
                elog.error(
                    "verifying", "github_checkpoint_failed",
                    "Verification blocked because the implementation checkpoint is not visible in GitHub",
                    exc=exc,
                )
                self._force_status(
                    experiment_id,
                    STATUS_NEEDS_REVIEW,
                    ended_at=_utcnow_iso(),
                    error=f"GitHub checkpoint failed before verification: {str(exc)[:2000]}",
                )
                return self.get_experiment(experiment_id)
        try:
            status_proc = _run_git(
                ["status", "--short", "--untracked-files=all"],
                cwd=worktree_path, elog=elog, phase="verifying",
            )
            with tempfile.TemporaryDirectory(prefix="experiment-git-index-") as index_dir:
                index_env = dict(os.environ)
                index_env["GIT_INDEX_FILE"] = str(Path(index_dir) / "index")
                read_tree_proc = _run_git(
                    ["read-tree", "HEAD"], cwd=worktree_path, env=index_env, elog=elog, phase="verifying"
                )
                add_proc = _run_git(["add", "-A"], cwd=worktree_path, env=index_env, elog=elog, phase="verifying")
                if read_tree_proc.returncode != 0 or add_proc.returncode != 0:
                    failed_proc = read_tree_proc if read_tree_proc.returncode != 0 else add_proc
                    raise subprocess.CalledProcessError(
                        failed_proc.returncode,
                        failed_proc.args,
                        output=failed_proc.stdout,
                        stderr=failed_proc.stderr,
                    )
                stat_proc = _run_git(
                    ["diff", "--cached", "--stat", "HEAD"],
                    cwd=worktree_path,
                    env=index_env,
                    elog=elog, phase="verifying",
                )
                diff_proc = _run_git(
                    ["diff", "--cached", "HEAD"], cwd=worktree_path, env=index_env,
                    elog=elog, phase="verifying",
                )
                check_proc = _run_git(
                    ["diff", "--cached", "--check", "HEAD"],
                    cwd=worktree_path,
                    env=index_env,
                    elog=elog, phase="verifying",
                )
        except (OSError, subprocess.SubprocessError) as exc:
            elog.error(
                "verifying", "verification_git_exception",
                "Exception while running verification git commands", exc=exc,
            )
            self._force_status(
                experiment_id, STATUS_NEEDS_REVIEW,
                ended_at=_utcnow_iso(),
                error=f"verification git commands failed: {exc}",
            )
            return self.get_experiment(experiment_id)

        git_status = status_proc.stdout
        diff_stat = stat_proc.stdout
        diff_text = _truncate_text(diff_proc.stdout, self.max_diff_bytes, label="diff")
        diff_check_issue = check_proc.stdout.strip() or (check_proc.returncode != 0 and check_proc.stderr.strip())
        if git_status.strip():
            elog.debug("verifying", "git_status_checked", "Worktree has uncommitted changes as expected")
        else:
            elog.warning("verifying", "git_status_clean", "Worktree has no changes after implementation phase")

        test_command_key = _DEFAULT_TEST_COMMAND_KEY
        test_command_template = _TEST_COMMANDS.get(test_command_key)
        test_command = (
            [part.format(python=self.test_python_executable) for part in test_command_template]
            if test_command_template is not None
            else None
        )

        if test_command is None or not (Path(worktree_path) / "tests").is_dir():
            elog.warning(
                "verifying", "no_test_command",
                "No safe test command could be determined for this worktree",
            )
            self._force_status(
                experiment_id, STATUS_NEEDS_REVIEW,
                ended_at=_utcnow_iso(),
                git_status=git_status,
                diff_stat=diff_stat,
                diff_text=diff_text,
                error="no safe test command could be determined for this worktree",
            )
            return self.get_experiment(experiment_id)

        test_timeout_seconds = self._get_test_timeout_seconds()
        elog.info(
            "verifying", "tests_started",
            "Running verification test command",
            command=test_command, cwd=worktree_path,
            timeout_seconds=test_timeout_seconds,
        )
        test_run = run_verification_test(
            test_command,
            cwd=worktree_path,
            timeout_seconds=test_timeout_seconds,
        )
        test_exit_code = test_run.exit_code
        test_output = test_run.output
        test_timed_out = test_run.timed_out
        if test_run.timed_out:
            elog.log_process(
                "verifying", "test_timeout",
                "Test command timed out",
                command=test_command, cwd=worktree_path,
                duration_ms=test_run.duration_ms,
                timed_out=True, level="ERROR", timeout_seconds=test_timeout_seconds,
            )
            next_timeout_seconds = self._increase_test_timeout_seconds(test_timeout_seconds)
            if next_timeout_seconds > test_timeout_seconds:
                elog.warning(
                    "verifying", "test_timeout_increased",
                    f"Verification test timeout increased from {test_timeout_seconds}s to "
                    f"{next_timeout_seconds}s for future runs",
                    previous_timeout_seconds=test_timeout_seconds,
                    new_timeout_seconds=next_timeout_seconds,
                )
        elif test_run.exception is not None:
            elog.error(
                "verifying", "tests_exception",
                "Exception while running test command", exc=test_run.exception,
                command=sanitize_command(test_command), cwd=worktree_path,
            )
        else:
            elog.log_process(
                "verifying", "tests_completed",
                f"Test command finished with exit code {test_exit_code}",
                command=test_command, cwd=worktree_path,
                duration_ms=test_run.duration_ms,
                exit_code=test_exit_code, stdout=test_run.stdout, stderr=test_run.stderr,
            )

        test_output_capped = _truncate_text(test_output, self.max_test_output_bytes, label="test output")

        final_status = STATUS_SUCCEEDED
        error: str | None = None
        quality_evaluation: dict[str, Any] | None = None
        if diff_check_issue:
            final_status = STATUS_NEEDS_REVIEW
            error = f"git diff --check reported issues: {str(diff_check_issue)[:500]}"
            elog.warning("verifying", "diff_check_issue", f"git diff --check reported issues: {diff_check_issue}")
        elif test_timed_out:
            # A timed-out test run is not a normal test failure: the tests
            # never finished (exit_code=None is expected and must not be
            # mistaken for a real failing exit status). Keep this branch
            # ahead of the exit-code check below so it can never be folded
            # into "tests failed". The distinct "test_timeout" event was
            # already logged above when the TimeoutExpired was caught.
            final_status = STATUS_TIMED_OUT
            error = "tests timed out (no exit code, test command did not finish within the time limit)"
        elif test_exit_code != 0:
            final_status = STATUS_FAILED if test_exit_code is not None else STATUS_NEEDS_REVIEW
            error = f"tests failed (exit code {test_exit_code})"
            elog.error("verifying", "tests_failed", error, exit_code=test_exit_code)
        else:
            elog.info("verifying", "tests_passed", "Verification tests passed")

            # ---- Deterministic quality evaluation (no LLM call) --------
            # Tests passing is necessary but never sufficient: this checks
            # that the *actual* diff matches what the plan (and the
            # original task) required — e.g. a plan for an implementation
            # change that only touched test files must never become
            # "succeeded" just because those tests happen to pass. A
            # failure anywhere in this block (a bug in the evaluator
            # itself, an unreadable plan, ...) must never be able to turn
            # into a silent "succeeded" — it always downgrades to
            # needs_review instead, with the worktree preserved for
            # manual review.
            try:
                experiment_for_eval = self.get_experiment(experiment_id)
                changed = parse_git_status_short(git_status)
                import modules.experiment_service as experiment_service_module

                eval_result = experiment_service_module.evaluate_implementation(
                    experiment_for_eval.plan,
                    experiment_for_eval.goal,
                    changed,
                    worktree_path,
                )
                quality_evaluation = eval_result.to_dict()
                if eval_result.outcome != "pass":
                    final_status = STATUS_NEEDS_REVIEW
                    error = f"quality evaluation: {eval_result.reason}"
                    elog.warning(
                        "verifying", "quality_evaluation_failed",
                        f"Deterministic quality evaluation flagged this experiment for review: {eval_result.reason}",
                        evaluation_json=json.dumps(quality_evaluation),
                    )
                else:
                    elog.info(
                        "verifying", "quality_evaluation_passed",
                        "Deterministic quality evaluation passed",
                        evaluation_json=json.dumps(quality_evaluation),
                    )
            except Exception as exc:  # noqa: BLE001 - never let this promote to succeeded
                final_status = STATUS_NEEDS_REVIEW
                error = f"quality evaluation failed unexpectedly: {exc}"
                elog.error(
                    "verifying", "quality_evaluation_exception",
                    "Exception while running deterministic quality evaluation", exc=exc,
                )

        self._force_status(
            experiment_id, final_status,
            ended_at=_utcnow_iso(),
            git_status=git_status,
            diff_stat=diff_stat,
            diff_text=diff_text,
            test_command_json=json.dumps(test_command),
            test_exit_code=test_exit_code,
            test_output=test_output_capped,
            error=error,
            quality_evaluation_json=json.dumps(quality_evaluation) if quality_evaluation is not None else None,
        )
        if github_work_tracking is not None:
            try:
                github_work_tracking.record_verification(
                    experiment_id,
                    status=final_status,
                    command=test_command,
                    exit_code=test_exit_code,
                    error=error,
                    output=test_output_capped,
                )
            except Exception as exc:  # noqa: BLE001 - keep work and require review
                elog.error(
                    "verifying", "github_verification_update_failed",
                    "Verification completed but the Draft PR diagnostics could not be updated",
                    exc=exc,
                )
                current = self.get_experiment(experiment_id)
                combined_error = (
                    f"{current.error}; " if current.error else ""
                ) + f"GitHub verification update failed: {str(exc)[:1000]}"
                self._force_status(
                    experiment_id,
                    STATUS_NEEDS_REVIEW,
                    ended_at=_utcnow_iso(),
                    error=combined_error,
                )
        return self.get_experiment(experiment_id)

    # ------------------------------------------------------------------
    # Cleanup — explicit only, never automatic
    # ------------------------------------------------------------------

    def cleanup_worktree(self, experiment_id: str) -> None:
        """Explicitly remove an experiment's git worktree and branch.

        Never called automatically by ``run_experiment``; the worktree is
        kept after an experiment finishes so a human can inspect it. Safe to
        call multiple times.
        """
        elog = self.get_experiment_logger(experiment_id)
        experiment = self.get_experiment(experiment_id)
        elog.info("cleanup", "worktree_cleanup_started", "Cleaning up experiment worktree and branch")
        if experiment.worktree_path:
            _run_git(
                ["worktree", "remove", "--force", experiment.worktree_path],
                cwd=self.repo_root,
                elog=elog, phase="cleanup",
            )
            shutil.rmtree(experiment.worktree_path, ignore_errors=True)
        if experiment.branch_name:
            _run_git(["branch", "-D", experiment.branch_name], cwd=self.repo_root, elog=elog, phase="cleanup")
        elog.info("cleanup", "worktree_cleanup_done", "Experiment worktree and branch cleanup finished")
