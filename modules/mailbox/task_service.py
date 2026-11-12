"""Bridge from free-form mailbox tasks to Nexus capabilities.

A mailbox task goes straight to Nexus: it is recorded as ``queued`` and the
worker hands it to the local controller without an approval step (#264).

The local Nexus LLM is the orchestration layer. It may inspect real Nexus
state, search memory, delegate bounded sub-problems, discuss ambiguity and
only then select an execution primitive.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TYPE_CHECKING

from modules.central_logging import DEFAULT_LOG_DIR, get_task_logger
from modules.experiment_launcher import ExperimentLauncher
from modules.experiment_wait import resume_with_new_orchestrator
from modules.experiment_resume import resume_experiment
from modules.github_work_tracking import GitHubWorkTrackingError, GitHubWorkTrackingService
from .development import process_command
from .task_ingest import insert_task_approval
from modules.outbox_publisher import (
    OutboxMessage,
    PUBLISH_PENDING_WRITE,
    TaskApprovalOutboxPublisher,
)
from modules.run_store import DEFAULT_DB_PATH, init_db
from modules.task_change_classifier import classify_command, classify_task
from modules.task_issue_planner import plan_issue
# ``modules.task_orchestrator`` is imported lazily, not here. It imports
# ``modules.mailbox.development``, which runs this package's ``__init__`` and
# comes straight back to this module - so importing the orchestrator *first*
# raised `ImportError: cannot import name 'ACTION_ANSWER' from partially
# initialized module`. The cycle made whole test files uncollectable and left
# `import modules.task_orchestrator` working only when something happened to
# import the mailbox package before it. Annotations are already lazy here
# (`from __future__ import annotations`), so only the runtime uses need it.

if TYPE_CHECKING:
    from modules.experiment_service import ExperimentService
    from modules.task_orchestrator import TaskDecision

logger = logging.getLogger(__name__)

# Entry state: recorded and waiting for the worker to hand it to Nexus. It
# stays ``queued`` while Nexus decides, so a decision can claim it once.
_STATUS_QUEUED = "queued"
# Left by the removed approval step (#264). Old rows stay readable; nothing new
# is written in these states and a ``pending_approval`` row is never run.
_LEGACY_STATUS_PENDING = "pending_approval"
_LEGACY_STATUS_APPROVED = "approved"
_LEGACY_STATUS_REJECTED = "rejected"
_STATUS_SUBMITTED = "submitted"
_STATUS_RUNNING = "running"
_STATUS_SUCCEEDED = "succeeded"
_STATUS_FAILED = "failed"
_STATUS_NEEDS_REVIEW = "needs_review"
# Nexus decides what follows a failed experiment; poll_running keeps out (#263).
_STATUS_NEXUS_DECIDING = "nexus_deciding"
# A mutating command finished, but its required GitHub result comment has not
# been acknowledged yet.  This is deliberately non-terminal and recoverable.
_STATUS_RESULT_PENDING = "result_comment_pending"

_TERMINAL_STATUSES = {
    _LEGACY_STATUS_REJECTED, _STATUS_SUCCEEDED, _STATUS_FAILED, _STATUS_NEEDS_REVIEW,
}
_NON_TERMINAL_STATUSES = {
    _STATUS_QUEUED, _LEGACY_STATUS_PENDING, _LEGACY_STATUS_APPROVED,
    _STATUS_SUBMITTED, _STATUS_RUNNING, _STATUS_NEXUS_DECIDING,
    _STATUS_RESULT_PENDING,
}
_EXP_TERMINAL = {"succeeded", "failed", "timed_out", "needs_review", "rejected", "interrupted"}
_EXP_RUNNING = {"selecting", "implementing", "verifying"}
_EXP_FAILED_TERMINAL = {"failed", "timed_out", "needs_review", "interrupted"}
# Verified, and left open because the task forbids the merge (#185).
_STATUS_READY_FOR_REVIEW = "ready_for_review"
_TERMINAL_STATUSES.add(_STATUS_READY_FOR_REVIEW)
# After a failed experiment Nexus may try again, but a task gets at most this
# many experiments in total (#263 step 4).
_MAX_EXPERIMENTS_PER_TASK = 2


class ApprovalNotFoundError(LookupError):
    pass


class MailboxTaskApproval:
    __slots__ = (
        "id", "mailbox_msg_id", "task_body", "sender", "file_path",
        "received_at", "status", "approved_at", "rejected_at",
        "rejection_reason", "experiment_id", "outbox_file",
        "publish_status", "publish_commit_sha", "result_summary", "error",
        "idempotency_key",
        "merge_forbidden", "merge_constraint_reason",
        "created_at", "updated_at",
    )

    def __init__(self, **kwargs: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def to_dict(self) -> dict[str, Any]:
        return {slot: getattr(self, slot) for slot in self.__slots__}


def _row_to_approval(row: sqlite3.Row | tuple) -> MailboxTaskApproval:
    keys = MailboxTaskApproval.__slots__
    if isinstance(row, sqlite3.Row):
        return MailboxTaskApproval(**{k: row[k] for k in keys if k in row.keys()})
    return MailboxTaskApproval(**dict(zip(keys, row)))


_ISSUE_PLANNER_FACTS = {
    "source": "Nexus mailbox task",
    "note": "Nexus works on this issue through an experiment or a tracked command.",
}


def registry_issue_planner(task_text: str) -> dict[str, str] | None:
    """Plan a task issue with the first available free planning model (#655).

    Exactly one call (``plan_issue``) and never a paid model; no free model
    means ``None``, so the caller keeps its placeholder issue.
    """
    from modules import model_registry

    try:
        free = [
            m
            for m in model_registry.candidates(
                model_registry.load_models(), role="planning", max_cost="free"
            )
            if m.cost == "free"
        ]
    except Exception:
        logger.warning("mailbox_task: no model for issue planning", exc_info=True)
        return None
    if not free:
        return None
    return plan_issue(task_text, _ISSUE_PLANNER_FACTS, free[0])


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact(text: str) -> str:
    return re.sub(
        r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+",
        "[REDACTED]",
        text,
    )


def _safe_error(error: str | None) -> str | None:
    if not error:
        return None
    return _redact(error)[:500]


class _RecordingPlanner:
    """Wraps the issue planner and remembers whether it produced a draft."""

    def __init__(self, planner: Any) -> None:
        self._planner = planner
        self.planned = False

    def __call__(self, task_text: str) -> dict[str, str] | None:
        draft = self._planner(task_text)
        self.planned = isinstance(draft, dict) and all(
            isinstance(draft.get(key), str) and draft[key].strip() for key in ("title", "body")
        )
        return draft


class MailboxTaskService:
    """Route free-form mailbox tasks through the persistent local controller."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        experiment_service: "ExperimentService | None" = None,
        outbox_dir: str = "/agent/outbox",
        repo_path: str = "/agent",
        mailbox_config: Any | None = None,
        operations_registry: Any | None = None,
        orchestrator_factory: Any | None = None,
        github_work_tracking: GitHubWorkTrackingService | None = None,
        task_log_dir: str | Path | None = None,
        failure_analysis_service: Any | None = None,
        task_issue_planner: Any | None = None,
    ) -> None:
        self.db_path = db_path
        # Turns a change task into a planned GitHub issue (#655); ``None``
        # from it keeps the ``Mailbox task <id>`` placeholder.
        self.task_issue_planner = task_issue_planner or registry_issue_planner
        self.experiment_service = experiment_service
        self._task_log_dir = task_log_dir
        self.failure_analysis_service = failure_analysis_service
        self._owns_failure_analysis_service = failure_analysis_service is None
        self._failure_analysis_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="failure-analysis"
        )
        self._failure_analysis_futures: set[Future[Any]] = set()
        self._failure_analysis_futures_lock = threading.Lock()
        if mailbox_config is not None:
            self.outbox_dir = Path(mailbox_config.outbox_dir)
            self.repo_path = Path(mailbox_config.code_repo_path)
            self.mailbox_repo_path = Path(mailbox_config.mailbox_repo_path)
        else:
            self.outbox_dir = Path(outbox_dir)
            self.repo_path = Path(repo_path)
            self.mailbox_repo_path = self.repo_path

        self._lock = threading.RLock()
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn

        owns_registry = operations_registry is None and experiment_service is not None
        if owns_registry:
            from modules.operations_registry import OperationsRegistry

            operations_registry = OperationsRegistry(
                experiment_service=experiment_service,
                db_path=db_path,
                repo_root=str(self.repo_path),
            )
        self.operations_registry = operations_registry
        self._owns_operations_registry = owns_registry
        if orchestrator_factory is None:
            from modules.task_orchestrator import NexusTaskOrchestrator

            orchestrator_factory = NexusTaskOrchestrator
        self._orchestrator_factory = orchestrator_factory
        self.github_work_tracking = github_work_tracking or GitHubWorkTrackingService(
            db_path=db_path,
            repo_root=str(self.repo_path),
        )

    def close(self) -> None:
        # Finish queued diagnoses before closing their shared service/SQLite
        # connection. Analysis runs outside the mailbox poll loop.
        self._failure_analysis_executor.shutdown(wait=True)
        nexus_task_store = getattr(self, "_nexus_task_store", None)
        if nexus_task_store is not None:
            nexus_task_store.close()
        if self._owns_operations_registry and self.operations_registry is not None:
            close = getattr(self.operations_registry, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    logger.exception("mailbox_task: failed closing operations registry")
        close_tracking = getattr(self.github_work_tracking, "close", None)
        if close_tracking:
            try:
                close_tracking()
            except Exception:
                logger.exception("mailbox_task: failed closing GitHub work tracking")
        if self._owns_failure_analysis_service and self.failure_analysis_service is not None:
            try:
                self.failure_analysis_service.close()
            except Exception:
                logger.exception("mailbox_task: failed closing failure analysis service")
        self._conn.close()

    def _schedule_failure_analysis(self, callback: Any, *args: Any) -> None:
        """Queue best-effort diagnosis without blocking the mailbox poll loop."""
        context = contextvars.copy_context()
        try:
            future = self._failure_analysis_executor.submit(context.run, callback, *args)
        except RuntimeError:
            logger.warning("mailbox_task: failure analysis skipped because service is closing")
            return
        with self._failure_analysis_futures_lock:
            self._failure_analysis_futures.add(future)
        future.add_done_callback(self._failure_analysis_done)

    def _failure_analysis_done(self, future: Future[Any]) -> None:
        with self._failure_analysis_futures_lock:
            self._failure_analysis_futures.discard(future)
        try:
            future.result()
        except Exception:
            logger.exception("mailbox_task: background failure analysis crashed")

    def _failure_analysis(self, *, context: str | None = None) -> Any | None:
        """Lazily-built :class:`FailureAnalysisService`, or ``None`` if unavailable (#145).

        Diagnosis reuses an already-optional capability: a Nexus deployment
        without a reachable local LLM must still process mailbox tasks
        normally, so any failure to construct the service here degrades to
        "no diagnosis" rather than blocking task processing. Shared across
        every terminal-failure call site (this base class's own
        ``_transition_terminal`` and the finalization subclass), so at most
        one instance - and one lazily-opened SQLite connection to the run
        store - exists per service.

        *context* identifies the originating experiment/task (#150) so an
        operator can tell, from this log line alone, which lifecycle event
        went undiagnosed - construction is retried (and re-logged) on every
        caller until it succeeds, since a transient cause (e.g. an
        unwritable path) can clear between calls.
        """
        if self.failure_analysis_service is None:
            try:
                from modules.failure_analysis_service import FailureAnalysisService

                self.failure_analysis_service = FailureAnalysisService(
                    db_path=self.db_path,
                    log_dir=self._resolve_log_dir(),
                    outbox_dir=self.outbox_dir,
                    repo_root=self.mailbox_repo_path,
                    # The same read-only capability catalog the controller
                    # resolves against (#172), so a diagnosis here can check a
                    # runtime claim instead of only repeating it.
                    operations_registry=self.operations_registry,
                    experiment_service=self.experiment_service,
                )
            except Exception:
                logger.exception(
                    "mailbox_task: could not initialize failure analysis service (%s)",
                    context or "no context",
                )
                return None
        return self.failure_analysis_service

    def _analyze_task_failure(self, approval: MailboxTaskApproval, status: str, exp: Any) -> None:
        """Best-effort diagnosis for a mailbox task that failed or needs review (#145).

        Distinct from the finalization-phase integration
        (``FinalizingMailboxTaskService._analyze_finalization_failure``):
        this covers the earlier development/verification stage, before
        adoption/finalization is even attempted - a demonstration that the
        analyzer is reusable beyond the finalization call site it was first
        written for.
        """
        service = self._failure_analysis(
            context=f"mailbox_task={approval.id} experiment={exp.id}"
        )
        if service is None:
            logger.warning(
                "mailbox_task: failure analysis unavailable for task %s (experiment %s); "
                "no analysis or analysis_unavailable record will be produced",
                approval.id, exp.id,
            )
            return
        try:
            task_log = self._task_logger(approval.id)
            service.analyze_from_logs(
                subject_type="mailbox_task",
                subject_id=exp.id,
                experiment_id=exp.id,
                mailbox_task_id=approval.id,
                failure={
                    "status": status,
                    "phase": "verification",
                    "reason": _safe_error(getattr(exp, "error", None)),
                    "test_exit_code": getattr(exp, "test_exit_code", None),
                },
                goal=_safe_error(str(getattr(approval, "task_body", "") or "")[:500]),
                elog=task_log,
            )
        except Exception:
            logger.exception(
                "mailbox_task: failure analysis errored for task %s (experiment %s)",
                approval.id, exp.id,
            )

    def _resolve_log_dir(self) -> str | Path:
        """The structured-log directory this service actually writes to.

        Shared by ``_task_logger`` and the failure-analysis integration
        (``_failure_analysis``/``_analyze_task_failure``): the latter must
        read evidence from the exact same directory the former writes to,
        or a deployment/test using a non-default ``task_log_dir`` would
        silently get an empty evidence bundle.
        """
        if self._task_log_dir is not None:
            return self._task_log_dir
        if self.experiment_service is not None:
            candidate = getattr(self.experiment_service, "experiment_log_dir", None)
            if isinstance(candidate, (str, Path)):
                return candidate
        return DEFAULT_LOG_DIR

    def _task_logger(self, approval_id: str):
        """Durable, pre-experiment structured logger for one mailbox task.

        Records intake/queueing/orchestration events -
        everything before (and, for tasks that never create one, instead
        of) an experiment id existing - so the unified Logs feed shows the
        complete task path from the moment a task arrives. Never raises:
        a logging failure must not interrupt task processing.
        """
        return get_task_logger(approval_id, log_dir=self._resolve_log_dir())

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_task(
        self,
        mailbox_msg_id: int,
        task_body: str,
        sender: str,
        file_path: str,
        received_at: str,
        idempotency_key: str | None = None,
    ) -> MailboxTaskApproval:
        """Record a task once per idempotency key (#378).

        The key defaults to one derived from *mailbox_msg_id*; API callers
        may pass their own. A repeated delivery returns the original task
        unchanged, so it is approved and dispatched at most once.
        """
        with self._lock:
            approval_id, created = insert_task_approval(
                self._conn,
                mailbox_msg_id=mailbox_msg_id,
                task_body=task_body,
                sender=sender,
                file_path=file_path,
                received_at=received_at,
                idempotency_key=idempotency_key,
            )
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if created:
            self._task_logger(approval_id).info(
                "mailbox", "task_received",
                "mailbox task received, handed to Nexus",
            )
        else:
            self._task_logger(approval_id).info(
                "mailbox", "task_duplicate_delivery",
                "mailbox task delivered again, returning the original task",
            )
        return _row_to_approval(row)

    def get(self, approval_id: str) -> MailboxTaskApproval:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise ApprovalNotFoundError(f"no approval with id {approval_id!r}")
        return _row_to_approval(row)

    def list_pending(self, limit: int = 50) -> list[MailboxTaskApproval]:
        """Tasks still waiting for Nexus to take them up."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (_STATUS_QUEUED, limit),
            ).fetchall()
        return [_row_to_approval(row) for row in rows]

    def list_recent(self, limit: int = 20) -> list[MailboxTaskApproval]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_approval(row) for row in rows]

    # ------------------------------------------------------------------
    # Persistent agentic orchestration
    # ------------------------------------------------------------------

    def submit_queued(self) -> None:
        """Hand the oldest queued task to Nexus, which chooses the actual action."""
        if self.experiment_service is None or self.operations_registry is None:
            return
        # Finish durable machine-work comments before accepting more work.
        if self._retry_pending_machine_result():
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE status=? ORDER BY created_at LIMIT 1",
                (_STATUS_QUEUED,),
            ).fetchone()
        if row is None:
            return
        approval = _row_to_approval(row)
        self._orchestrate_approval(approval, approval.task_body)

    def _orchestrate_approval(
        self,
        approval: MailboxTaskApproval,
        current_input: str,
    ) -> None:
        orchestrator = None
        try:
            orchestrator = self._orchestrator_factory(
                operations_registry=self.operations_registry,
                experiment_service=self.experiment_service,
            )
            decision: TaskDecision = orchestrator.run(
                current_input,
                source_type="task",
                source_id=approval.id,
                title=f"Mailbox task {approval.id[:8]}",
                task_logger=self._task_logger(approval.id),
            )
        except Exception as exc:
            logger.exception("mailbox_task: local orchestrator failed for %s", approval.id)
            self._complete_without_experiment(
                approval,
                _STATUS_NEEDS_REVIEW,
                f"Local Nexus orchestration failed: {_safe_error(str(exc)) or type(exc).__name__}",
                error=str(exc),
            )
            return
        finally:
            if orchestrator is not None:
                close = getattr(orchestrator, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        logger.exception("mailbox_task: failed closing task orchestrator")

        logger.info(
            "mailbox_task: approval=%s session=%s selected action=%s reason=%s",
            approval.id[:8],
            (decision.session_id or "-")[:8],
            decision.action,
            decision.reason[:300],
        )
        self._dispatch_decision(approval, decision)

    def _dispatch_decision(
        self,
        approval: MailboxTaskApproval,
        decision: TaskDecision,
    ) -> None:
        from modules.task_orchestrator import (
            ACTION_ANSWER,
            ACTION_CLARIFICATION,
            ACTION_COMMAND,
            ACTION_MISSING_CAPABILITY,
            ACTION_NEW_EXPERIMENT,
            ACTION_RESUME_EXPERIMENT,
        )

        if decision.action == ACTION_NEW_EXPERIMENT:
            self._start_new_experiment(approval, decision)
        elif decision.action == ACTION_RESUME_EXPERIMENT:
            self._resume_existing_experiment(approval, decision)
        elif decision.action == ACTION_COMMAND:
            self._run_command(approval, decision)
        elif decision.action == ACTION_ANSWER:
            self._complete_without_experiment(
                approval,
                _STATUS_SUCCEEDED,
                decision.message or decision.reason or "Task answered from Nexus state.",
            )
        elif decision.action in (ACTION_CLARIFICATION, ACTION_MISSING_CAPABILITY):
            summary = decision.message or decision.reason or decision.action
            if decision.session_id:
                summary = f"{summary}\n\nNexus session: {decision.session_id}"
            self._complete_without_experiment(
                approval,
                _STATUS_NEEDS_REVIEW,
                summary,
                error=decision.reason or decision.message,
            )
        else:
            self._complete_without_experiment(
                approval,
                _STATUS_NEEDS_REVIEW,
                f"Unsupported orchestrator action: {decision.action}",
                error=decision.reason,
            )

    def _start_new_experiment(
        self, approval: MailboxTaskApproval, decision: TaskDecision | None = None
    ) -> dict[str, str] | None:
        # Re-read task_body so clarification follow-ups accumulated by
        # continue_discussion are included in the implementation goal.
        current_approval = self.get(approval.id)
        task_log = self._task_logger(approval.id)
        task_log.info("queued", "github_work_tracking_started", "starting GitHub work tracking")
        planner = self._issue_planner_for(current_approval.task_body)
        try:
            work_ref = self.github_work_tracking.ensure_for_source(
                SimpleNamespace(
                    source_type="task",
                    source_id=approval.id,
                    title=f"Mailbox task {approval.id[:8]}",
                    content=current_approval.task_body,
                ),
                planner=planner,
            )
        except GitHubWorkTrackingError as exc:
            task_log.error(
                "queued", "github_work_tracking_failed",
                "GitHub lifecycle blocked before implementation start", exc=exc,
            )
            self._complete_without_experiment(
                approval,
                _STATUS_NEEDS_REVIEW,
                f"GitHub lifecycle blocked before implementation start: {exc}",
                error=str(exc),
            )
            return
        # Issue #184: make the chosen Issue visible at the moment it is
        # chosen, so "did Nexus reuse #180 or open a duplicate?" is an
        # answerable question rather than an inference from GitHub state.
        task_log.info(
            "queued", "github_work_tracking_linked",
            f"work tracked on GitHub issue #{work_ref.issue_number}",
            issue_number=work_ref.issue_number,
            branch_name=work_ref.branch_name,
        )
        goal = current_approval.task_body
        if planner is not None and planner.planned:
            # #655: the experiment works on the planned issue, so its goal
            # names it the standard way (``GitHub Issue #N: title``).
            goal = (
                f"GitHub Issue #{work_ref.issue_number}: "
                f"{getattr(work_ref, 'issue_title', '') or 'planned task'}\n\n{goal}"
            )
        source = SimpleNamespace(
            source_type="task",
            source_id=approval.id,
            title=f"Mailbox task {approval.id[:8]}",
            content=current_approval.task_body,
        )

        def mark_submitted(experiment_id: str, _work_ref: Any) -> None:
            with self._lock:
                updated = self._conn.execute(
                    """UPDATE mailbox_task_approvals SET status=?, experiment_id=?, updated_at=?
                       WHERE id=? AND status=?""",
                    (_STATUS_SUBMITTED, experiment_id, _now_iso(), approval.id, _STATUS_QUEUED),
                ).rowcount
                self._conn.commit()
            if not updated:
                raise RuntimeError(
                    f"mailbox task {approval.id} left queued before experiment start"
                )
            task_log.info(
                "queued", "experiment_created", "experiment created for mailbox task",
                experiment_id=experiment_id,
            )

        with self._lock:
            current = self._conn.execute(
                "SELECT status FROM mailbox_task_approvals WHERE id=?", (approval.id,)
            ).fetchone()
            if current is None or current["status"] != _STATUS_QUEUED:
                return
            result = ExperimentLauncher(
                self.experiment_service,
                self.github_work_tracking,
                self._start_thread,
                self._after_experiment,
            ).launch(
                decision.session_id if decision is not None else None,
                goal,
                decision.experiment_handover if decision is not None else None,
                source,
                task_store=(
                    self._with_task_store(lambda store: store)
                    if decision is not None and decision.session_id else None
                ),
                create_kwargs={
                    "complexity": decision.complexity if decision is not None else None,
                    "source_type": "task",
                    "source_id": approval.id,
                },
                work_ref=work_ref,
                before_start=mark_submitted,
            )
            if result.already_active:
                task_log.info(
                    "queued", "already_active",
                    "mailbox task matched an active experiment",
                    experiment_id=result.experiment_id,
                    experiment_status=result.status,
                )
                self._complete_without_experiment(
                    approval,
                    _STATUS_NEEDS_REVIEW,
                    (
                        "already_active: an experiment for this GitHub issue is "
                        f"already active (experiment_id={result.experiment_id}, "
                        f"status={result.status})"
                    ),
                )
                return {
                    "status": "already_active",
                    "experiment_id": result.experiment_id,
                    "experiment_status": result.status,
                }

    def _issue_planner_for(self, task_body: str) -> "_RecordingPlanner | None":
        """The issue planner for a change task; ``None`` keeps the placeholder (#655).

        A question never reaches here with a planner. Whether the planner is
        actually called is ``ensure_for_source``'s decision: a task that
        names its issue (``solve github issue #N``) reuses that issue.
        """
        if self.task_issue_planner is None or classify_task(task_body or "") != "change":
            return None
        return _RecordingPlanner(self.task_issue_planner)

    def _track_machine_work(self, approval: MailboxTaskApproval, command: str) -> Any:
        """Create and durably record tracking before a mutation may execute."""
        task_log = self._task_logger(approval.id)
        body = self.get(approval.id).task_body or command
        work_ref = self.github_work_tracking.ensure_for_source(
            SimpleNamespace(
                source_type="task", source_id=approval.id,
                title=f"Mailbox task {approval.id[:8]}", content=body,
            ),
            planner=self._issue_planner_for(body), create_branch=False,
        )
        issue_number = getattr(work_ref, "issue_number", None)
        if not isinstance(issue_number, int):
            raise GitHubWorkTrackingError("machine work did not receive a GitHub issue")
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """INSERT INTO mailbox_machine_work
                   (task_id, command, issue_number, state, created_at, updated_at)
                   VALUES (?, ?, ?, 'tracked', ?, ?)
                   ON CONFLICT(task_id) DO NOTHING""",
                (approval.id, command, issue_number, now, now),
            )
            row = self._conn.execute(
                "SELECT command, issue_number FROM mailbox_machine_work WHERE task_id=?",
                (approval.id,),
            ).fetchone()
            self._conn.commit()
        if row is None or row["command"] != command or row["issue_number"] != issue_number:
            raise RuntimeError("machine-work tracking could not be persisted consistently")
        task_log.info(
            "queued", "github_work_tracking_linked",
            f"machine work tracked on GitHub issue #{issue_number}",
            issue_number=issue_number,
        )
        return work_ref

    def _comment_machine_result(
        self, approval: MailboxTaskApproval, work_ref: Any, command: str, status: str, result_body: str
    ) -> None:
        """Post the required result comment; failures remain retryable."""
        marker = self._machine_result_marker(approval.id)
        self.github_work_tracking.comment(
            work_ref.issue_number,
            (
                f"Nexus ran a command for mailbox task `{approval.id[:8]}`.\n\n"
                f"**Status:** {status}\n\n"
                f"```text\n$ {command[:2000]}\n```\n\n"
                f"{_redact(result_body)[:50000]}\n\n{marker}"
            ),
        )

    @staticmethod
    def _machine_result_marker(task_id: str) -> str:
        return f"<!-- nexus-mailbox-machine-result:{task_id} -->"

    def _machine_result_comment_exists(self, issue_number: int, task_id: str) -> bool:
        return self.github_work_tracking.issue_comment_contains(
            issue_number, self._machine_result_marker(task_id)
        )

    def _retry_pending_machine_result(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                """SELECT m.*, a.* FROM mailbox_machine_work m
                   JOIN mailbox_task_approvals a ON a.id=m.task_id
                   WHERE a.status=? ORDER BY a.updated_at LIMIT 1""",
                (_STATUS_RESULT_PENDING,),
            ).fetchone()
        if row is None:
            return False
        approval = self.get(str(row["task_id"]))
        work_ref = SimpleNamespace(issue_number=int(row["issue_number"]))
        if row["state"] != "commented":
            try:
                if not self._machine_result_comment_exists(work_ref.issue_number, approval.id):
                    try:
                        self._comment_machine_result(
                            approval, work_ref, str(row["command"]), str(row["result_status"]),
                            str(row["result_body"] or ""),
                        )
                    except Exception:
                        # A transport error does not prove the POST failed.
                        # Reconcile before leaving it pending for another pass.
                        if not self._machine_result_comment_exists(
                            work_ref.issue_number, approval.id
                        ):
                            raise
            except Exception as exc:
                self._task_logger(approval.id).warning(
                    "queued", "github_result_comment_failed",
                    f"result comment still pending on issue #{row['issue_number']}", exc=exc,
                )
                return True
            with self._lock:
                self._conn.execute(
                    "UPDATE mailbox_machine_work SET state='commented', updated_at=? WHERE task_id=?",
                    (_now_iso(), approval.id),
                )
                self._conn.commit()
        summary = f"{row['result_body']}\n\nTracked on GitHub issue #{row['issue_number']}."
        self._complete_claimed_without_experiment(
            approval, str(row["result_status"]), summary, error=row["error"],
            expected_status=_STATUS_RESULT_PENDING,
        )
        return True

    def _with_task_store(self, action: Any) -> Any:
        """Run *action* against the Nexus task store the orchestrator uses.

        Opened once and kept: the worker checks it every cycle.
        """
        if getattr(self, "_nexus_task_store", None) is None:
            from modules.nexus_task_store import NexusTaskStore

            # The service's own database: the same file as the orchestrator's
            # in production, and never a mock's attribute in tests (#279).
            self._nexus_task_store = NexusTaskStore(db_path=self.db_path)
        return action(self._nexus_task_store)

    def resume_finished_experiments(self) -> None:
        """Safety net for #263: continue sessions whose experiment ended unnoticed.

        The experiment thread continues its session the moment it ends; a
        restart in between loses that moment. Checking is one query, so this
        runs every worker cycle and builds an orchestrator only when a waiting
        session exists.
        """
        if self.experiment_service is None:
            return
        waiting = self._with_task_store(lambda store: store.sessions_waiting_for_experiments())
        for session in waiting:
            experiment_id = session.waiting_experiment_id
            if not experiment_id:
                continue
            try:
                experiment = self.experiment_service.get_experiment(experiment_id)
                if experiment.status not in _EXP_TERMINAL:
                    continue
                self._after_experiment(experiment_id)
            except Exception:
                logger.exception(
                    "mailbox_task: resume after experiment %s failed", experiment_id
                )

    def _after_experiment(self, experiment_id: str) -> None:
        """Continue the Nexus session, then carry out what it decided (#263 step 4).

        A succeeded experiment keeps its existing path (``poll_running`` and
        finalization). After a failed one the task stays open until Nexus has
        decided: another experiment, within the limit, or an end with Nexus'
        own words. The task is claimed as ``nexus_deciding`` before Nexus
        resumes, so ``poll_running`` cannot close it in between.
        """
        experiment = self.experiment_service.get_experiment(experiment_id)
        claimed = experiment.status in _EXP_FAILED_TERMINAL and self._claim_decision(experiment_id)
        try:
            decisions = resume_with_new_orchestrator(
                self._orchestrator_factory,
                self.experiment_service,
                self.github_work_tracking,
                experiment_id,
                operations_registry=self.operations_registry,
            )
        except BaseException:
            if claimed:
                self._release_decision(experiment_id)
            raise
        if not claimed:
            return
        if not decisions:
            # Nobody resumed the session here; the ordinary path takes over.
            self._release_decision(experiment_id)
            return
        self._continue_after_failed_experiment(
            self._approval_for_experiment(experiment_id), experiment, decisions[0]
        )

    def _claim_decision(self, experiment_id: str) -> bool:
        with self._lock:
            claimed = self._conn.execute(
                """UPDATE mailbox_task_approvals SET status=?, updated_at=?
                   WHERE experiment_id=? AND status IN (?, ?)""",
                (_STATUS_NEXUS_DECIDING, _now_iso(), experiment_id,
                 _STATUS_SUBMITTED, _STATUS_RUNNING),
            ).rowcount
            self._conn.commit()
        return claimed > 0

    def _release_decision(self, experiment_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE mailbox_task_approvals SET status=?, updated_at=?
                   WHERE experiment_id=? AND status=?""",
                (_STATUS_RUNNING, _now_iso(), experiment_id, _STATUS_NEXUS_DECIDING),
            )
            self._conn.commit()

    def _approval_for_experiment(self, experiment_id: str) -> MailboxTaskApproval | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
        return _row_to_approval(row) if row else None

    def _nexus_decides_failure(self, approval: MailboxTaskApproval) -> bool:
        """Whether Nexus is still deciding what follows a failed experiment.

        ``poll_running`` leaves such a task open; ``_after_experiment`` closes
        it. A resume that dies leaves the session failed or abandoned, and
        the ordinary path takes over again.
        """
        session = self._with_task_store(
            lambda store: store.find_by_source("task", approval.id)
        )
        return session is not None and session.status in (
            "waiting_for_experiment",
            "active",
        )

    def _continue_after_failed_experiment(
        self,
        approval: MailboxTaskApproval,
        experiment: Any,
        decision: TaskDecision,
    ) -> None:
        from modules.task_orchestrator import (
            ACTION_ANSWER,
            ACTION_CLARIFICATION,
            ACTION_MISSING_CAPABILITY,
            ACTION_NEW_EXPERIMENT,
        )

        status = (
            _STATUS_NEEDS_REVIEW if experiment.status == "needs_review" else _STATUS_FAILED
        )
        attempts = len(
            self._with_task_store(
                lambda store: store.recent_events(
                    decision.session_id or "",
                    role="tool",
                    kind=ACTION_NEW_EXPERIMENT,
                    limit=_MAX_EXPERIMENTS_PER_TASK + 1,
                )
            )
        )
        if decision.action == ACTION_NEW_EXPERIMENT and attempts < _MAX_EXPERIMENTS_PER_TASK:
            self._task_logger(approval.id).info(
                "queued", "experiment_retry",
                f"Nexus starts experiment {attempts + 1} of {_MAX_EXPERIMENTS_PER_TASK} "
                "after a failed one",
                previous_experiment_id=experiment.id,
            )
            with self._lock:
                updated = self._conn.execute(
                    """UPDATE mailbox_task_approvals SET status=?, updated_at=?
                       WHERE id=? AND status=?""",
                    (_STATUS_QUEUED, _now_iso(), approval.id, _STATUS_NEXUS_DECIDING),
                ).rowcount
                self._conn.commit()
            if updated:
                self._start_new_experiment(self.get(approval.id), decision)
            return
        note = None
        if decision.action == ACTION_NEW_EXPERIMENT:
            note = (
                f"Nexus wanted another experiment; the limit of "
                f"{_MAX_EXPERIMENTS_PER_TASK} experiments per task is reached."
            )
        elif decision.action in (ACTION_ANSWER, ACTION_CLARIFICATION, ACTION_MISSING_CAPABILITY):
            note = decision.message or decision.reason or None
        self._transition_terminal(approval, status, experiment, nexus_note=note)

    def _resume_existing_experiment(
        self,
        approval: MailboxTaskApproval,
        decision: TaskDecision,
    ) -> None:
        task_log = self._task_logger(approval.id)
        if not decision.experiment_id:
            self._complete_without_experiment(
                approval,
                _STATUS_NEEDS_REVIEW,
                "The local orchestrator chose resume but did not identify an experiment.",
            )
            return
        experiment = self.experiment_service.resolve_experiment(decision.experiment_id)
        task_log.info(
            "queued", "resume_started", "resuming existing experiment",
            experiment_id=experiment.id,
        )
        try:
            self.github_work_tracking.recover_experiment(experiment)
        except GitHubWorkTrackingError as exc:
            task_log.error(
                "queued", "resume_failed",
                "GitHub lifecycle blocked before experiment resume", exc=exc,
                experiment_id=experiment.id,
            )
            self._complete_without_experiment(
                approval,
                _STATUS_NEEDS_REVIEW,
                f"GitHub lifecycle blocked before experiment resume: {exc}",
                error=str(exc),
            )
            return
        with self._lock:
            updated = self._conn.execute(
                """UPDATE mailbox_task_approvals SET status=?, experiment_id=?, updated_at=?
                   WHERE id=? AND status=?""",
                (_STATUS_SUBMITTED, experiment.id, _now_iso(), approval.id, _STATUS_QUEUED),
            ).rowcount
            self._conn.commit()
        if not updated:
            return
        self._start_thread(
            experiment.id,
            lambda: resume_experiment(
                self.experiment_service,
                experiment.id,
                github_work_tracking=self.github_work_tracking,
            ),
        )

    def _run_command(self, approval: MailboxTaskApproval, decision: TaskDecision) -> None:
        command = (decision.command or "").strip()
        if not command:
            self._complete_without_experiment(
                approval,
                _STATUS_NEEDS_REVIEW,
                "The local orchestrator chose command but did not provide a command.",
            )
            return
        with self._lock:
            claimed = self._conn.execute(
                "UPDATE mailbox_task_approvals SET status=?, updated_at=? WHERE id=? AND status=?",
                (_STATUS_RUNNING, _now_iso(), approval.id, _STATUS_QUEUED),
            ).rowcount
            self._conn.commit()
        if not claimed:
            return
        # #655: machine work is a change and is tracked on an issue that gets
        # the result; a read-only command is answered without one.
        mutating = classify_command(command) == "mutating"
        work_ref = None
        if mutating:
            try:
                work_ref = self._track_machine_work(approval, command)
            except Exception as exc:
                self._task_logger(approval.id).error(
                    "running", "github_work_tracking_failed",
                    "mutating command blocked before execution", exc=exc,
                )
                self._complete_claimed_without_experiment(
                    approval, _STATUS_NEEDS_REVIEW,
                    f"Mutating command was not run because GitHub tracking failed: {_safe_error(str(exc))}",
                    error=str(exc), expected_status=_STATUS_RUNNING,
                )
                return
        try:
            msg = SimpleNamespace(body=command, meta={})
            result_body, _ = process_command(
                msg,
                self.repo_path,
                self._conn,
                approval.mailbox_msg_id,
            )
            success = "**Exit code:** 0" in result_body
            status = _STATUS_SUCCEEDED if success else _STATUS_FAILED
            error = None if success else "command failed"
        except Exception as exc:
            result_body = f"Command execution failed: {_safe_error(str(exc)) or type(exc).__name__}"
            status = _STATUS_FAILED
            error = str(exc)
        if work_ref is None:
            self._complete_claimed_without_experiment(
                approval, status, result_body, error=error, expected_status=_STATUS_RUNNING
            )
            return
        # Commit the command result before the fallible network call.  From
        # here onward retries only post the comment; they never rerun command.
        with self._lock:
            self._conn.execute(
                """UPDATE mailbox_machine_work SET state='result_pending',
                   result_status=?, result_body=?, error=?, updated_at=? WHERE task_id=?""",
                (status, result_body, _safe_error(error), _now_iso(), approval.id),
            )
            self._conn.execute(
                "UPDATE mailbox_task_approvals SET status=?, updated_at=? WHERE id=? AND status=?",
                (_STATUS_RESULT_PENDING, _now_iso(), approval.id, _STATUS_RUNNING),
            )
            self._conn.commit()
        self._retry_pending_machine_result()

    def _start_thread(self, experiment_id: str, target: Any) -> None:
        def _run() -> None:
            try:
                target()
            except Exception:
                logger.exception(
                    "mailbox_task: experiment %s raised during execution", experiment_id
                )

        threading.Thread(
            target=_run,
            daemon=True,
            name=f"mts-exp-{experiment_id[:8]}",
        ).start()

    # ------------------------------------------------------------------
    # Experiment polling / recovery
    # ------------------------------------------------------------------

    def poll_running(self) -> None:
        if self.experiment_service is None:
            return
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE status IN (?, ?) AND experiment_id IS NOT NULL",
                (_STATUS_SUBMITTED, _STATUS_RUNNING),
            ).fetchall()
        for row in rows:
            approval = _row_to_approval(row)
            try:
                exp = self.experiment_service.get_experiment(approval.experiment_id)
            except Exception:
                logger.exception(
                    "mailbox_task: failed to fetch experiment %s", approval.experiment_id
                )
                continue
            if exp.status in _EXP_RUNNING or (
                approval.status == _STATUS_SUBMITTED and exp.status not in _EXP_TERMINAL
            ):
                with self._lock:
                    self._conn.execute(
                        "UPDATE mailbox_task_approvals SET status=?, updated_at=? WHERE id=?",
                        (_STATUS_RUNNING, _now_iso(), approval.id),
                    )
                    self._conn.commit()
            elif exp.status == "succeeded":
                self._transition_terminal(approval, _STATUS_SUCCEEDED, exp)
            elif exp.status in _EXP_FAILED_TERMINAL and self._nexus_decides_failure(approval):
                continue
            elif exp.status in ("failed", "timed_out", "interrupted"):
                self._transition_terminal(approval, _STATUS_FAILED, exp)
            elif exp.status == "needs_review":
                self._transition_terminal(approval, _STATUS_NEEDS_REVIEW, exp)

    def recover_after_restart(self) -> None:
        with self._lock:
            now = _now_iso()
            interrupted_machine_ids = [
                str(r["id"])
                for r in self._conn.execute(
                    """SELECT a.id FROM mailbox_task_approvals a
                       JOIN mailbox_machine_work m ON m.task_id=a.id
                       WHERE a.status=? AND a.experiment_id IS NULL
                         AND m.state='tracked'""",
                    (_STATUS_RUNNING,),
                ).fetchall()
            ]
            # The mutation may have completed immediately before process
            # death.  Never run it again: persist an unknown/interrupted
            # outcome and make reporting that uncertainty the retryable work.
            for task_id in interrupted_machine_ids:
                body = (
                    "Nexus was interrupted after this mutating command started. "
                    "Its outcome is unknown, and it was not run again."
                )
                self._conn.execute(
                    """UPDATE mailbox_machine_work SET state='result_pending',
                       result_status=?, result_body=?, error=?, updated_at=?
                       WHERE task_id=? AND state='tracked'""",
                    (_STATUS_NEEDS_REVIEW, body, "command outcome unknown after restart", now, task_id),
                )
                self._conn.execute(
                    """UPDATE mailbox_task_approvals SET status=?, updated_at=?
                       WHERE id=? AND status=?""",
                    (_STATUS_RESULT_PENDING, now, task_id, _STATUS_RUNNING),
                )
            interrupted_ids = [
                str(r["id"])
                for r in self._conn.execute(
                    """SELECT id FROM mailbox_task_approvals
                       WHERE status=? AND experiment_id IS NULL""",
                (_STATUS_RUNNING,),
                ).fetchall()
            ]
            self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, error=?, updated_at=?
                   WHERE status=? AND experiment_id IS NULL""",
                (
                    _STATUS_NEEDS_REVIEW,
                    "non-experiment task interrupted by restart",
                    _now_iso(),
                    _STATUS_RUNNING,
                ),
            )
            # A decision cut off by the restart goes back to the ordinary path.
            self._conn.execute(
                "UPDATE mailbox_task_approvals SET status=?, updated_at=? WHERE status=?",
                (_STATUS_RUNNING, _now_iso(), _STATUS_NEXUS_DECIDING),
            )
            self._conn.commit()
        for approval_id in interrupted_ids:
            self._task_logger(approval_id).warning(
                "mailbox", "task_interrupted_by_restart",
                "non-experiment task interrupted by restart; marked needs_review",
            )
        for approval_id in interrupted_machine_ids:
            self._task_logger(approval_id).warning(
                "mailbox", "machine_work_interrupted_by_restart",
                "mutating command outcome unknown; result comment queued without rerunning",
            )
        # One attempt per recovery pass. A remote outage must not turn startup
        # into a busy loop; the ordinary worker cycle retries it later.
        self._retry_pending_machine_result()
        self.poll_running()

    # ------------------------------------------------------------------
    # Terminal results / outbox
    # ------------------------------------------------------------------

    def _transition_terminal(
        self,
        approval: MailboxTaskApproval,
        new_status: str,
        exp: Any,
        *,
        nexus_note: str | None = None,
    ) -> None:
        summary = self._make_result_summary(exp)
        if nexus_note:
            summary = f"{summary}\n\nNexus: {nexus_note}"
        self._write_result_outbox(approval, new_status, summary, exp)
        with self._lock:
            self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, result_summary=?, error=?, updated_at=? WHERE id=?""",
                (
                    new_status,
                    summary,
                    _safe_error(exp.error)
                    if new_status in (_STATUS_FAILED, _STATUS_NEEDS_REVIEW)
                    else None,
                    _now_iso(),
                    approval.id,
                ),
            )
            self._conn.commit()
        task_log = self._task_logger(approval.id)
        if new_status in (_STATUS_FAILED, _STATUS_NEEDS_REVIEW):
            task_log.warning(
                "queued", "task_completed", f"mailbox task reached terminal status {new_status}",
                experiment_id=getattr(exp, "id", None),
            )
            self._write_problem(approval, new_status, summary, exp)
            self._schedule_failure_analysis(
                self._analyze_task_failure, approval, new_status, exp
            )
        else:
            task_log.info(
                "queued", "task_completed", f"mailbox task reached terminal status {new_status}",
                experiment_id=getattr(exp, "id", None),
            )

    def _complete_without_experiment(
        self,
        approval: MailboxTaskApproval,
        status: str,
        summary: str,
        *,
        error: str | None = None,
    ) -> None:
        with self._lock:
            updated = self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, experiment_id=NULL, result_summary=?, error=?, updated_at=?
                   WHERE id=? AND status=?""",
                (
                    status,
                    summary[:8000],
                    _safe_error(error),
                    _now_iso(),
                    approval.id,
                    _STATUS_QUEUED,
                ),
            ).rowcount
            self._conn.commit()
        if updated:
            self._task_logger(approval.id).warning(
                "queued", "task_completed_without_experiment",
                f"mailbox task completed without an experiment (status={status})",
            )
            self._write_result_outbox(approval, status, summary, None)

    def _complete_claimed_without_experiment(
        self,
        approval: MailboxTaskApproval,
        status: str,
        summary: str,
        *,
        error: str | None = None,
        expected_status: str | None = None,
    ) -> None:
        with self._lock:
            updated = self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, result_summary=?, error=?, updated_at=?
                   WHERE id=? AND (? IS NULL OR status=?)""",
                (status, summary[:8000], _safe_error(error), _now_iso(), approval.id,
                 expected_status, expected_status),
            ).rowcount
            self._conn.commit()
        if updated:
            self._task_logger(approval.id).warning(
                "queued", "task_completed_without_experiment",
                f"mailbox task completed without an experiment (status={status})",
            )
            self._write_result_outbox(approval, status, summary, None)

    def _work_ref_for(self, exp: Any) -> Any | None:
        """Best-effort GitHub link for a finished experiment (never fatal)."""
        experiment_id = getattr(exp, "id", None)
        lookup = getattr(self.github_work_tracking, "for_experiment", None)
        if not experiment_id or not callable(lookup):
            return None
        try:
            return lookup(str(experiment_id))
        except Exception:
            logger.debug(
                "mailbox_task: could not resolve GitHub link for %s", experiment_id,
                exc_info=True,
            )
            return None

    def _make_result_summary(self, exp: Any) -> str:
        parts = [f"status={exp.status}"]
        # Issue #184: report the Issue the work was tracked on so a reused
        # Issue number is externally verifiable from the task result alone.
        work_ref = self._work_ref_for(exp)
        issue_number = getattr(work_ref, "issue_number", None)
        if isinstance(issue_number, int):
            parts.append(f"issue=#{issue_number}")
            branch_name = getattr(work_ref, "branch_name", None)
            if isinstance(branch_name, str) and branch_name:
                parts.append(f"branch={branch_name}")
        command = " ".join(getattr(exp, "test_command", None) or [])
        if command:
            parts.append(f"test_command={command[:1000]}")
        parts.append(
            f"test_exit_code={exp.test_exit_code if exp.test_exit_code is not None else 'none'}"
        )
        if getattr(exp, "error", None):
            parts.append(f"error={_safe_error(exp.error)}")
        if exp.diff_stat:
            parts.append(f"diff_stat={exp.diff_stat[:100]}")
        summary = "; ".join(parts)
        output = getattr(exp, "test_output", None)
        if output:
            safe_output = re.sub(
                r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+",
                "[REDACTED]",
                str(output),
            )
            summary += f"\n\n## Bounded verification output\n\n```text\n{safe_output[-6000:]}\n```"
        return summary

    def _write_result_outbox(
        self,
        approval: MailboxTaskApproval,
        status: str,
        summary: str,
        exp: Any | None,
    ) -> str | None:
        try:
            experiment_id = exp.id if exp is not None else "none"
            test_result = ""
            if exp is not None and exp.test_exit_code is not None:
                test_result = (
                    f"\ntest_result: {'pass' if exp.test_exit_code == 0 else 'fail'}"
                )
            content = (
                "---\n"
                "type: task_approval_result\n"
                f"status: {status}\n"
                f"approval_id: {approval.id}\n"
                f"experiment_id: {experiment_id}\n"
                f"sender: {approval.sender}\n"
                f"source: {Path(approval.file_path).name}\n"
                f"created_at: {approval.created_at}{test_result}\n"
                "---\n\n"
                "## Task Result\n\n"
                f"**Status:** {status}\n\n"
                f"{summary[:32000]}\n"
            )
            with self._lock:
                self._conn.execute(
                    """UPDATE mailbox_task_approvals
                       SET outbox_file=NULL, outbox_content=?, publish_status=?, updated_at=?
                       WHERE id=?""",
                    (content, PUBLISH_PENDING_WRITE, _now_iso(), approval.id),
                )
                self._conn.commit()
            publisher = TaskApprovalOutboxPublisher(
                db_path=self.db_path,
                mailbox_config=SimpleNamespace(
                    mailbox_repo_path=self.mailbox_repo_path,
                    outbox_dir=self.outbox_dir,
                ),
            )
            try:
                return publisher.write_message(OutboxMessage(approval.id, content))
            finally:
                publisher.close()
        except Exception:
            logger.exception("mailbox_task: failed to write outbox for %s", approval.id)
            return None

    def _write_problem(
        self,
        approval: MailboxTaskApproval,
        status: str,
        summary: str,
        exp: Any,
    ) -> None:
        try:
            runs_dir = self.repo_path / "state" / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            filename = runs_dir / (
                f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                f"-task-approval-{approval.id[:8]}.json"
            )
            filename.write_text(
                json.dumps(
                    {
                        "timestamp": _now_iso(),
                        "source": "mailbox_task_service",
                        "approval_id": approval.id,
                        "experiment_id": exp.id,
                        "response": (
                            f"ERROR: task approval {approval.id[:8]} "
                            f"ended with status={status}; {summary}"
                        ),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("mailbox_task: failed to write problem for %s", approval.id)


class MailboxTaskWorker(threading.Thread):
    def __init__(self, service: MailboxTaskService, interval: float = 10.0) -> None:
        super().__init__(daemon=True, name="mailbox-task-worker")
        self._service = service
        self._interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("MailboxTaskWorker started (interval=%.0fs)", self._interval)
        while not self._stop_event.wait(self._interval):
            try:
                self._service.submit_queued()
            except Exception:
                logger.exception("MailboxTaskWorker: submit_queued failed")
            try:
                self._service.poll_running()
            except Exception:
                logger.exception("MailboxTaskWorker: poll_running failed")
            try:
                self._service.resume_finished_experiments()
            except Exception:
                logger.exception("MailboxTaskWorker: resume_finished_experiments failed")
        logger.info("MailboxTaskWorker stopped")
