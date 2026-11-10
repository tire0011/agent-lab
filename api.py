import json
import os
import re
import logging
import threading
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

import chat as chat_module
from data_sources import InvalidIdeaTextError, add_idea, list_ideas, list_problems
from modules import mailbox as mailbox_module
from modules.codex_task_service import (
    CodexTaskService,
    TaskNotFoundError,
    TaskNotRunnableError,
)
from modules.mailbox.config import MailboxConfig
from modules.mailbox.github_webhook import (
    expected_repository,
    has_inbox_changes,
    is_ping_event,
    repository_matches,
    verify_signature,
)
from modules.mailbox.poll_runner import run_mailbox_poll_once
from modules.mailbox.task_service import (
    ApprovalNotFoundError,
    MailboxTaskService,
    MailboxTaskWorker,
)
from modules.nexus_chat_service import NexusChatService
from modules.nexus_chat_jobs import NexusChatJobService, STATUS_FAILED, STATUS_SUCCEEDED
from modules.heartbeat import resolve_stale_timeout
from modules.nexus_task_store import NexusTaskStore
from modules.task_orchestrator import TaskOrchestratorError
from modules.experiment_adoption_service import ExperimentAdoptionError, ExperimentAdoptionService
from modules.experiment_service import (
    ExperimentNotCancellableError,
    ExperimentNotFoundError,
    ExperimentService,
)
from modules.failure_analysis_service import FailureAnalysisService
from modules.operations_registry import OperationValidationError, OperationsRegistry
from modules.operation_chain_service import OperationChainService
from modules import redaction
from modules.run_store import DEFAULT_DB_PATH, calculate_estimated_cost, get_applicable_price
from models import (
    DiagnosticActivityResponse,
    ChatRequest,
    ChatJobResponse,
    CodexTaskCreateRequest,
    CodexTaskListResponse,
    CodexTaskResponse,
    CodexTaskSummary,
    DiagnosticReportResponse,
    DiagnosticServiceLogsResponse,
    ExperimentAdoptionStatus,
    ExperimentDetail,
    ExperimentLogEntry,
    ExperimentLogsResponse,
    FailureAnalysesResponse,
    FailureAnalysisEntry,
    FailureAnalysisRequest,
    LogTraceResponse,
    UnifiedLogsResponse,
    ExperimentsResponse,
    GitCommitPushRequest,
    HealthResponse,
    Idea,
    IdeaCreateRequest,
    IdeasResponse,
    LlamaModelTestRequest,
    MailboxMessageEntry,
    MailboxMessagesResponse,
    MaintenanceAuditResponse,
    MaintenanceExecuteRequest,
    MaintenanceExecuteResponse,
    MaintenanceStatusResponse,
    NexusLivenessResponse,
    NexusSessionClaim,
    OperationCatalogEntry,
    OperationResponse,
    OperationsCatalogResponse,
    OperationsExecuteRequest,
    OperationsExecuteResponse,
    MailboxTaskApproval,
    MailboxTaskApprovalsResponse,
    OperationChainRequest,
    OperationChainResult,
    PublicDiagnosticExperiment,
    PublicDiagnosticMailboxStatus,
    PublicDiagnosticMailboxTask,
    PublicDiagnosticServiceStatus,
    PublicDiagnosticsResponse,
    Problem,
    ProblemsResponse,
    PublicActivityResponse,
    PublicExperimentSummary,
    ServiceRestartRequest,
    SetConfigRequest,
    StatusResponse,
)
from modules.log_reader import LogReader
from modules.operations_executor import OperationsExecutor
from modules.outbox_publisher import OperationChainOutboxPublisher, TaskApprovalOutboxPublisher
from modules.redaction import sanitized_reason as _sanitized_reason
from modules import central_logging as central_logging_module
from modules import log_trace as log_trace_module
from modules import nexus_diagnostics as nexus_diagnostics_module
from modules.logging_config import configure_logging
from modules.maintenance_service import (
    MaintenanceAuthError,
    MaintenanceDisabledError,
    MaintenanceOperationError,
    MaintenanceService,
)
from nexus_mcp import create_mcp_mount
from nexus_version import VERSION

