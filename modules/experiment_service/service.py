from __future__ import annotations

import json
import logging
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from modules.coding_provider import (
    build_random_worker,
    provider_of,
    random_worker_factory,
)
from modules.codex_worker import CodexResult, CodexWorker
from modules.coding_worker import agent_watchdog_limits
from modules.experiment_evaluation import parse_git_status_short
from modules.central_logging import (
    DEFAULT_LOG_DIR,
    ExperimentLogger,
    _tail_read_lines,
    sanitize_command,
)
from modules.redaction import sanitized_reason
from modules.run_store import (
    DEFAULT_DB_PATH,
    get_setting,
    init_db,
    insert_activity,
    list_activity,
    set_setting,
)
from modules.test_run_log import global_heartbeat_bridge
from modules.verification_runner import (
    WatchdogConfig,
    run_verification_test_monitored,
)

from .git_utils import _run_git
from .models import (
    Experiment,
    ExperimentNotFoundError,
    ExperimentNotRunnableError,
    ExperimentNotCancellableError,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_REJECTED,
    STATUS_SELECTING,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
    STATUS_VERIFYING,
    _TERMINAL_STATUSES,
    _row_to_experiment,
)
from .policy import _check_goal_policy, _policy_rule_for_reason
from .prompts import _build_direct_implementation_prompt, _build_repair_implementation_prompt
from .text_utils import _truncate_text, _utcnow_iso

logger = logging.getLogger("modules.experiment_service")