def _read_git_revision() -> str | None:
    """Read the revision currently checked out, without retaining the result."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        revision = completed.stdout.strip()
        return revision or None
    except Exception:
        return None


_PROCESS_START_TIME = time.time()
# Capture at module load: reading HEAD lazily would describe a later checkout,
# not the revision this process is executing.
_STARTUP_REVISION = _read_git_revision()
configure_logging()
_logger = logging.getLogger(__name__)
_MCP_MOUNT = create_mcp_mount()

# How long a session may sit untouched mid-turn before it is called abandoned.
# Inferred from `updated_at` (no heartbeat, #158), so it sits well clear of a slow
# but healthy turn: calling a live session dead is worse than noticing a dead one late.
_STALE_SESSION_SECONDS = int(os.environ.get("NEXUS_STALE_SESSION_SECONDS", 6 * 60 * 60))

# With the heartbeat (#162) a worker says it is alive explicitly, so silence means
# something and the timeout can be minutes, not hours. ``resolve_stale_timeout``
# clamps a zero/negative/unparsable setting to a safe floor rather than reaping live work.
_STALE_HEARTBEAT_SECONDS = resolve_stale_timeout(
    os.environ.get("NEXUS_STALE_HEARTBEAT_SECONDS")
)
LIMIT_MIN = 1


@lru_cache(maxsize=1)
def get_codex_service() -> CodexTaskService:
    """Singleton CodexTaskService for the process.

    Overridable in tests via ``app.dependency_overrides[get_codex_service]``.
    """
    return CodexTaskService()


@lru_cache(maxsize=1)
def get_experiment_service() -> ExperimentService:
    """Singleton ExperimentService for the process, shared with MailboxTaskService
    so both observe the same in-process state (one SQLite connection/lock).

    Overridable in tests via ``app.dependency_overrides[get_experiment_service]``.
    """
    return ExperimentService()


@lru_cache(maxsize=1)
def get_mailbox_task_service() -> MailboxTaskService:
    """Singleton MailboxTaskService for the process.

    Overridable in tests via ``app.dependency_overrides[get_mailbox_task_service]``.
    """
    return MailboxTaskService(
        experiment_service=_resolve_experiment_service(),
        mailbox_config=MailboxConfig.from_env(),
    )


@lru_cache(maxsize=1)
def get_experiment_adoption_service() -> ExperimentAdoptionService:
    """Singleton adoption service for the process."""
    from modules.nexus_reviewer import default_reviewer

    return ExperimentAdoptionService(
        experiment_service=_resolve_experiment_service(), reviewer=default_reviewer(),
    )


@lru_cache(maxsize=1)
def get_failure_analysis_service() -> FailureAnalysisService:
    """Singleton failure-analysis service for the process (#145), wired to the same
    read-only capability catalog as the persistent controller (#172).

    Overridable in tests via ``app.dependency_overrides[get_failure_analysis_service]``.
    """
    return FailureAnalysisService(
        operations_registry=get_operations_service(),
        experiment_service=_resolve_experiment_service(),
    )


@lru_cache(maxsize=1)
def get_operations_service() -> OperationsRegistry:
    """Singleton safe operations registry for the process."""
    return OperationsRegistry(experiment_service=_resolve_experiment_service())


@lru_cache(maxsize=1)
def get_nexus_chat_service() -> NexusChatService:
    return NexusChatService(
        operations_registry=get_operations_service(),
        experiment_service=get_experiment_service(),
    )


@lru_cache(maxsize=1)
def get_nexus_chat_job_service() -> NexusChatJobService:
    return NexusChatJobService(get_nexus_chat_service())


@lru_cache(maxsize=1)
def get_operation_chain_service() -> OperationChainService:
    """Singleton operation chain service for the process."""
    return OperationChainService(
        operations_registry=_resolve_operations_service(),
        mailbox_config=MailboxConfig.from_env(),
    )


def get_nexus_task_store() -> Iterator[NexusTaskStore]:
    """Controller session store for liveness inspection, opened and closed per
    request - occasional diagnostic reads do not need a process-lifetime handle.

    Overridable in tests via ``app.dependency_overrides[get_nexus_task_store]``.
    """
    store = NexusTaskStore()
    try:
        yield store
    finally:
        store.close()


@lru_cache(maxsize=1)
def get_maintenance_service() -> MaintenanceService:
    """Singleton MaintenanceService for the process."""
    return MaintenanceService()


def _resolve_experiment_service() -> ExperimentService:
    override = app.dependency_overrides.get(get_experiment_service)
    if override is not None:
        return override()
    return get_experiment_service()


def _resolve_codex_service() -> CodexTaskService:
    """Look up the (possibly test-overridden) Codex service for startup use:
    direct calls to ``get_codex_service()`` would bypass ``dependency_overrides``.
    """
    override = app.dependency_overrides.get(get_codex_service)
    if override is not None:
        return override()
    return get_codex_service()


def _resolve_mailbox_task_service() -> MailboxTaskService:
    override = app.dependency_overrides.get(get_mailbox_task_service)
    if override is not None:
        return override()
    return get_mailbox_task_service()


def _resolve_experiment_adoption_service() -> ExperimentAdoptionService:
    override = app.dependency_overrides.get(get_experiment_adoption_service)
    if override is not None:
        return override()
    return get_experiment_adoption_service()


def _resolve_operations_service() -> OperationsRegistry:
    override = app.dependency_overrides.get(get_operations_service)
    if override is not None:
        return override()
    return get_operations_service()


def _resolve_operation_chain_service() -> OperationChainService:
    override = app.dependency_overrides.get(get_operation_chain_service)
    if override is not None:
        return override()
    return get_operation_chain_service()


def _resolve_nexus_chat_job_service() -> NexusChatJobService:
    override = app.dependency_overrides.get(get_nexus_chat_job_service)
    if override is not None:
        return override()
    return get_nexus_chat_job_service()


# How often the API process sweeps for stalled experiments (#400); startup
# reconciliation only helps on a restart.
_STALL_SWEEP_INTERVAL_SECONDS = 5 * 60


def _sweep_stalled_experiments_once() -> None:
    try:
        stalled = _resolve_experiment_service().sweep_stalled_experiments()
    except Exception as exc:  # noqa: BLE001 - the sweep must never stop the API
        _logger.error("could not sweep stalled experiments: %s", _sanitized_reason(str(exc)))
        return
    if isinstance(stalled, list) and stalled:
        _logger.warning(
            "marked %d stalled experiment(s) failed: %s",
            len(stalled),
            ", ".join(stalled[:10]),
        )


def _start_stall_sweeper(stop: threading.Event) -> threading.Thread:
    """Sweep for stalled experiments every few minutes until *stop* is set."""

    def run() -> None:
        while not stop.wait(_STALL_SWEEP_INTERVAL_SECONDS):
            _sweep_stalled_experiments_once()

    thread = threading.Thread(target=run, daemon=True, name="experiment-stall-sweeper")
    thread.start()
    return thread


def _resume_blocked_capabilities_after_startup() -> None:
    """Resume one eligible capability blocker behind normal initiative gates."""
    store = None
    try:
        store = NexusTaskStore()
        if not store.blocked_missing_capabilities(limit=1):
            return
    except Exception as exc:  # noqa: BLE001 - startup recovery is best effort
        _logger.error("could not inspect blocked capability sessions: %s", exc)
        return
    finally:
        if store is not None:
            store.close()

    def run() -> None:
        from modules.nexus_controller_service import NexusControllerService
        from modules.nexus_initiative import NexusInitiativeService
        from modules.task_orchestrator import NexusTaskOrchestrator

        registry = None
        orchestrator = None
        initiative = None
        try:
            experiment_service = _resolve_experiment_service()
            registry = OperationsRegistry(experiment_service=experiment_service)
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=experiment_service,
            )
            controller = NexusControllerService(orchestrator=orchestrator)
            initiative = NexusInitiativeService(
                db_path=DEFAULT_DB_PATH,
                experiment_service=experiment_service,
                adoption_service=_resolve_experiment_adoption_service(),
                controller=controller,
            )
            decision = initiative.resume_blocked_capability()
            if decision.decision == "dispatched":
                _logger.info(
                    "resumed capability-blocked controller session %s after startup",
                    decision.candidate.ref if decision.candidate else "?",
                )
            elif decision.decision not in {"idle", "busy", "budget_exhausted"}:
                _logger.warning(
                    "capability-blocked startup resume ended as %s: %s",
                    decision.decision,
                    decision.detail,
                )
        except Exception as exc:  # noqa: BLE001 - never block API startup
            _logger.error("capability-blocked session resume failed: %s", exc)
        finally:
            if initiative is not None:
                initiative.close()
            if orchestrator is not None:
                orchestrator.close()
            if registry is not None:
                close = getattr(registry, "close", None)
                if callable(close):
                    close()

    threading.Thread(
        target=run,
        daemon=True,
        name="nexus-capability-resume",
    ).start()


@lru_cache(maxsize=1)
def get_operations_executor() -> OperationsExecutor:
    """Singleton OperationsExecutor for the process."""
    return OperationsExecutor()


@lru_cache(maxsize=1)
def get_log_reader() -> LogReader:
    """Singleton LogReader for the process, reading the merged
    agent-api/nexus-mailbox/llama-server systemd journals.

    Overridable in tests via ``app.dependency_overrides[get_log_reader]`` so a test
    never shells out to the real ``journalctl`` binary.
    """
    return LogReader()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with _MCP_MOUNT.lifespan():
        # Mark orphaned running Codex tasks failed exactly once at startup; never
        # restarts execution and never starts a Codex process.
        _resolve_codex_service().mark_interrupted_tasks_failed()
        # Experiment threads die with this process too (#281, #371); reconcile them
        # before the mailbox recovery so the waiting Nexus session can resume.
        try:
            _interrupted = _resolve_experiment_service().mark_interrupted_experiments_failed()
            if isinstance(_interrupted, list) and _interrupted:
                _logger.warning(
                    "reconciled %d experiment(s) interrupted by service restart: %s",
                    len(_interrupted),
                    ", ".join(_interrupted[:10]),
                )
        except Exception as _exc:  # noqa: BLE001 - recovery must not block startup
            _logger.error("could not mark interrupted experiments: %s", _exc)
        # Terminal experiments keep their worktree for inspection; past the retention
        # window it is removed so finished runs cannot fill the disk (#376). Runs after
        # the reconciliation above, which stamps just-interrupted runs with a fresh ended_at.
        try:
            _gc = _resolve_experiment_service().cleanup_expired_worktrees()
            if getattr(_gc, "removed", None):
                _logger.info("removed %d expired experiment worktree(s)", len(_gc.removed))
            if getattr(_gc, "errors", None):
                _logger.error("could not remove %d expired experiment worktree(s)", len(_gc.errors))
        except Exception as _exc:  # noqa: BLE001 - cleanup must not block startup
            _logger.error("could not clean up expired experiment worktrees: %s", _exc)
        mts = _resolve_mailbox_task_service()
        readiness = getattr(mts.github_work_tracking, "readiness_error", lambda: None)()
        if isinstance(readiness, str) and readiness:
            _logger.error("Nexus implementation lifecycle is blocked: %s", readiness)
        mts.recover_after_restart()
        _resolve_experiment_adoption_service().recover_after_restart()
        _resolve_operation_chain_service().recover_after_restart()
        _resume_blocked_capabilities_after_startup()

        # Sessions only reach a terminal status through the final decision path, so a turn
        # dying mid-flight leaves its row `active` forever (#158). Marking is deliberate and
        # resumes nothing - re-running a turn may repeat whatever killed it. Never aborts startup.
        try:
            _abandoned = NexusTaskStore().abandon_stale_sessions(
                _STALE_SESSION_SECONDS
            )
            if _abandoned:
                _logger.warning(
                    "marked %d controller session(s) abandoned after %ds without activity",
                    len(_abandoned),
                    _STALE_SESSION_SECONDS,
                )
        except Exception as _exc:  # noqa: BLE001 - recovery must not block startup
            _logger.error("could not reap stale controller sessions: %s", _exc)

        # Claims whose worker stopped heartbeating (#162): on restart every inherited
        # worker is gone, so reconcile their claims to an explicit terminal reason instead
        # of waiting out the updated_at threshold. Idempotent, never resumes work.
        _claim_store = None
        try:
            _claim_store = NexusTaskStore()
            _stale_claims = _claim_store.reconcile_stale_heartbeats(
                _STALE_HEARTBEAT_SECONDS
            )
            if _stale_claims:
                _logger.warning(
                    "released %d controller claim(s) with no heartbeat for %ds: %s",
                    len(_stale_claims),
                    _STALE_HEARTBEAT_SECONDS,
                    ", ".join(s.id for s in _stale_claims[:10]),
                )
        except Exception as _exc:  # noqa: BLE001 - recovery must not block startup
            _logger.error("could not reconcile stale controller claims: %s", _exc)
        finally:
            if _claim_store is not None:
                try:
                    _claim_store.close()
                except Exception:  # noqa: BLE001
                    pass

        # Best-effort outbox recovery: publish un-pushed outbox records from a previous
        # crash. Each publisher is fully wrapped so a constructor or close failure never
        # skips the next one and never aborts startup. exc_info is omitted on purpose:
        # raw tracebacks can expose paths/tokens.
        _mc = MailboxConfig.from_env()
        for _pub_cls in (OperationChainOutboxPublisher, TaskApprovalOutboxPublisher):
            _cls_name = getattr(_pub_cls, "__name__", repr(_pub_cls))
            _pub = None
            try:
                _pub = _pub_cls(mailbox_config=_mc)
                _pub.publish_pending()
            except Exception as _exc:
                _logger.warning(
                    "Startup outbox recovery failed for %s: %s",
                    _cls_name,
                    _sanitized_reason(str(_exc)),
                )
            finally:
                if _pub is not None:
                    try:
                        _pub.close()
                    except Exception as _close_exc:
                        _logger.warning(
                            "Startup outbox close failed for %s: %s",
                            _cls_name,
                            _sanitized_reason(str(_close_exc)),
                        )

        worker = MailboxTaskWorker(mts)
        worker.start()
        stall_sweep_stop = threading.Event()
        _start_stall_sweeper(stall_sweep_stop)
        try:
            yield
        finally:
            stall_sweep_stop.set()
            worker.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/mcp", _MCP_MOUNT)

_OUTBOX_DIR = MailboxConfig.from_env().outbox_dir

API_KEY = os.environ["AGENT_API_KEY"]
GITHUB_WEBHOOK_SECRET_ENV = "AGENT_GITHUB_WEBHOOK_SECRET"
_MAILBOX_LOCK_DEFAULT = "/var/lib/nexus/mailbox.lock"


def _mailbox_lock_path_from_env() -> Path:
    return Path(os.environ.get("AGENT_MAILBOX_LOCK_PATH", _MAILBOX_LOCK_DEFAULT))


def _run_mailbox_poll_once_for_webhook() -> None:
    result = run_mailbox_poll_once(
        db_path=DEFAULT_DB_PATH,
        mailbox_config=MailboxConfig.from_env(),
        lock_path=_mailbox_lock_path_from_env(),
        dry_run=False,
    )
    if result.status == "busy":
        # The active processor rescans the inbox before releasing the lock, so the
        # announced message is still picked up.
        _logger.info("mailbox_webhook busy: another processor is already running")
        return
    if result.status == "processed":
        _logger.info("mailbox_webhook accepted")
        return
    if result.status == "dirty_worktree":
        _logger.error("mailbox_webhook failed reason=dirty_worktree")
        return
    if result.status == "pull_failed":
        _logger.error("mailbox_webhook failed reason=pull_failed")
        return
    if result.status == "sync_failed":
        _logger.error("mailbox_webhook failed reason=sync_failed count=%d", len(result.results))
        return
    if result.status == "error":
        _logger.error("mailbox_webhook failed reason=%s", _sanitized_reason(result.error) or "unknown")
        return


def _start_mailbox_webhook_poll() -> None:
    thread_name = f"mailbox-webhook-poll-{time.time_ns()}"
    threading.Thread(
        target=_run_mailbox_poll_once_for_webhook,
        daemon=True,
        name=thread_name,
    ).start()


def authorize(x_api_key: str | None):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


_PUBLIC_PHASES = {
    "queued": "queued", "selecting": "selecting", "implementing": "implementing",
    "verifying": "verifying", "succeeded": "succeeded", "failed": "failed",
    "needs_review": "needs_review", "timed_out": "failed", "rejected": "needs_review",
    "cancelled": "failed",
    "interrupted": "failed",
}


def _public_phase(status: str | None) -> str:
    return _PUBLIC_PHASES.get(status or "", "needs_review")


def _changed_file_names(git_status: str | None) -> list[str]:
    """Extract basenames only; public responses must never contain paths."""
    return redaction.changed_file_names(git_status)


def _failure_reason(experiment) -> str | None:
    """Classify failures without copying raw error or exception text."""
    if experiment.status == "timed_out":
        return "Experiment timed out."
    if experiment.status == "rejected":
        return "Experiment was rejected by the safety policy."
    if experiment.status == "cancelled":
        return "Experiment was cancelled."
    if experiment.status == "interrupted":
        return "Experiment was interrupted by a service restart."
    if experiment.status == "needs_review":
        return "Experiment requires human review."
    if experiment.status == "failed":
        if experiment.test_exit_code is not None:
            return "Automated tests failed."
        return "Experiment failed before verification completed."
    return None


def _test_summary(experiment) -> str | None:
    if experiment.test_exit_code == 0:
        return "Automated tests passed."
    if experiment.test_exit_code is not None:
        return f"Automated tests failed with exit code {experiment.test_exit_code}."
    return None


def _mailbox_task_id(service: ExperimentService, experiment_id: str) -> str | None:
    lookup = getattr(service, "mailbox_task_id_for_experiment", None)
    return lookup(experiment_id) if lookup else None


_SECRET_PATTERN = redaction._SECRET_PATTERN
_PATH_PATTERN = redaction._PATH_PATTERN


def _redact(text: str) -> str:
    return redaction.redact(text)


def _revisions_differ(startup: str | None, checkout: str | None) -> bool:
    """Only claim staleness when both revisions could be determined."""
    return startup is not None and checkout is not None and startup != checkout


@lru_cache(maxsize=1)
def _deployment_time() -> str | None:
    """Process start time, used as a proxy for 'when this build was deployed'."""
    try:
        return datetime.fromtimestamp(_PROCESS_START_TIME, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except Exception:
        return None


def get_nexus_diagnostics() -> Iterator[nexus_diagnostics_module.NexusDiagnostics]:
    """Read-only diagnostic backend, opened and closed per request like
    ``get_nexus_task_store``.

    Overridable in tests via ``app.dependency_overrides[get_nexus_diagnostics]``.
    """
    try:
        diagnostics = nexus_diagnostics_module.NexusDiagnostics(
            log_dir=_resolve_experiment_service().experiment_log_dir,
            stale_after_seconds=_STALE_HEARTBEAT_SECONDS,
        )
    except nexus_diagnostics_module.DiagnosticsError as exc:
        # The backend opens the database read-only and never creates it, so a deployment
        # without one has nothing to diagnose - a service condition with a plain cause, not a 500.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        yield diagnostics
    finally:
        diagnostics.close()


# Router wiring (#579): the routes live in api_routers/ by topic; this module
# stays the single entry point - same app object, lifespan, /mcp mount and
# service/dependency helpers - and re-exports every moved name. The routers
# are included in the exact order the routes were registered on app before
# the split, so path matching (fixed paths before /{...} paths) is unchanged.

from fastapi.routing import APIRouter

from api_routers import (
    activity,
    codex,
    diagnostics,
    experiments,
    logs,
    mailbox,
    maintenance,
    operations,
)
from api_routers import chat as chat_routes
from api_routers import health as health_routes
from api_routers import ideas as ideas_routes
from api_routers import status as status_routes


from api_routers.activity import (  # noqa: E402
    ACTIVITY_DEFAULT_LIMIT,
    ACTIVITY_MAX_LIMIT,
    _diagnostic_lines,
    _sanitized_rejection_reason,
    list_activity,
    list_public_activity,
)
from api_routers.chat import chat, get_chat_job  # noqa: E402
from api_routers.codex import (  # noqa: E402
    CODEX_DEFAULT_TIMEOUT_SECONDS,
    CODEX_LIST_DEFAULT_LIMIT,
    CODEX_LIST_MAX_LIMIT,
    CODEX_LIST_PROMPT_PREVIEW_CHARS,
    CODEX_MAX_TIMEOUT_SECONDS,
    CODEX_SANDBOX,
    CODEX_WORKING_DIR,
    _prompt_preview,
    create_codex_task,
    get_codex_task,
    list_codex_tasks,
    run_codex_task,
)
from api_routers.diagnostics import (  # noqa: E402
    DIAGNOSTIC_LOG_DEFAULT_LINES,
    DIAGNOSTIC_LOG_MAX_LINES,
    DIAGNOSTICS_DEFAULT_LIMIT,
    DIAGNOSTICS_MAX_LIMIT,
    FAILURE_ANALYSES_DEFAULT_LIMIT,
    FAILURE_ANALYSES_MAX_LIMIT,
    _aggregate_usage,
    _diagnostic_experiment,
    _diagnostic_mailbox_task,
    _diagnostic_report_response,
    _estimated_cost,
    _experiment_lifecycle,
    _phase_duration_ms,
    get_diagnostic_last_failure,
    get_diagnostic_llm_job,
    get_diagnostic_logs,
    get_diagnostic_service_logs,
    get_diagnostic_status,
    get_failure_analysis,
    get_nexus_liveness,
    get_public_diagnostics,
    list_failure_analyses,
    trigger_failure_analysis,
)
from api_routers.experiments import (  # noqa: E402
    EXPERIMENTS_DEFAULT_LIMIT,
    EXPERIMENTS_MAX_LIMIT,
    _public_experiment,
    _public_experiment_usage,
    adopt_experiment,
    cancel_experiment,
    get_experiment,
    get_experiment_drift,
    get_experiment_adoption,
    get_public_experiment,
    list_experiments,
)
from api_routers.health import health, secure_health  # noqa: E402
from api_routers.ideas import create_idea, ideas  # noqa: E402
from api_routers.logs import (  # noqa: E402
    EXPERIMENT_LOGS_DEFAULT_LIMIT,
    EXPERIMENT_LOGS_MAX_LIMIT,
    UNIFIED_LOGS_DEFAULT_LIMIT,
    UNIFIED_LOGS_MAX_LIMIT,
    _apply_log_source,
    _derive_log_source,
    _paginate_log_entries,
    _CONTROLLER_SOURCE_PHASES,
    _LOG_LEVELS,
    _MAILBOX_SOURCE_PHASES,
    _UNIFIED_LOGS_JOURNAL_LINES,
    get_experiment_logs,
    get_log_trace,
    get_public_unified_logs,
    get_unified_logs,
)
from api_routers.mailbox import (  # noqa: E402
    MAILBOX_MESSAGES_DEFAULT_LIMIT,
    MAILBOX_MESSAGES_MAX_LIMIT,
    MAILBOX_TASKS_DEFAULT_LIMIT,
    MAILBOX_TASKS_MAX_LIMIT,
    _approval_to_response,
    _inbox_entries,
    _mailbox_preview,
    _mailbox_type_label,
    _outbox_entries,
    _safe_mailbox_sender,
    _MAILBOX_TYPE_LABELS_DE,
    _SAFE_SENDER_RE,
    get_mailbox_task,
    list_mailbox_messages,
    list_mailbox_tasks,
    mailbox_github_webhook,
)
from api_routers.maintenance import (  # noqa: E402
    get_maintenance_audit,
    get_maintenance_status,
    maintenance_execute,
)
from api_routers.operations import (  # noqa: E402
    OPERATION_CHAINS_DEFAULT_LIMIT,
    OPERATION_CHAINS_MAX_LIMIT,
    _op_response,
    execute_operation,
    get_operation_chain,
    get_operations_catalog,
    list_operation_chains,
    operations_context_env,
    operations_context_set_config,
    operations_git_commit_push,
    operations_git_pull,
    operations_git_status,
    operations_healing_cleanup_temps,
    operations_healing_diagnose,
    operations_healing_fix_locks,
    operations_llama_reload_model,
    operations_logs_errors,
    operations_logs_tail,
    operations_models_status,
    operations_models_test,
    operations_services_restart,
    operations_services_status,
    start_operation_chain,
)
from api_routers.status import problems, status  # noqa: E402

def _include_router(router: APIRouter) -> None:
    """Register *router*'s routes on the app, in the router's own order.

    The routes are appended to ``app.router.routes`` directly - exactly what
    ``@app.get(...)`` did before the split - so ``app.routes`` lists every
    route in registration order. The routers are created with this app as
    their ``dependency_overrides_provider``, so ``app.dependency_overrides``
    keeps applying to their ``Depends(...)`` dependencies.
    """
    app.router.routes.extend(router.routes)
    app.router._mark_routes_changed()


_include_router(health_routes.router)
_include_router(status_routes.router)
_include_router(ideas_routes.router)
_include_router(chat_routes.router)
_include_router(codex.router)
_include_router(mailbox.router)
_include_router(experiments.router)
_include_router(logs.router)
_include_router(experiments.adoption_router)
_include_router(operations.registry_router)
_include_router(experiments.public_router)
_include_router(activity.router)
_include_router(operations.chains_router)
_include_router(diagnostics.router)
_include_router(logs.public_router)
_include_router(maintenance.router)
_include_router(operations.executor_router)