# Statuses that represent a problem worth surfacing at WARNING level in the
# journal (as opposed to ordinary forward progress, logged at INFO). Kept
# as a small fixed set here so lifecycle logging in ``_transition``/
# ``_force_status`` stays centralized and consistent.
_WARNING_STATUSES = frozenset(
    {"rejected", "failed", "timed_out", "needs_review", "cancelled", "interrupted"}
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
# Absolute cap only (#327): the coding agent is progress-watched and stopped
# when it stalls; see modules.coding_worker.agent_watchdog_limits.
_DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS = 3 * 60 * 60
_DEFAULT_MAX_REPAIR_ITERATIONS = 2

# Error recorded on experiments whose thread died with the process (#281).
_INTERRUPTED_BY_RESTART_ERROR = "interrupted by service restart"

# Worktree retention (#376). Every experiment keeps its worktree after it ends
# so it can be inspected, retried or adopted - but a full checkout per run on a
# 1 GB VM silently exhausts the disk. Terminal experiments whose ``ended_at``
# is older than this many hours have their worktree directory removed; the
# experiment row, its log and its branch are kept. ``0`` disables cleanup, and
# so does anything unparsable or negative: a typo must not start deleting.
WORKTREE_RETENTION_ENV_VAR = "NEXUS_EXPERIMENT_WORKTREE_RETENTION_HOURS"
DEFAULT_WORKTREE_RETENTION_HOURS = 7 * 24

# Adoption statuses that still need the experiment's worktree, whatever its age.
_ADOPTION_TERMINAL_STATUSES = ("adopted", "needs_review", "failed", "interrupted")

# Stall sweep (#400). Experiment deac59fb logged ``codex_call_start`` and then
# nothing for 11 hours; neither the absolute cap nor the progress watchdog left
# a trace, and only a service restart ended it. The sweep runs periodically and
# fails an active experiment whose newest log or activity event is older than
# its phase's hard limit plus this grace. A live worker logs
# ``coding_agent_progress`` every 10 minutes, so a healthy run never gets near
# the limit.
STALL_GRACE_ENV_VAR = "NEXUS_EXPERIMENT_STALL_GRACE_SECONDS"
DEFAULT_STALL_GRACE_SECONDS = 15 * 60
_STALLED_ERROR_PREFIX = "stalled: "
_STALL_SWEEP_STATUSES = (STATUS_QUEUED, STATUS_SELECTING, STATUS_IMPLEMENTING, STATUS_VERIFYING)
# Time a stalled worker's process group gets after SIGTERM before SIGKILL.
_STALL_TERMINATE_GRACE_SECONDS = 5.0
# Only the end of an experiment log is read to find its newest entry.
_STALL_LOG_TAIL_BYTES = 64 * 1024


def stall_grace_seconds(raw: str | None = None) -> float:
    """Configured stall grace in seconds; a typo falls back to the default."""
    value = raw if raw is not None else os.environ.get(STALL_GRACE_ENV_VAR)
    if value is None or not str(value).strip():
        return float(DEFAULT_STALL_GRACE_SECONDS)
    try:
        seconds = float(str(value).strip())
    except ValueError:
        logger.warning("ignoring invalid %s value; using the default", STALL_GRACE_ENV_VAR)
        return float(DEFAULT_STALL_GRACE_SECONDS)
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return float(DEFAULT_STALL_GRACE_SECONDS)
    return seconds


def _is_stalled(status: str | None, error: str | None) -> bool:
    """Whether the stall sweep ended this experiment (#400)."""
    return status == STATUS_FAILED and str(error or "").startswith(_STALLED_ERROR_PREFIX)


def worktree_retention_hours(raw: str | None = None) -> float:
    """Configured worktree retention in hours; ``0`` disables cleanup."""
    value = raw if raw is not None else os.environ.get(WORKTREE_RETENTION_ENV_VAR)
    if value is None or not str(value).strip():
        return float(DEFAULT_WORKTREE_RETENTION_HOURS)
    try:
        hours = float(str(value).strip())
    except ValueError:
        logger.warning("ignoring invalid %s value; worktree cleanup disabled", WORKTREE_RETENTION_ENV_VAR)
        return 0.0
    return hours if hours > 0 and hours != float("inf") else 0.0


def _parse_utc(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class WorktreeCleanupResult:
    """What one worktree retention pass did, for logging and tests."""

    retention_hours: float
    removed: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

# Progress-aware local verification (#209). The verification subprocess is no
# longer bounded by a fixed wall-clock timeout, adaptive or not: a wall-clock
# limit cannot tell a healthy-but-slow suite from a stuck test, which is how
# experiment df97d67bc08e45b4bc5c573db0c84d85 came to be reported as
# ``timed_out`` with no exit code and nothing to diagnose. Instead the run is
# watched for *progress* (see ``modules.verification_runner``): it continues
# for as long as tests keep starting and finishing, and is aborted when they
# stop. The absolute cap below survives only as a final guard against a
# permanently orphaned process, deliberately far too large to be the normal
# mechanism for deciding whether a run is failing.
#
# The total test count the suite last reported is persisted in
# ``service_settings`` (see ``modules.run_store``), so a run in progress can
# report "842 / 1873" rather than a bare count from its very first minute.
# The watchdog's own numbers (and their environment variables and floors)
# live with the watchdog in ``modules.verification_runner``; leaving these
# unset here is what lets an operator tune a deployment through the
# environment without editing code.
_TEST_TOTAL_SETTING_KEY = "verification_test_total"

# Built-in default test command, keyed by a fixed identifier. The plan/LLM output
# never supplies a raw shell string; the service only ever chooses one of
# these fixed argv templates. "{python}" is substituted with the
# configured test interpreter (see ExperimentService.test_python_executable)
# so that worktrees without their own virtualenv can still run tests using
# the main repository's already-installed interpreter/site-packages,
# without copying a .venv or installing any package into the worktree.
# ``-v`` rather than ``-q``: the verbose stream is the only in-band signal
# that says which test is running right now, and the progress watchdog is
# built on it. It does not make the persisted test output any larger - the
# runner strips the per-test "... ok" lines back out again (see
# ``modules.test_progress.UnittestStreamParser``), so what reaches
# ``test_output`` (and its head-first truncation, and the failure tail the UI
# shows) stays what ``-q`` produced, plus a one-liner per failing test.
_TEST_COMMANDS: dict[str, list[str]] = {
    "unittest-discover": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"],
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

    Reuses the external coding-agent workers (``CodexWorker`` /
    ``ClaudeWorker``) for both the read-only selection phase and the isolated
    workspace-write implementation phase; no worker/subprocess logic is
    duplicated here beyond git worktree management and the fixed verification
    commands.

    Which agent runs an experiment is selected by a persistent rotation (see
    ``modules.coding_provider``) unless the caller injects its own worker or
    factory. The selected provider is recorded on the experiment as
    ``implementation_provider``.
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
        test_stall_timeout_seconds: float | None = None,
        test_watchdog_interval_seconds: float | None = None,
        test_safety_cap_seconds: float | None = None,
        max_repair_iterations: int = _DEFAULT_MAX_REPAIR_ITERATIONS,
        rng: Any | None = None,
    ) -> None:
        """
        Parameters
        ----------
        rng:
            Deprecated compatibility argument. Provider selection is now a
            deterministic, persistent rotation. Ignored when factories are injected.
        selection_worker:
            ``CodexWorker`` used for the read-only *research* phase (the
            only phase that ever investigates the live repository).
            Defaults to a real worker allowed only inside ``repo_root``.
        worker_factory:
            Callable ``(worktree_path) -> CodexWorker`` used to build the
            implementation-phase worker, scoped (legacy worker root configuration) to that
            single worktree path only. Defaults to a factory that selects the
            next coding agent once per worktree
            (``modules.coding_provider.random_worker_factory``).
        plan_worker_factory:
            Callable ``(scratch_dir) -> CodexWorker`` used to build the
            *plan*-phase worker (and its one reformat retry), scoped
            (legacy worker root configuration) to a fresh, empty scratch directory created
            fresh for each experiment and removed afterwards. This worker
            never sees ``repo_root`` and is given only the goal plus the
            research summary as input — it cannot perform any further
            repository research even if it tried to run a shell command,
            since its working directory is empty. Defaults to a factory that
            selects the next coding agent once per scratch directory.
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
            written via ``modules.central_logging.ExperimentLogger``.
            Defaults to ``<repo_root>/state/experiment-logs``.
        test_stall_timeout_seconds:
            How long a local verification run may go without a single test
            starting or finishing before the watchdog calls it stalled and
            aborts it. This - not total runtime - is what decides whether a
            run is failing. ``None`` (the default) resolves it from
            ``NEXUS_TEST_STALL_SECONDS``, or the watchdog's own default.
        test_watchdog_interval_seconds:
            How often the watchdog checks progress, and therefore how often
            the test heartbeat reaches this experiment's log. ``None``
            resolves it from ``NEXUS_TEST_WATCHDOG_INTERVAL_SECONDS``.
        test_safety_cap_seconds:
            Absolute ceiling on one local verification run, as a final guard
            against a permanently orphaned process. Deliberately large; it
            must never be the mechanism that ends a healthy run. ``None``
            resolves it from ``NEXUS_TEST_SAFETY_CAP_SECONDS``.

            All three are passed through as written when set explicitly, and
            clamped to safe floors when they come from the environment - see
            ``WatchdogConfig.resolve``.
        """
        self.selection_worker = (
            selection_worker
            if selection_worker is not None
            else build_random_worker(db_path=db_path)[1]
        )
        # One selection per worktree/scratch directory, i.e. one agent per
        # experiment: the implementation call and any repair iteration run on
        # the same provider, while the next experiment rotates again.
        self.worker_factory = worker_factory or random_worker_factory(db_path=db_path, rng=rng)
        self.plan_worker_factory = plan_worker_factory or random_worker_factory(db_path=db_path, rng=rng)
        self.db_path = db_path
        self.repo_root = repo_root
        self.worktrees_root = worktrees_root or str(Path.home() / "agent-worktrees")
        self.max_test_output_bytes = max_test_output_bytes
        self.max_diff_bytes = max_diff_bytes
        self.max_raw_message_bytes = max_raw_message_bytes
        self.max_diagnostic_log_bytes = max_diagnostic_log_bytes
        self.test_python_executable = test_python_executable
        self.test_stall_timeout_seconds = test_stall_timeout_seconds
        self.test_watchdog_interval_seconds = test_watchdog_interval_seconds
        self.test_safety_cap_seconds = test_safety_cap_seconds
        self.max_repair_iterations = max(0, max_repair_iterations)
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
        self._active_processes: dict[str, subprocess.Popen] = {}
        self._process_lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def _track_process(self, experiment_id: str, process: subprocess.Popen | None) -> None:
        with self._process_lock:
            if process is None:
                self._active_processes.pop(experiment_id, None)
            else:
                self._active_processes[experiment_id] = process
        # Workers run in their own process group (start_new_session). The id is
        # persisted so a stall sweep in another process can stop it (#400).
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE experiments SET worker_pgid = ? WHERE id = ?",
                (process.pid if process is not None else None, experiment_id),
            )
        # Close the narrow race where cancellation (or the stall sweep)
        # commits after the caller starts a phase but before that phase
        # publishes its Popen handle.
        if process is not None and self._ended_from_outside(experiment_id):
            self._terminate_process(process)

    def _ended_from_outside(self, experiment_id: str) -> bool:
        """Cancelled by an operator or failed by the stall sweep while running.

        The pipeline thread checks this after every blocking step and stops
        instead of overwriting the outcome.
        """
        experiment = self.get_experiment(experiment_id)
        return experiment.status == STATUS_CANCELLED or _is_stalled(experiment.status, experiment.error)

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=2)

    def cancel_experiment(self, experiment_id: str, cancelled_by: str) -> Experiment:
        """Cancel an active implementation or verification and retain its worktree."""
        experiment = self.resolve_experiment(experiment_id)
        actor = (cancelled_by or "api").strip()[:200] or "api"
        now = _utcnow_iso()
        moved = False
        for status in (STATUS_IMPLEMENTING, STATUS_VERIFYING):
            if self._transition(
                experiment.id, status, STATUS_CANCELLED,
                ended_at=now, cancelled_at=now, cancelled_by=actor,
                error=f"cancelled by {actor}",
            ):
                moved = True
                break
        if not moved:
            current = self.get_experiment(experiment.id)
            raise ExperimentNotCancellableError(
                f"experiment {experiment.id!r} cannot be cancelled from status {current.status!r}"
            )

        with self._process_lock:
            process = self._active_processes.get(experiment.id)
        if process is not None:
            self._terminate_process(process)
        return self.get_experiment(experiment.id)

    # ------------------------------------------------------------------
    # verification test watchdog (#209)
    # ------------------------------------------------------------------

    def _get_test_watchdog_config(self) -> WatchdogConfig:
        """Progress rules for the next local verification run.

        Whatever this service was constructed with wins; anything left unset
        falls through to the environment (clamped) and then to the watchdog's
        defaults.
        """
        return WatchdogConfig.resolve(
            stall_timeout_seconds=self.test_stall_timeout_seconds,
            check_interval_seconds=self.test_watchdog_interval_seconds,
            safety_cap_seconds=self.test_safety_cap_seconds,
        )

    def _get_expected_test_total(self) -> int | None:
        """How many tests the suite reported last time, if it ever finished.

        Only used to make progress readable ("842 / 1873"); a wrong or
        missing value costs nothing, so every failure here is simply "we do
        not know the total yet".
        """
        with self._lock:
            raw = get_setting(self._conn, _TEST_TOTAL_SETTING_KEY)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _remember_test_total(self, total: int | None) -> None:
        """Persist the total a completed run reported, for the next run."""
        if not total or total <= 0:
            return
        with self._lock:
            set_setting(
                self._conn,
                _TEST_TOTAL_SETTING_KEY,
                str(int(total)),
                updated_at=_utcnow_iso(),
            )

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

    def list_verification_runs(self, experiment_id: str) -> list[dict[str, Any]]:
        """Return every persisted verification attempt for an experiment."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT iteration, started_at, ended_at, status, commit_sha,
                          command_json, exit_code, output, error,
                          failure_fingerprint
                   FROM verification_runs
                   WHERE experiment_id = ?
                   ORDER BY iteration""",
                (experiment_id,),
            ).fetchall()
        return [
            {
                "iteration": row[0],
                "started_at": row[1],
                "ended_at": row[2],
                "status": row[3],
                "commit_sha": row[4],
                "command": json.loads(row[5]) if row[5] else None,
                "exit_code": row[6],
                "output": row[7],
                "error": row[8],
                "failure_fingerprint": row[9],
            }
            for row in rows
        ]

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
            current = self._conn.execute(
                "SELECT status, error FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            if current is not None and current[0] == STATUS_CANCELLED and status != STATUS_CANCELLED:
                return
            # The pipeline thread of a stalled experiment may still wake up
            # once its worker is stopped; the stall verdict stands (#400).
            if current is not None and _is_stalled(current[0], current[1]):
                return
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
    # mark_interrupted_experiments_failed — startup recovery (#281, #371)
    # ------------------------------------------------------------------

    def mark_interrupted_experiments_failed(self) -> list[str]:
        """Reconcile experiments orphaned by a service restart.

        An experiment runs in a daemon thread of the ``agent-api`` process, so
        a restart kills it mid-flight and leaves its row in ``implementing`` or
        ``verifying`` forever (#281). Intended to be called once at process
        startup, before any new experiment is run: at that moment no thread in
        this process can still own an active experiment.

        Never resumes the work. Moving the experiment to a terminal status is
        what lets the waiting Nexus session resume (see
        ``MailboxTaskService.resume_finished_experiments``) and decide whether
        the task continues or ends.

        Implementation and verification runs become the distinct terminal
        ``interrupted`` state. The legacy ``selecting`` phase still becomes
        ``failed`` so old persisted rows retain their previous recovery
        behaviour. Worktrees and their database references are deliberately
        retained for inspection or retry.

        Returns the list of experiment ids that were reconciled.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, status FROM experiments WHERE status IN (?, ?, ?)",
                (STATUS_SELECTING, STATUS_IMPLEMENTING, STATUS_VERIFYING),
            ).fetchall()
        marked: list[str] = []
        for row in rows:
            # Conditional per row: a status that moved on in between is left alone.
            terminal_status = (
                STATUS_FAILED if row["status"] == STATUS_SELECTING else STATUS_INTERRUPTED
            )
            if self._transition(
                row["id"], row["status"], terminal_status,
                ended_at=_utcnow_iso(),
                error=_INTERRUPTED_BY_RESTART_ERROR,
            ):
                # A newly constructed service has no live handles, but clearing
                # the registry here also makes explicit/in-process startup
                # reconciliation safe and prevents stale cancellation targets.
                with self._process_lock:
                    self._active_processes.pop(row["id"], None)
                marked.append(row["id"])
        return marked

    # ------------------------------------------------------------------
    # sweep_stalled_experiments — periodic stall detection (#400)
    # ------------------------------------------------------------------

    def _phase_hard_limit_seconds(self, status: str) -> float:
        """Longest a phase may legitimately go without a single log entry.

        Verification runs the test suite under its own safety cap and may then
        start a repair agent, so it gets the larger of both.
        """
        # The implementation timeout is the phase's upper bound, not a floor.
        # A deliberately shorter coding-agent cap must make a silent phase
        # eligible for sweeping sooner, while an absent or oversized setting
        # retains the three-hour implementation maximum.
        agent_cap = min(agent_watchdog_limits()[1], float(_DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS))
        if status == STATUS_VERIFYING:
            return max(agent_cap, float(self._get_test_watchdog_config().safety_cap_seconds))
        return agent_cap

    def _newest_log_entry(self, experiment_id: str) -> tuple[datetime | None, str]:
        """Timestamp and event of the newest entry in the experiment's JSONL log."""
        newest: datetime | None = None
        event = ""
        # A rotation leaves the active file briefly empty; the previous one
        # then holds the newest entry.
        for name in (f"{experiment_id}.jsonl", f"{experiment_id}-1.jsonl"):
            path = Path(self.experiment_log_dir) / name
            try:
                lines = _tail_read_lines(path, _STALL_LOG_TAIL_BYTES)
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                stamp = _parse_utc(entry.get("timestamp"))
                if stamp is not None and (newest is None or stamp >= newest):
                    newest, event = stamp, str(entry.get("event") or "")
            if newest is not None:
                break
        return newest, event

    def _last_progress(self, experiment_id: str, row: sqlite3.Row) -> tuple[datetime | None, str]:
        """Newest sign of life: log entry, activity event or lifecycle stamp."""
        seen: list[tuple[datetime, str]] = []
        log_at, log_event = self._newest_log_entry(experiment_id)
        if log_at is not None:
            seen.append((log_at, log_event or "log_entry"))
        with self._lock:
            activity = self._conn.execute(
                "SELECT timestamp, event FROM activity_log "
                "WHERE entity_type = 'experiment' AND entity_id = ? ORDER BY id DESC LIMIT 1",
                (experiment_id,),
            ).fetchone()
        if activity is not None:
            stamp = _parse_utc(activity[0])
            if stamp is not None:
                seen.append((stamp, f"activity:{activity[1]}"))
        for column in ("created_at", "implementation_started_at", "implementation_ended_at"):
            stamp = _parse_utc(row[column])
            if stamp is not None:
                seen.append((stamp, column))
        if not seen:
            return None, ""
        return max(seen, key=lambda item: item[0])

    def _stop_stalled_worker(self, experiment_id: str, pgid: int | None) -> str:
        """Stop the worker's process group; returns what happened, never raises."""
        with self._process_lock:
            process = self._active_processes.pop(experiment_id, None)
        if process is not None:
            try:
                self._terminate_process(process)
            except (OSError, subprocess.SubprocessError) as exc:
                return f"terminate failed: {type(exc).__name__}"
            return "terminated"
        if not pgid:
            return "no worker recorded"
        pgid = int(pgid)
        # Never signal this process's own group, init or anything below it.
        if pgid <= 1 or pgid == os.getpgrp():
            return "refused to signal own process group"
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return "worker no longer running"
        except PermissionError:
            return "not permitted to signal worker"
        deadline = time.monotonic() + _STALL_TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return "terminated"
            time.sleep(0.1)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return "terminated"

    def sweep_stalled_experiments(self, now: datetime | None = None) -> list[str]:
        """Fail active experiments that logged nothing past their phase limit.

        Startup reconciliation (#371) only helps when the process restarts; a
        worker that hangs silently in a live process kept its experiment
        ``implementing`` for 11 hours (#400), blocking the issue, the daily
        budget and the retry logic. An experiment whose newest experiment log
        entry or activity event is older than the phase's hard limit plus the
        grace (``NEXUS_EXPERIMENT_STALL_GRACE_SECONDS``) is moved to
        ``failed`` with a ``stalled:`` error, and its worker's process group,
        if still alive, is stopped. Anything that logged within the limit is
        never touched. Idempotent: a swept experiment is terminal.

        Returns the ids of the experiments marked stalled.
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        grace = stall_grace_seconds()
        placeholders = ", ".join("?" * len(_STALL_SWEEP_STATUSES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM experiments WHERE status IN ({placeholders})",
                _STALL_SWEEP_STATUSES,
            ).fetchall()
        stalled: list[str] = []
        for row in rows:
            experiment_id, phase = row["id"], row["status"]
            limit = self._phase_hard_limit_seconds(phase)
            last_at, last_event = self._last_progress(experiment_id, row)
            if last_at is None:
                continue  # nothing to judge by; never guess
            silent = (now - last_at).total_seconds()
            if silent <= limit + grace:
                continue
            error = f"{_STALLED_ERROR_PREFIX}no progress for {int(silent)}s in phase {phase}"
            # Claim the row first, conditionally: an experiment that moved on
            # in between is left alone, and once it is failed the pipeline
            # thread woken by the stopped worker cannot overwrite the verdict.
            if not self._transition(
                experiment_id, phase, STATUS_FAILED,
                ended_at=now.isoformat(), error=error, worker_pgid=None,
            ):
                continue
            worker = self._stop_stalled_worker(experiment_id, row["worker_pgid"])
            self.get_experiment_logger(experiment_id).error(
                phase, "stalled_detected",
                f"No progress for {int(silent)}s in phase {phase} "
                f"(limit {int(limit)}s + grace {int(grace)}s); last event "
                f"{last_event or 'unknown'} at {last_at.isoformat()}; worker: {worker}",
                last_event=last_event,
                last_event_at=last_at.isoformat(),
                silent_seconds=int(silent),
                limit_seconds=int(limit),
                grace_seconds=int(grace),
                worker=worker,
            )
            logger.warning(
                "experiment %s stalled in phase %s: no progress for %ds (last event %s at %s); worker: %s",
                experiment_id, phase, int(silent), last_event, last_at.isoformat(), worker,
            )
            stalled.append(experiment_id)
        return stalled

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

        phase = {"name": "pipeline"}
        try:
            return self._run_pipeline(
                experiment_id,
                github_work_tracking=github_work_tracking,
                phase=phase,
            )
        except Exception as exc:  # noqa: BLE001 - must never leave an active status
            failure_phase = phase["name"]
            failure_type = type(exc).__name__
            elog.error(
                failure_phase, "unhandled_exception",
                "Unhandled exception in experiment pipeline; forcing status=failed",
                exc=exc, failure_phase=failure_phase, failure_type=failure_type,
            )
            self._force_status(
                experiment_id,
                STATUS_FAILED,
                ended_at=_utcnow_iso(),
                failure_phase=failure_phase,
                failure_type=failure_type,
                error=f"unhandled {failure_type} in phase {failure_phase}: {exc}",
            )
            return self.get_experiment(experiment_id)

    def _run_pipeline(
        self,
        experiment_id: str,
        *,
        github_work_tracking: Any | None = None,
        phase: dict[str, str] | None = None,
    ) -> Experiment:
        """Single-call implementation pipeline.

        Phases (in order):
          1. Goal security check (deterministic, no LLM call).
          2. Isolated git worktree creation.
          3. One coding-agent call (Codex or Claude, selected per experiment) —
             the worker analyses the repo, plans, and implements the goal
             directly inside the worktree.
          4. Verification — git diff + tests + deterministic quality gate.

        The injected GitHub lifecycle publishes a checkpoint through a
        temporary index before verification; it never changes this worktree's
        HEAD/index and never merges or deploys from this service.
        Reuses the external coding-agent workers (no duplicated
        subprocess/worker logic) and the existing SQLite store.
        """
        experiment = self.get_experiment(experiment_id)
        elog = self.get_experiment_logger(experiment_id)
        phase = phase if phase is not None else {"name": "pipeline"}

        # ---- Security check: apply safety denylist to the raw goal --------
        phase["name"] = "goal_policy"
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
        base_ref = "HEAD"
        if github_work_tracking is not None:
            phase["name"] = "github_branch"
            base_ref = github_work_tracking.experiment_base_sha(experiment_id)

        phase["name"] = "worktree"
        elog.debug(
            "implementation", "worktree_check",
            f"Creating isolated git worktree at branch {branch_name} from {base_ref}",
        )
        try:
            Path(self.worktrees_root).mkdir(parents=True, exist_ok=True)
            create_proc = _run_git(
                ["worktree", "add", "-b", branch_name, worktree_path, base_ref],
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
        phase["name"] = "implementation"
        elog.info(
            "implementation", "implementation_started",
            "Starting implementation (single coding-agent call: analyse, plan, implement)",
        )
        implementation_prompt = _build_direct_implementation_prompt(experiment.goal)
        implementation_task_id = uuid.uuid4().hex
        worker = self.worker_factory(worktree_path)
        # Which coding agent got this experiment is recorded before the call,
        # so a timeout or a crash still leaves the run attributable.
        implementation_provider = provider_of(worker)
        self._force_status(
            experiment_id, STATUS_IMPLEMENTING,
            implementation_provider=implementation_provider,
        )
        elog.info(
            "implementation", "implementation_provider_selected",
            f"Implementation runs on coding agent {implementation_provider or 'unknown'}",
            provider=implementation_provider or "",
        )

        stall_seconds, max_seconds = agent_watchdog_limits()

        implementation_result: CodexResult = worker.run(
            task=implementation_prompt,
            working_dir=worktree_path,
            sandbox="workspace-write",
            timeout_seconds=max_seconds,
            stall_seconds=stall_seconds,
            experiment_logger=elog,
            phase="implementation",
            on_process=lambda process: self._track_process(experiment_id, process),
        )

        if self._ended_from_outside(experiment_id):
            return self.get_experiment(experiment_id)

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
                error="implementation phase timed out"
                + (f" ({implementation_result.error})" if implementation_result.error else "")
                + checkpoint_error,
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

        if self._ended_from_outside(experiment_id):
            return self.get_experiment(experiment_id)

        # ---- Verification -------------------------------------------------
        phase["name"] = "quality_gate"
        elog.info("verifying", "verification_started", "Starting verification phase (git diff + tests)")
        result = self._verify(
            experiment_id,
            worktree_path,
            elog,
            github_work_tracking=github_work_tracking,
        )
        return self._repair_failed_verification(
            experiment_id, worktree_path, elog, result,
            github_work_tracking=github_work_tracking,
            phase=phase,
        )

    def _record_verification_run(
        self, experiment_id: str, iteration: int, status: str,
        command: list[str] | None, exit_code: int | None, output: str | None,
        error: str | None, commit_sha: str | None = None,
    ) -> None:
        import hashlib

        fingerprint = hashlib.sha256(
            f"{status}\n{exit_code}\n{error or ''}\n{output or ''}".encode()
        ).hexdigest()
        now = _utcnow_iso()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO verification_runs
                   (experiment_id, iteration, started_at, ended_at, status,
                    commit_sha, command_json, exit_code, output, error,
                    failure_fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (experiment_id, iteration, now, now, status, commit_sha,
                 json.dumps(command) if command else None, exit_code,
                 output, error, fingerprint),
            )

    def _repair_failed_verification(
        self, experiment_id: str, worktree_path: str, elog: ExperimentLogger,
        result: Experiment, *, github_work_tracking: Any | None = None,
        phase: dict[str, str] | None = None,
    ) -> Experiment:
        """Give actionable test failures a small number of focused repair attempts."""
        phase = phase if phase is not None else {"name": "quality_gate"}
        if result.status != STATUS_FAILED or result.test_exit_code is None:
            return result

        for repair_iteration in range(1, self.max_repair_iterations + 1):
            with self._lock:
                rows = self._conn.execute(
                    "SELECT failure_fingerprint FROM verification_runs "
                    "WHERE experiment_id = ? ORDER BY iteration",
                    (experiment_id,),
                ).fetchall()
                previous_fingerprints = [row[0] for row in rows[:-1] if row[0]]
            feedback = _truncate_text(
                f"Verification status: {result.status}\n"
                f"Exit code: {result.test_exit_code}\n"
                f"Failure: {result.error or 'tests failed'}\n"
                f"Output:\n{result.test_output or '(no output)'}",
                12000,
                label="verification feedback",
            )
            import hashlib
            current_fingerprint = hashlib.sha256(
                f"{result.status}\n{result.test_exit_code}\n{result.error or ''}\n"
                f"{result.test_output or ''}".encode()
            ).hexdigest()
            if current_fingerprint in previous_fingerprints:
                elog.warning(
                    "verifying", "repair_repeated_failure",
                    "Stopping repair loop because the verification failure repeated",
                    iteration=repair_iteration,
                )
                break

            worker = self.worker_factory(worktree_path)
            self._force_status(
                experiment_id, STATUS_VERIFYING,
                implementation_provider=provider_of(worker),
            )
            elog.warning(
                "implementing", "repair_started",
                f"Starting focused repair iteration {repair_iteration}/"
                f"{self.max_repair_iterations}",
                iteration=repair_iteration,
                provider=provider_of(worker) or "",
            )
            # Update the shared pipeline phase before invoking the worker so
            # an exception raised by the repair agent is attributed to the
            # existing implementation_repair elog phase.
            phase["name"] = "implementation_repair"
            repair_result = worker.run(
                task=_build_repair_implementation_prompt(result.goal, feedback),
                working_dir=worktree_path,
                sandbox="workspace-write",
                timeout_seconds=agent_watchdog_limits()[1],
                stall_seconds=agent_watchdog_limits()[0],
                experiment_logger=elog,
                phase="implementation_repair",
                on_process=lambda process: self._track_process(experiment_id, process),
            )
            if self._ended_from_outside(experiment_id):
                return self.get_experiment(experiment_id)
            if repair_result.timed_out or repair_result.error:
                self._force_status(
                    experiment_id,
                    STATUS_TIMED_OUT if repair_result.timed_out else STATUS_FAILED,
                    ended_at=_utcnow_iso(),
                    error=repair_result.error or "repair phase timed out",
                )
                return self.get_experiment(experiment_id)

            phase["name"] = "quality_gate"
            result = self._verify(
                experiment_id, worktree_path, elog,
                github_work_tracking=github_work_tracking,
                repair_iteration=repair_iteration,
            )
            if result.status != STATUS_FAILED:
                return result

        final = self.get_experiment(experiment_id)
        if final.status == STATUS_FAILED:
            elog.error(
                "verifying", "repair_exhausted",
                "Verification remained failed after bounded repair attempts",
                max_repair_iterations=self.max_repair_iterations,
            )
        return final

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
        repair_iteration: int = 0,
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

        ci_verification = None
        if github_work_tracking is not None:
            elog.info(
                "verifying", "github_ci_wait_started",
                "Waiting for GitHub Actions verification of the exact checkpoint commit",
                checkpoint_sha=ref.checkpoint_sha,
            )
            try:
                ci_verification = github_work_tracking.wait_for_ci(experiment_id)
            except Exception as exc:  # noqa: BLE001 - local verification is the fallback
                elog.warning(
                    "verifying", "github_ci_unavailable",
                    "GitHub Actions verification lookup failed; using local fallback",
                    reason=str(exc)[:1000],
                )
            else:
                event = {
                    "passed": "github_ci_passed",
                    "failed": "github_ci_failed",
                    "unavailable": "github_ci_unavailable",
                }.get(ci_verification.status, "github_ci_unavailable")
                level = {
                    "passed": "info",
                    "failed": "error",
                }.get(ci_verification.status, "warning")
                getattr(elog, level)(
                    "verifying", event,
                    "GitHub Actions verification completed"
                    if ci_verification.status in {"passed", "failed"}
                    else "GitHub Actions verification unavailable; using local fallback",
                    checkpoint_sha=ci_verification.commit_sha,
                    conclusion=ci_verification.conclusion,
                    workflow_run_id=ci_verification.run_id,
                    workflow_url=ci_verification.url,
                    reason=ci_verification.reason,
                )
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

        use_local_tests = ci_verification is None or ci_verification.status == "unavailable"
        if use_local_tests and (test_command is None or not (Path(worktree_path) / "tests").is_dir()):
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

        test_timed_out = False
        if not use_local_tests:
            test_command = ["GitHub Actions", "tests.yml", ci_verification.commit_sha]
            test_exit_code = 0 if ci_verification.status == "passed" else 1
            test_output = (
                f"GitHub Actions run {ci_verification.run_id or 'unknown'} "
                f"for commit {ci_verification.commit_sha} completed with conclusion "
                f"{ci_verification.conclusion or 'unknown'}."
                + (f" {ci_verification.url}" if ci_verification.url else "")
            )
        else:
            watchdog = self._get_test_watchdog_config()
            expected_total = self._get_expected_test_total()
            elog.info(
                "verifying", "local_tests_fallback_started",
                "Running local verification because matching GitHub CI was unavailable",
                command=test_command, cwd=worktree_path,
                stall_timeout_seconds=watchdog.stall_timeout_seconds,
                safety_cap_seconds=watchdog.safety_cap_seconds,
            )
            elog.info(
                "verifying", "tests_started",
                "Running verification test command under the progress watchdog",
                command=test_command, cwd=worktree_path,
                stall_timeout_seconds=watchdog.stall_timeout_seconds,
                watchdog_interval_seconds=watchdog.check_interval_seconds,
                safety_cap_seconds=watchdog.safety_cap_seconds,
                expected_total_tests=expected_total,
            )
            test_run = run_verification_test_monitored(
                test_command,
                cwd=worktree_path,
                run_id=experiment_id,
                config=watchdog,
                expected_total=expected_total,
                # Publishing the test heartbeat into this experiment's own log
                # is what keeps the global Nexus heartbeat alive while a long
                # suite runs; the per-test entries stay in the dedicated test
                # log rather than flooding this one.
                on_heartbeat=global_heartbeat_bridge(elog, "verifying"),
                on_process=lambda process: self._track_process(experiment_id, process),
            )
            if self._ended_from_outside(experiment_id):
                return self.get_experiment(experiment_id)
            test_exit_code = test_run.exit_code
            test_output = test_run.output
            test_timed_out = test_run.timed_out
            self._remember_test_total(test_run.tests_total)

        if use_local_tests and test_run.timed_out:
            elog.log_process(
                "verifying",
                "test_stalled" if test_run.stalled else "test_safety_cap_exceeded",
                test_run.stall_diagnostics or "Test run aborted by the progress watchdog",
                command=test_command, cwd=worktree_path,
                duration_ms=test_run.duration_ms,
                timed_out=True, level="ERROR",
                stall_timeout_seconds=watchdog.stall_timeout_seconds,
                safety_cap_seconds=watchdog.safety_cap_seconds,
                test_log_path=test_run.test_log_path,
                **(test_run.progress or {}),
            )
        elif use_local_tests and test_run.process_died:
            elog.log_process(
                "verifying", "test_process_died",
                test_run.stall_diagnostics or "Test process was killed before it could finish",
                command=test_command, cwd=worktree_path,
                duration_ms=test_run.duration_ms,
                level="ERROR",
                test_log_path=test_run.test_log_path,
                **(test_run.progress or {}),
            )
        elif use_local_tests and test_run.exception is not None:
            elog.error(
                "verifying", "tests_exception",
                "Exception while running test command", exc=test_run.exception,
                command=sanitize_command(test_command), cwd=worktree_path,
            )
        elif use_local_tests:
            elog.log_process(
                "verifying", "tests_completed",
                f"Test command finished with exit code {test_exit_code}",
                command=test_command, cwd=worktree_path,
                duration_ms=test_run.duration_ms,
                exit_code=test_exit_code, stdout=test_run.stdout, stderr=test_run.stderr,
                test_log_path=test_run.test_log_path,
                tests_completed=(test_run.progress or {}).get("tests_completed"),
                tests_total=test_run.tests_total,
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
            # An aborted test run is not a normal test failure: the tests
            # never finished (exit_code=None is expected and must not be
            # mistaken for a real failing exit status). Keep this branch
            # ahead of the exit-code check below so it can never be folded
            # into "tests failed". The distinct "test_stalled" /
            # "test_safety_cap_exceeded" event was already logged above,
            # with the diagnostics naming the test progress stopped on.
            final_status = STATUS_TIMED_OUT
            if use_local_tests and test_run.stalled:
                error = (
                    "tests stalled (no exit code, no test started or finished for "
                    f"{int(watchdog.stall_timeout_seconds)}s)"
                )
            elif use_local_tests and test_run.safety_cap_exceeded:
                error = (
                    "tests hit the absolute safety cap (no exit code, run exceeded "
                    f"{int(watchdog.safety_cap_seconds)}s)"
                )
            else:
                error = "tests timed out (no exit code, test command did not finish within the time limit)"
            if use_local_tests and test_run.stall_diagnostics:
                error = f"{error}\n{test_run.stall_diagnostics}"
        elif use_local_tests and test_run.process_died:
            # Killed by a signal (OOM, an operator, the host): the suite never
            # reached a verdict, so it must not be filed as a test failure.
            final_status = STATUS_NEEDS_REVIEW
            error = "test process died before finishing (killed by a signal, no exit code)"
            if test_run.stall_diagnostics:
                error = f"{error}\n{test_run.stall_diagnostics}"
        elif test_exit_code != 0:
            final_status = STATUS_FAILED if test_exit_code is not None else STATUS_NEEDS_REVIEW
            error = f"tests failed (exit code {test_exit_code})"
            elog.error(
                "verifying",
                "tests_failed" if repair_iteration == 0 else "tests_failed_repair",
                error,
                exit_code=test_exit_code,
                iteration=repair_iteration,
            )
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

        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(iteration), -1) + 1 FROM verification_runs "
                "WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
        self._record_verification_run(
            experiment_id,
            int(row[0]),
            final_status,
            test_command,
            test_exit_code,
            test_output_capped,
            error,
            (ci_verification.commit_sha if ci_verification is not None else None),
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
    # Cleanup — explicit removal of worktree and branch
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

    # ------------------------------------------------------------------
    # Worktree retention (#376) — startup and periodic, terminal runs only
    # ------------------------------------------------------------------

    def cleanup_expired_worktrees(
        self, retention_hours: float | None = None, *, now: datetime | None = None
    ) -> WorktreeCleanupResult:
        """Remove worktrees of terminal experiments that ended over ``retention_hours`` ago.

        ``retention_hours`` defaults to ``NEXUS_EXPERIMENT_WORKTREE_RETENTION_HOURS``
        (see ``worktree_retention_hours``); ``0`` disables the pass. Only the
        worktree directory goes: the experiment row (including its
        ``worktree_path``), its JSONL log and its branch are kept, so the
        record stays complete and committed work stays recoverable.

        A worktree is never removed while its experiment is non-terminal, has
        no ``ended_at``, ended within the retention window, or has an adoption
        that has not finished, nor when its path lies outside
        ``worktrees_root``. Already-missing directories are skipped silently,
        so repeated passes are no-ops. A failure on one worktree is recorded
        (activity log, experiment log) and does not stop the others.
        """
        hours = worktree_retention_hours() if retention_hours is None else max(0.0, float(retention_hours))
        result = WorktreeCleanupResult(retention_hours=hours)
        if hours <= 0:
            return result
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ended_at, worktree_path FROM experiments "
                f"WHERE status IN ({', '.join('?' * len(_TERMINAL_STATUSES))}) "
                "AND worktree_path IS NOT NULL AND worktree_path != ''",
                _TERMINAL_STATUSES,
            ).fetchall()
        for row in rows:
            ended_at = _parse_utc(row["ended_at"])
            if ended_at is None or ended_at > cutoff:
                continue
            path = Path(row["worktree_path"])
            if not path.exists() and not path.is_symlink():
                continue
            try:
                if self._remove_expired_worktree(row["id"], path, hours):
                    result.removed.append(row["id"])
            except Exception as exc:  # noqa: BLE001 - one bad worktree must not stop the pass
                reason = sanitized_reason(f"{type(exc).__name__}: {exc}")
                result.errors[row["id"]] = reason
                self._record_worktree_cleanup_error(row["id"], str(path), reason)
        return result

    def _remove_expired_worktree(self, experiment_id: str, path: Path, hours: float) -> bool:
        """Remove one expired worktree; ``False`` if it turned out to be still needed."""
        terminal = ", ".join("?" * len(_TERMINAL_STATUSES))
        finished = ", ".join("?" * len(_ADOPTION_TERMINAL_STATUSES))
        with self._lock:
            # Re-checked right before deleting: an adoption may have been
            # queued since the candidates were listed.
            still_expired = self._conn.execute(
                f"SELECT 1 FROM experiments e WHERE e.id = ? AND e.status IN ({terminal}) "
                "AND NOT EXISTS (SELECT 1 FROM experiment_adoptions a "
                f"WHERE a.experiment_id = e.id AND a.status NOT IN ({finished}))",
                (experiment_id, *_TERMINAL_STATUSES, *_ADOPTION_TERMINAL_STATUSES),
            ).fetchone()
        if still_expired is None:
            return False

        root = Path(self.worktrees_root).resolve()
        if path.is_symlink() or root not in path.resolve().parents:
            raise ValueError(f"refusing to remove {path}: not a directory inside {root}")

        elog = self.get_experiment_logger(experiment_id)
        try:
            # Unregisters the worktree from the main repository; the rmtree
            # below is what actually guarantees the directory is gone.
            _run_git(
                ["worktree", "remove", "--force", str(path)],
                cwd=self.repo_root, elog=elog, phase="cleanup",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("git worktree remove failed for %s: %s", path, exc)
        if path.exists():
            shutil.rmtree(path)

        message = f"Removed worktree {path} after the {hours:g} h retention window"
        with self._lock, self._conn:
            insert_activity(
                self._conn,
                timestamp=_utcnow_iso(),
                entity_type="experiment",
                entity_id=experiment_id,
                event="worktree_removed",
                message=message,
            )
        logger.info("experiment %s: %s", experiment_id, message)
        elog.info("cleanup", "worktree_retention_removed", message, worktree_path=str(path))
        return True

    def _record_worktree_cleanup_error(self, experiment_id: str, path: str, reason: str) -> None:
        logger.warning("experiment %s: could not remove expired worktree %s: %s", experiment_id, path, reason)
        try:
            with self._lock, self._conn:
                insert_activity(
                    self._conn,
                    timestamp=_utcnow_iso(),
                    entity_type="experiment",
                    entity_id=experiment_id,
                    event="worktree_cleanup_failed",
                    message=reason,
                )
            self.get_experiment_logger(experiment_id).error(
                "cleanup", "worktree_retention_failed",
                "Could not remove expired worktree", worktree_path=path, reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 - recording is best effort
            logger.warning("could not record worktree cleanup failure for %s: %s", experiment_id, exc)
