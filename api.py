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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import chat as chat_module
from data_sources import InvalidIdeaTextError, add_idea, list_ideas, list_problems
from modules import mailbox as mailbox_module
from modules.codex_task_service import (
    CodexTaskService,
    TaskNotFoundError,
    TaskNotRunnableError,
)
from modules.mailbox.config import MailboxConfig
from modules.mailbox.github_webhook import has_inbox_changes, is_ping_event, verify_signature
from modules.mailbox.poll_runner import run_mailbox_poll_once
from modules.mailbox.task_service import (
    ApprovalNotFoundError,
    ApprovalStateError,
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
    ChatResponse,
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
    MailboxTaskRejectRequest,
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
    PublicLogEntry,
    PublicUnifiedLogsResponse,
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

_PROCESS_START_TIME = time.time()
configure_logging()
_logger = logging.getLogger(__name__)
_MCP_MOUNT = create_mcp_mount()

# How long a controller session may sit untouched mid-turn before it is called
# abandoned. Liveness has to be inferred from `updated_at` because there is no
# heartbeat (#158), so this sits well clear of a slow but healthy turn: local
# inference plus tool calls can legitimately take many minutes, and calling a
# live session dead is worse than noticing a dead one late.
_STALE_SESSION_SECONDS = int(os.environ.get("NEXUS_STALE_SESSION_SECONDS", 6 * 60 * 60))

# The heartbeat path (#162) replaces that inference for work that claims itself:
# a worker says explicitly that it is alive, so silence means something and the
# timeout can be minutes instead of hours. ``resolve_stale_timeout`` clamps a
# zero/negative/unparsable setting to a safe floor rather than reaping live work.
_STALE_HEARTBEAT_SECONDS = resolve_stale_timeout(
    os.environ.get("NEXUS_STALE_HEARTBEAT_SECONDS")
)



# ---------------------------------------------------------------------------
# Codex task API: fixed server-side execution boundaries.
#
# Clients never choose the working directory or sandbox mode; both are
# hard-coded here so Codex tasks started through this API stay read-only
# and confined to the repository root, regardless of request payload.
# ---------------------------------------------------------------------------
CODEX_WORKING_DIR = "/agent"
CODEX_SANDBOX = "read-only"
CODEX_DEFAULT_TIMEOUT_SECONDS = 120
CODEX_MAX_TIMEOUT_SECONDS = 120
LIMIT_MIN = 1
CODEX_LIST_DEFAULT_LIMIT = 20
CODEX_LIST_MAX_LIMIT = 100
CODEX_LIST_PROMPT_PREVIEW_CHARS = 80
MAILBOX_TASKS_DEFAULT_LIMIT = 20
MAILBOX_TASKS_MAX_LIMIT = 100
EXPERIMENTS_DEFAULT_LIMIT = 20
EXPERIMENTS_MAX_LIMIT = 100
EXPERIMENT_LOGS_DEFAULT_LIMIT = 500
EXPERIMENT_LOGS_MAX_LIMIT = 5000
UNIFIED_LOGS_DEFAULT_LIMIT = 500
UNIFIED_LOGS_MAX_LIMIT = 5000
ACTIVITY_DEFAULT_LIMIT = 50
ACTIVITY_MAX_LIMIT = 200
OPERATION_CHAINS_DEFAULT_LIMIT = 20
OPERATION_CHAINS_MAX_LIMIT = 100


@lru_cache(maxsize=1)
def get_codex_service() -> CodexTaskService:
    """Singleton CodexTaskService for the process.

    Overridable in tests via ``app.dependency_overrides[get_codex_service]``.
    """
    return CodexTaskService()


@lru_cache(maxsize=1)
def get_experiment_service() -> ExperimentService:
    """Singleton ExperimentService for the process.

    Shared with ``MailboxTaskService`` so both the mailbox approval bridge
    and the read-only ``/experiments`` and ``/activity`` endpoints observe
    the same in-process state (single SQLite connection/lock), rather than
    each opening its own separate connection to the same database file.

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
    """Singleton failure-analysis service for the process (#145).

    Given the process's own operations registry so diagnosis reaches the same
    read-only capability catalog the persistent controller does (#172).

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
    """Controller session store for liveness inspection.

    Opened and closed per request rather than cached: this serves occasional
    diagnostic reads, and a process-lifetime connection for them would be one
    more shared handle for no benefit.

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
    """Look up the (possibly test-overridden) Codex service for startup use.

    Direct calls to ``get_codex_service()`` would bypass FastAPI's
    ``dependency_overrides`` used by tests, so lifespan startup goes through
    this helper instead.
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


# How often the API process sweeps for stalled experiments (#400). Startup
# reconciliation only helps on a restart; a worker that hangs silently in this
# process kept experiment deac59fb `implementing` for 11 hours.
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
    """Singleton LogReader for the process.

    Reads the ``agent-api``/``nexus-mailbox``/``llama-server`` systemd
    journals merged into ``GET /logs``. Overridable in tests via
    ``app.dependency_overrides[get_log_reader]`` so a test never shells
    out to the real ``journalctl`` binary.
    """
    return LogReader()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with _MCP_MOUNT.lifespan():
        # Mark orphaned 'running' Codex tasks as failed exactly once at startup.
        # Never restarts execution and never starts a Codex process.
        _resolve_codex_service().mark_interrupted_tasks_failed()
        # Experiment threads die with this process too (#281, #371). Reconciling them
        # before the mailbox recovery below lets the waiting Nexus session
        # resume and decide whether the task continues or ends.
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
        # Terminal experiments keep their worktree for inspection; past the
        # retention window it is removed so finished runs cannot fill the
        # disk (#376). Runs after the reconciliation above, which stamps
        # just-interrupted runs with a fresh ended_at, so they are kept.
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

        # Controller sessions only reach a terminal status through the final
        # decision path, so a turn that dies mid-flight leaves its row `active`
        # forever. 17 of the 41 sessions ever started were in that state,
        # untouched for 8 to 19 days, with nothing reaping or reporting them
        # (#158). Marking them is deliberate and does not resume anything:
        # re-running a turn costs a model call and may repeat whatever killed
        # it. Never aborts startup.
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

        # Claims whose worker stopped heartbeating (#162). A restart is the
        # clearest case: every worker this process inherited is gone by
        # definition, so their claims are reconciled to an explicit terminal
        # reason instead of waiting out the six-hour `updated_at` threshold.
        # Idempotent, and never resumes the work.
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

        # Best-effort outbox recovery: publish any un-pushed outbox records from
        # a previous crash.  Both publishers share the same MailboxConfig / DB.
        # Each publisher is fully wrapped so that a constructor *or* close failure
        # never skips the next publisher and never aborts the API startup.
        # exc_info is intentionally omitted: raw tracebacks can expose paths /
        # tokens; the redacted message is sufficient for diagnosis.
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

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
        # The active processor rescans the inbox before releasing the lock, so
        # the message this webhook announced is still picked up.
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


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


def authorize(x_api_key: str | None):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=VERSION)

@app.get("/secure-health")
def secure_health(x_api_key: str | None = Header(default=None)):
    authorize(x_api_key)
    return {"status": "ok", "auth": "ok"}


@app.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    return StatusResponse(**chat_module.build_status())


@app.get("/problems", response_model=ProblemsResponse)
def problems() -> ProblemsResponse:
    found: list[Problem] = list_problems()
    return ProblemsResponse(count=len(found), problems=found)


@app.get("/ideas", response_model=IdeasResponse)
def ideas() -> IdeasResponse:
    found: list[Idea] = list_ideas()
    return IdeasResponse(count=len(found), ideas=found)


@app.post("/ideas", response_model=Idea, status_code=201)
def create_idea(
    request: IdeaCreateRequest,
    x_api_key: str | None = Header(default=None),
) -> Idea:
    authorize(x_api_key)

    try:
        return add_idea(request.text)
    except InvalidIdeaTextError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/chat", response_model=ChatJobResponse, status_code=202)
def chat(
    request: ChatRequest,
    x_api_key: str | None = Header(default=None),
) -> ChatJobResponse:
    authorize(x_api_key)
    service = _resolve_nexus_chat_job_service()
    try:
        job = service.submit(request.message, session_id=request.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ChatJobResponse(job_id=job.id, status=job.status, session_id=job.session_id)


@app.get("/chat/jobs/{job_id}", response_model=ChatJobResponse)
def get_chat_job(
    job_id: str,
    x_api_key: str | None = Header(default=None),
) -> ChatJobResponse:
    authorize(x_api_key)
    service = _resolve_nexus_chat_job_service()
    job = service.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="chat job not found")
    if job.status == STATUS_SUCCEEDED and job.result is not None:
        result = job.result
        return ChatJobResponse(
  job_id=job.id,
  status=job.status,
  reply=result.reply,
  intent=result.action,
  session_id=result.session_id,
  action=result.action,
  experiment_id=result.experiment_id,
        )
    return ChatJobResponse(
        job_id=job.id,
        status=job.status,
        session_id=job.session_id,
        error=job.error if job.status == STATUS_FAILED else None,
    )

# ---------------------------------------------------------------------------
# Codex task endpoints (authenticated, synchronous, read-only sandbox only)
# ---------------------------------------------------------------------------


@app.post("/codex/tasks", response_model=CodexTaskResponse, status_code=201)
def create_codex_task(
    request: CodexTaskCreateRequest,
    x_api_key: str | None = Header(default=None),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskResponse:
    authorize(x_api_key)

    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt must not be empty")

    timeout_seconds = min(
        request.timeout_seconds or CODEX_DEFAULT_TIMEOUT_SECONDS,
        CODEX_MAX_TIMEOUT_SECONDS,
    )

    task = service.create_task(
        prompt=prompt,
        working_dir=CODEX_WORKING_DIR,
        sandbox=CODEX_SANDBOX,
        timeout_seconds=timeout_seconds,
    )
    return CodexTaskResponse(**task.to_dict())


def _prompt_preview(prompt: str, max_chars: int = CODEX_LIST_PROMPT_PREVIEW_CHARS) -> str:
    stripped = prompt.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars].rstrip() + "…"


@app.get("/codex/tasks", response_model=CodexTaskListResponse)
def list_codex_tasks(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=CODEX_LIST_DEFAULT_LIMIT, ge=LIMIT_MIN, le=CODEX_LIST_MAX_LIMIT),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskListResponse:
    authorize(x_api_key)

    tasks = service.list_tasks(limit=limit)
    return CodexTaskListResponse(
        count=len(tasks),
        tasks=[
            CodexTaskSummary(
                **task.to_dict(),
                prompt_preview=_prompt_preview(task.prompt),
            )
            for task in tasks
        ],
    )


@app.get("/codex/tasks/{task_id}", response_model=CodexTaskResponse)
def get_codex_task(
    task_id: str,
    x_api_key: str | None = Header(default=None),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskResponse:
    authorize(x_api_key)

    try:
        task = service.get_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="task not found")
    return CodexTaskResponse(**task.to_dict())


@app.post("/codex/tasks/{task_id}/run", response_model=CodexTaskResponse)
def run_codex_task(
    task_id: str,
    x_api_key: str | None = Header(default=None),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskResponse:
    authorize(x_api_key)

    try:
        task = service.run_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="task not found")
    except TaskNotRunnableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        # Worker-level failures are already captured as a stored task status
        # by CodexTaskService.run_task. Anything reaching here is an
        # unexpected internal error; never leak tracebacks/secrets.
        raise HTTPException(status_code=500, detail="internal error running codex task")
    return CodexTaskResponse(**task.to_dict())


# ---------------------------------------------------------------------------
# Mailbox task approval endpoints (authenticated)
# ---------------------------------------------------------------------------


def _approval_to_response(approval) -> MailboxTaskApproval:
    return MailboxTaskApproval(**approval.to_dict())


@app.get("/mailbox/tasks", response_model=MailboxTaskApprovalsResponse)
def list_mailbox_tasks(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=MAILBOX_TASKS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=MAILBOX_TASKS_MAX_LIMIT),
    service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> MailboxTaskApprovalsResponse:
    authorize(x_api_key)
    tasks = service.list_recent(limit=limit)
    return MailboxTaskApprovalsResponse(
        count=len(tasks),
        tasks=[_approval_to_response(t) for t in tasks],
    )


@app.get("/mailbox/tasks/{approval_id}", response_model=MailboxTaskApproval)
def get_mailbox_task(
    approval_id: str,
    x_api_key: str | None = Header(default=None),
    service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> MailboxTaskApproval:
    authorize(x_api_key)
    try:
        approval = service.get(approval_id)
    except ApprovalNotFoundError:
        raise HTTPException(status_code=404, detail="approval not found")
    return _approval_to_response(approval)


@app.post("/mailbox/tasks/{approval_id}/approve", response_model=MailboxTaskApproval)
def approve_mailbox_task(
    approval_id: str,
    x_api_key: str | None = Header(default=None),
    service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> MailboxTaskApproval:
    authorize(x_api_key)
    try:
        approval = service.approve(approval_id)
    except ApprovalNotFoundError:
        raise HTTPException(status_code=404, detail="approval not found")
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _approval_to_response(approval)


@app.post("/mailbox/tasks/{approval_id}/reject", response_model=MailboxTaskApproval)
def reject_mailbox_task(
    approval_id: str,
    request: MailboxTaskRejectRequest,
    x_api_key: str | None = Header(default=None),
    service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> MailboxTaskApproval:
    authorize(x_api_key)
    try:
        approval = service.reject(approval_id, request.reason)
    except ApprovalNotFoundError:
        raise HTTPException(status_code=404, detail="approval not found")
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _approval_to_response(approval)


@app.post("/mailbox/webhook/github")
async def mailbox_github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> JSONResponse:
    secret = os.environ.get(GITHUB_WEBHOOK_SECRET_ENV)
    if not secret:
        _logger.error("mailbox_webhook failed reason=missing_secret")
        raise HTTPException(status_code=503, detail="webhook not configured")

    raw_body = await request.body()
    if not verify_signature(
        secret=secret,
        raw_body=raw_body,
        signature_header=x_hub_signature_256,
    ):
        _logger.warning("mailbox_webhook rejected reason=invalid_signature")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _logger.warning("mailbox_webhook rejected reason=invalid_json")
        raise HTTPException(status_code=400, detail="invalid json")

    if is_ping_event(x_github_event):
        _logger.info("mailbox_webhook ignored reason=ping")
        return JSONResponse(status_code=200, content={"status": "pong"})

    if (x_github_event or "").strip().lower() != "push":
        _logger.info("mailbox_webhook ignored reason=event")
        return JSONResponse(status_code=200, content={"status": "ignored"})

    if not isinstance(payload, dict) or not has_inbox_changes(payload):
        _logger.info("mailbox_webhook ignored reason=no_inbox_changes")
        return JSONResponse(status_code=200, content={"status": "ignored"})

    _start_mailbox_webhook_poll()
    _logger.info("mailbox_webhook accepted reason=inbox_change")
    return JSONResponse(status_code=202, content={"status": "accepted"})


# ---------------------------------------------------------------------------
# Mailbox inbox/outbox listing (public, sanitized, read-only)
#
# Separate from the "Aufträge" task-approval bridge above: this surfaces the
# general communication mailbox (idea/task/question/context messages and
# their outbox responses) so the web UI can show what came in and what went
# out, without ever exposing full prompts, file paths, or secrets.
# ---------------------------------------------------------------------------

MAILBOX_MESSAGES_DEFAULT_LIMIT = 20
MAILBOX_MESSAGES_MAX_LIMIT = 100

_MAILBOX_TYPE_LABELS_DE = {
    "idea": "Idee",
    "task": "Auftrag",
    "question": "Frage",
    "context": "Kontext",
    "task_approval_result": "Freigabe-Ergebnis",
    "failure_analysis": "Fehleranalyse",
}

_SAFE_SENDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _mailbox_type_label(msg_type: str | None) -> str:
    if not msg_type:
        return "Nachricht"
    return _MAILBOX_TYPE_LABELS_DE.get(msg_type, msg_type.replace("_", " ").strip().capitalize() or "Nachricht")


def _safe_mailbox_sender(raw: str | None) -> str | None:
    """Only expose sender/creator identifiers matching a safe, narrow shape."""
    if not raw:
        return None
    candidate = raw.strip()
    return candidate if _SAFE_SENDER_RE.fullmatch(candidate) else None


def _mailbox_preview(text: str | None) -> str | None:
    if not text:
        return None
    # Drop markdown headings (e.g. "## Result") so the preview starts with
    # actual content, then redact and bound it like every other diagnostic.
    stripped = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    ).strip()
    return redaction.sanitized_reason(stripped, max_chars=220)


def _inbox_entries(service: ExperimentService, limit: int) -> list[MailboxMessageEntry]:
    rows = mailbox_module.list_recent_db_messages(service._conn, limit=limit)
    entries = []
    for row in rows:
        preview = _mailbox_preview(mailbox_module.read_inbox_preview(row.get("file_path")))
        entries.append(
            MailboxMessageEntry(
                id=f"msg-{row['id']}",
                timestamp=row.get("received_at") or "",
                type=row.get("msg_type") or "unknown",
                status=row.get("status") or "unknown",
                sender=_safe_mailbox_sender(row.get("created_by")),
                title=_mailbox_type_label(row.get("msg_type")),
                preview=preview,
            )
        )
    return entries


def _outbox_entries(limit: int) -> list[MailboxMessageEntry]:
    files = mailbox_module.list_outbox_files(_OUTBOX_DIR, limit=limit)
    entries = []
    for item in files:
        meta = item.get("meta") or {}
        msg_type = meta.get("type")
        entries.append(
            MailboxMessageEntry(
                id=item["filename"],
                timestamp=meta.get("processed_at") or meta.get("created_at") or "",
                type=msg_type or "unknown",
                status=meta.get("status") or "unknown",
                sender=_safe_mailbox_sender(meta.get("created_by") or meta.get("sender")),
                title=_mailbox_type_label(msg_type),
                preview=_mailbox_preview(item.get("body")),
            )
        )
    return entries


@app.get("/mailbox/messages", response_model=MailboxMessagesResponse)
def list_mailbox_messages(
    limit: int = Query(default=MAILBOX_MESSAGES_DEFAULT_LIMIT, ge=LIMIT_MIN, le=MAILBOX_MESSAGES_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> MailboxMessagesResponse:
    inbox = _inbox_entries(service, limit)
    outbox = _outbox_entries(limit)
    return MailboxMessagesResponse(
        inbox_count=len(inbox),
        outbox_count=len(outbox),
        inbox=inbox,
        outbox=outbox,
    )


# ---------------------------------------------------------------------------
# Experiment and activity log endpoints (public allowlisted + authenticated)
#
# Thin routes over the existing ExperimentService/run_store persistence;
# no experiment orchestration or new SQLite access is added here.
# ---------------------------------------------------------------------------


@app.get("/experiments", response_model=ExperimentsResponse)
def list_experiments(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=EXPERIMENTS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=EXPERIMENTS_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentsResponse:
    authorize(x_api_key)
    experiments = service.list_experiments(limit=limit)
    return ExperimentsResponse(
        count=len(experiments),
        experiments=[exp.to_dict() for exp in experiments],
    )


@app.get("/experiments/{experiment_id}", response_model=ExperimentDetail)
def get_experiment(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentDetail:
    authorize(x_api_key)
    try:
        experiment = service.resolve_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    adoption_service = _resolve_experiment_adoption_service()
    adoption = adoption_service.get(experiment.id)
    payload = experiment.to_dict()
    if experiment.test_exit_code not in (None, 0):
        payload["test_failure_tail"] = redaction.test_failure_tail(experiment.test_output)
    if adoption.status != "not_started":
        payload["adoption"] = adoption.to_dict()
    return ExperimentDetail(**payload)


@app.post("/experiments/{experiment_id}/cancel", response_model=ExperimentDetail)
def cancel_experiment(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    x_cancelled_by: str | None = Header(default=None),
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentDetail:
    authorize(x_api_key)
    try:
        experiment = service.cancel_experiment(experiment_id, x_cancelled_by or "api")
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    except ExperimentNotCancellableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ExperimentDetail(**experiment.to_dict())


_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}

# Coarse groupings of a structured JSONL entry's ``phase`` into the
# ``source`` shown by the unified Logs timeline, independent of the
# systemd journal sources (``agent-api``/``nexus-mailbox``/``llama-server``)
# merged in by ``GET /logs``. Experiment id / mailbox task id / job id
# remain available as optional detail/filter fields on every entry;
# ``source`` is only the primary at-a-glance grouping.
_MAILBOX_SOURCE_PHASES = {
    "mailbox",
    "queued",
    # Entries written before the phase for mailbox loggers was corrected
    # (see modules/logging_config._phase_for_logger) carry the logger's
    # module name as their phase. Recognizing those here reclassifies the
    # existing history as `mailbox` on read, with no migration.
    "mailbox_processor",
    "poll_runner",
    "processing",
    "retention",
}
_CONTROLLER_SOURCE_PHASES = {
    "research", "selection", "selection_reformat_retry", "selection_policy_replan",
}


def _derive_log_source(phase: str | None) -> str:
    if phase in _MAILBOX_SOURCE_PHASES:
        return "mailbox"
    if phase in _CONTROLLER_SOURCE_PHASES:
        return "controller"
    if phase == "chat":
        return "chat"
    if phase == "system":
        return "system"
    return "experiment"


def _apply_log_source(raw_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for raw in raw_entries:
        raw["source"] = _derive_log_source(raw.get("phase"))
    return raw_entries


@app.get("/experiments/{experiment_id}/logs", response_model=ExperimentLogsResponse)
def get_experiment_logs(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    level: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=EXPERIMENT_LOGS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=EXPERIMENT_LOGS_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentLogsResponse:
    """Single, chronological, structured view of one experiment's complete
    task/experiment run - mailbox approval through research, selection,
    implementation, verification, and cleanup - reusing the same durable
    JSONL log written by ``modules.central_logging``, merged with its
    correlated mailbox task's pre-experiment-id log entries if any.
    """
    authorize(x_api_key)
    try:
        experiment = service.resolve_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")

    if level is not None and level.upper() not in _LOG_LEVELS:
        raise HTTPException(status_code=400, detail="invalid level filter")

    mailbox_task_id = _mailbox_task_id(service, experiment.id)
    try:
        # Fetch one extra entry to detect whether an older page remains,
        # without re-scanning already-returned entries on the next call.
        raw_entries = central_logging_module.read_experiment_logs(
            service.experiment_log_dir, experiment.id,
            mailbox_task_id=mailbox_task_id, level=level, phase=phase,
            limit=limit + 1, before=cursor,
        )
    except OSError as exc:
        _logger.error("experiment log read failed: %s", _sanitized_reason(str(exc)))
        raise HTTPException(status_code=500, detail="log storage is unreadable") from exc

    raw_entries, next_cursor = _paginate_log_entries(raw_entries, limit)
    _apply_log_source(raw_entries)

    entries: list[ExperimentLogEntry] = []
    for raw in raw_entries:
        try:
            entries.append(ExperimentLogEntry(**raw))
        except Exception:
            # A malformed entry must not fail the whole response.
            continue
    return ExperimentLogsResponse(
        count=len(entries), experiment_id=experiment.id, entries=entries, next_cursor=next_cursor,
    )


# Per-service line count merged from each journalctl unit into every
# unfiltered-by-id page of GET /logs; bounded so one manual refresh (or
# "load older" call) cannot turn into an unbounded read across three
# services. ``modules.log_reader.LogReader.tail_journal_entries`` also
# normalizes each journal entry's timestamp into the same string shape
# structured entries use and attaches the same ``_source``/``_rseq`` tie
# -break keys, so ``modules.central_logging.entry_sort_key``/
# ``encode_log_cursor``/``decode_log_cursor`` can order and paginate
# both kinds of entries together with no journal-specific logic here.
_UNIFIED_LOGS_JOURNAL_LINES = 100


@app.get("/logs", response_model=UnifiedLogsResponse)
async def get_unified_logs(
    level: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    source: str | None = Query(default=None),
    search: str | None = Query(default=None),
    experiment_id: str | None = Query(default=None),
    mailbox_task_id: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=UNIFIED_LOGS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=UNIFIED_LOGS_MAX_LIMIT),
    include_journal: bool = Query(default=True),
    service: ExperimentService = Depends(get_experiment_service),
    log_reader: LogReader = Depends(get_log_reader),
) -> UnifiedLogsResponse:
    """One complete, chronological Nexus log timeline.

    Unlike ``/activity`` (lifecycle/status summaries only), this returns
    the real structured JSONL entries written by ``modules.central_logging``
    - mailbox intake, queueing, research/selection (``controller``),
    implementation, verification, GitHub lifecycle, adoption, chat jobs,
    and failures/timeouts - merged in chronological order from every
    ``<experiment_log_dir>/*.jsonl`` file, including ``task-<mailbox_task_id>``
    files that hold events recorded before an experiment id existed. On
    every unfiltered-by-id page (with or without a cursor), this is
    additionally merged with the ``agent-api``/``nexus-mailbox``/
    ``llama-server`` systemd journals so the whole runtime is visible in
    one list without picking an experiment or a log section first, and
    "load older" walks back into each journal's real history (via
    ``--until``) rather than only ever re-reading the same fixed newest
    window. Every entry carries a coarse ``source`` (``mailbox``/
    ``controller``/``experiment``/``chat``/``system``, or the journal's
    service name) usable as an optional filter. A running/new experiment
    is included as soon as it has written any log line, with no
    dependency on the public diagnostics list or a terminal status.
    Bounded reads and one cursor-based pagination scheme spanning both
    structured and journal entries (see *cursor*/*next_cursor*) keep one
    manual refresh - and "load older" - from re-scanning unbounded
    historical data while never re-showing (or permanently skipping) an
    entry across pages; if a journal source cannot be read, its failure
    is reported in ``source_errors`` rather than emptying the whole feed.
    """
    if level is not None and level.upper() not in _LOG_LEVELS:
        raise HTTPException(status_code=400, detail="invalid level filter")

    resolved_experiment_id = experiment_id
    if experiment_id:
        try:
            resolved_experiment_id = service.resolve_experiment(experiment_id).id
        except ExperimentNotFoundError:
            return UnifiedLogsResponse(count=0, entries=[])

    resolved_mailbox_task_id = mailbox_task_id
    if resolved_experiment_id and not resolved_mailbox_task_id:
        # Correlate the experiment with its mailbox task log automatically,
        # same as the single-experiment endpoint, so filtering the unified
        # feed down to one experiment shows the same complete task path.
        resolved_mailbox_task_id = _mailbox_task_id(service, resolved_experiment_id)

    try:
        # Fetch one extra entry to detect whether an older page remains,
        # without re-scanning already-returned entries on the next call.
        # This is already a correct upper bound even after merging in
        # journal entries below: the final page can never need more than
        # `limit + 1` structured entries regardless of how many extra
        # journal candidates are merged in alongside them.
        scan = central_logging_module.scan_log_entries(
            service.experiment_log_dir,
            experiment_id=resolved_experiment_id,
            mailbox_task_id=resolved_mailbox_task_id,
            job_id=job_id,
            session_id=session_id,
            level=level,
            phase=phase,
            search=search,
            limit=limit + 1,
            before=cursor,
        )
    except OSError as exc:
        _logger.error("unified log read failed: %s", _sanitized_reason(str(exc)))
        raise HTTPException(status_code=500, detail="log storage is unreadable") from exc

    raw_entries = scan.entries
    _apply_log_source(raw_entries)

    # The journal is only merged into the unfiltered-by-id feed, so
    # filtering down to one experiment/task/job never mixes in unrelated
    # systemd noise it cannot be correlated to. Unlike the structured
    # feed, it is merged on *every* such page (a cursor no longer opts
    # it out) - otherwise a page beyond the first could never surface
    # any journal entry at all, no matter how recent.
    source_errors: dict[str, str] | None = None
    merged_entries = raw_entries
    is_default_page = (
        not resolved_experiment_id and not resolved_mailbox_task_id
        and not job_id and not session_id
    )
    if include_journal and is_default_page:
        decoded_cursor = central_logging_module.decode_log_cursor(cursor) if cursor else None
        journal_before: str | None = decoded_cursor[0] if decoded_cursor else None
        # Reuses the cursor's own timestamp component as the journal
        # page boundary; an unrecognized/legacy cursor simply omits
        # `--until` (falls back to each service's newest window)
        # rather than risking an incorrectly-scoped, possibly
        # duplicate-producing query.
        journal_entries, errors = await log_reader.read_all_journal_entries(
            lines=_UNIFIED_LOGS_JOURNAL_LINES, before=journal_before, search=search,
        )
        if decoded_cursor is not None:
            # `journalctl --until` only has second precision and is
            # inclusive, so it can legitimately return an entry again
            # that was already on the previous page (e.g. two journal
            # lines within the same second straddling the cursor
            # boundary). Re-apply the exact same strict, sub-second
            # cursor comparison the structured feed already uses
            # (`_entry_sort_key(e) < cursor_key` in
            # ``central_logging.read_log_entries``) so correctness never
            # depends on journalctl's coarser boundary semantics.
            journal_entries = [
                e for e in journal_entries
                if central_logging_module.entry_sort_key(e) < decoded_cursor
            ]
        if level:
            journal_entries = [e for e in journal_entries if e.get("level") == level.upper()]
        if phase:
            journal_entries = [e for e in journal_entries if e.get("phase") == phase]
        merged_entries = raw_entries + journal_entries
        if errors:
            source_errors = errors

    if source:
        wanted = source.strip().lower()
        merged_entries = [e for e in merged_entries if str(e.get("source", "")).lower() == wanted]
    # `search` is deliberately *not* re-applied here: it is pushed down
    # into both readers (the JSONL scan and `journalctl --grep`), so it
    # searches the whole retained history. Filtering here as well would
    # re-introduce the original bug's blind spot - matches would again be
    # limited to whatever one page happened to contain.

    # One shared sort + cursor scheme (see `_UNIFIED_LOGS_JOURNAL_LINES`
    # above) spans structured and journal entries alike, so the
    # `next_cursor` returned below reflects the true oldest entry of the
    # *merged* page - never just the structured feed's own boundary -
    # and no entry is ever shown twice or skipped across pages.
    merged_entries.sort(key=central_logging_module.entry_sort_key)
    merged_entries, next_cursor = _paginate_log_entries(merged_entries, limit)

    entries: list[ExperimentLogEntry] = []
    for raw in merged_entries:
        try:
            entries.append(ExperimentLogEntry(**raw))
        except Exception:
            # A malformed entry must not fail the whole response.
            continue
    return UnifiedLogsResponse(
        count=len(entries), entries=entries, next_cursor=next_cursor,
        source_errors=source_errors, truncated=scan.truncated,
    )


@app.get("/logs/trace/{reference:path}", response_model=LogTraceResponse)
def get_log_trace(
    reference: str,
    limit: int = Query(default=UNIFIED_LOGS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=UNIFIED_LOGS_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> LogTraceResponse:
    """Everything Nexus did for one order, from any reference to it.

    ``GET /logs`` answers "what happened recently"; this answers "Nexus
    was given this - what did it actually do?". *reference* can be an
    experiment id, a mailbox task id, a controller session or chat job
    id, an inbox message filename, or any distinctive fragment (part of
    an error, part of a command): the resolver finds whatever entries
    that identifies, reads the correlation ids off them, and pulls in
    everything sharing those ids, so the whole chain - mailbox intake,
    approval, controller decisions, implementation, verification, result
    - comes back from any single point in it.

    An unresolvable reference returns ``found: false`` with an empty
    timeline rather than a 404: "I looked and found nothing" is a useful
    answer here, and is not the same as a broken route.
    """
    try:
        trace = log_trace_module.resolve_trace(
            service.experiment_log_dir, reference, limit=limit,
        )
    except OSError as exc:
        _logger.error("log trace read failed: %s", _sanitized_reason(str(exc)))
        raise HTTPException(status_code=500, detail="log storage is unreadable") from exc

    raw_entries = _apply_log_source(trace.entries)
    entries: list[ExperimentLogEntry] = []
    for raw in raw_entries:
        try:
            entries.append(ExperimentLogEntry(**raw))
        except Exception:
            # A malformed entry must not fail the whole trace.
            continue

    return LogTraceResponse(
        reference=reference,
        found=trace.found,
        correlation_ids=trace.correlation_ids,
        summary=log_trace_module.summarize(raw_entries),
        count=len(entries),
        entries=entries,
        truncated=trace.truncated,
    )


@app.post("/experiments/{experiment_id}/adopt", response_model=ExperimentAdoptionStatus, status_code=202)
def adopt_experiment(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    service: ExperimentService = Depends(get_experiment_service),
    adoption_service: ExperimentAdoptionService = Depends(get_experiment_adoption_service),
) -> ExperimentAdoptionStatus:
    authorize(x_api_key)
    try:
        experiment = service.resolve_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    try:
        adoption = adoption_service.start_adoption(experiment.id)
    except ExperimentAdoptionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return ExperimentAdoptionStatus(**adoption.to_dict())


@app.get("/experiments/{experiment_id}/adoption", response_model=ExperimentAdoptionStatus)
def get_experiment_adoption(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    service: ExperimentService = Depends(get_experiment_service),
    adoption_service: ExperimentAdoptionService = Depends(get_experiment_adoption_service),
) -> ExperimentAdoptionStatus:
    authorize(x_api_key)
    try:
        experiment = service.resolve_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    # Chosen contract: never 404 just because adoption was not started yet.
    payload = adoption_service.get(experiment.id).to_dict()
    payload["previous_failures"] = adoption_service.failure_history(experiment.id)
    return ExperimentAdoptionStatus(**payload)


@app.get("/operations/catalog", response_model=OperationsCatalogResponse)
def get_operations_catalog(
    x_api_key: str | None = Header(default=None),
    operations_service: OperationsRegistry = Depends(get_operations_service),
) -> OperationsCatalogResponse:
    authorize(x_api_key)
    operations = [OperationCatalogEntry(**entry.to_dict()) for entry in operations_service.catalog()]
    return OperationsCatalogResponse(count=len(operations), operations=operations)


@app.post("/operations/execute", response_model=OperationsExecuteResponse)
def execute_operation(
    request: OperationsExecuteRequest,
    x_api_key: str | None = Header(default=None),
    operations_service: OperationsRegistry = Depends(get_operations_service),
) -> OperationsExecuteResponse:
    authorize(x_api_key)
    try:
        result = operations_service.execute(
            request.operation,
            request.args.model_dump(exclude_none=True),
            invocation_id=request.invocation_id,
        )
    except OperationValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return OperationsExecuteResponse(**result.to_dict())


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


def _paginate_log_entries(
    raw_entries: list[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Split a *raw_entries* list fetched with ``limit + 1`` into exactly
    ``limit`` entries to return plus the ``next_cursor`` for the
    following (older) page, if any.

    ``raw_entries`` must be chronological (oldest first), which is what
    every ``read_*`` function in ``modules.central_logging`` returns.
    The page boundary is anchored to the oldest entry actually *returned*
    on this page - not the extra look-ahead entry used only to detect
    whether more data remains - and encoded as an opaque cursor (see
    ``encode_log_cursor``) rather than a bare timestamp, so that entries
    sharing an identical timestamp at a page boundary are never skipped
    or duplicated across successive manual "load older" calls.
    """
    if len(raw_entries) <= limit:
        return raw_entries, None
    page = raw_entries[-limit:] if limit > 0 else []
    next_cursor = central_logging_module.encode_log_cursor(page[0]) if page else None
    return page, next_cursor


def _public_experiment_usage(experiment) -> dict[str, int | None] | None:
    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
    totals: dict[str, int | None] = {field: None for field in fields}
    for usage in (
        experiment.research_usage,
        experiment.selection_usage,
        experiment.reformat_retry_usage,
        experiment.policy_replan_usage,
        experiment.implementation_usage,
    ):
        if not isinstance(usage, dict):
            continue
        for field in fields:
            value = usage.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[field] = (totals[field] or 0) + int(value)
    return totals if any(value is not None for value in totals.values()) else None


def _public_experiment(experiment, mailbox_task_id: str | None) -> PublicExperimentSummary:
    # Strict allowlist: never derive this response from to_dict/model_dump.
    return PublicExperimentSummary(
        id=experiment.id,
        mailbox_task_id=mailbox_task_id,
        phase=_public_phase(experiment.status),
        status=experiment.status,
        created_at=experiment.created_at,
        ended_at=experiment.ended_at,
        failure_reason=_failure_reason(experiment),
        test_exit_code=experiment.test_exit_code,
        changed_files=_changed_file_names(experiment.git_status),
        test_summary=_test_summary(experiment),
        usage=_public_experiment_usage(experiment),
    )


@app.get("/experiments/{experiment_id}/public", response_model=PublicExperimentSummary)
def get_public_experiment(
    experiment_id: str,
    service: ExperimentService = Depends(get_experiment_service),
) -> PublicExperimentSummary:
    try:
        experiment = service.resolve_public_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    return _public_experiment(experiment, _mailbox_task_id(service, experiment.id))


_SECRET_PATTERN = redaction._SECRET_PATTERN
_PATH_PATTERN = redaction._PATH_PATTERN


def _redact(text: str) -> str:
    return redaction.redact(text)


def _diagnostic_lines(raw: str | None) -> list[str]:
    """Return bounded, redacted test diagnostics; never return raw output."""
    return redaction.bounded_lines(raw)


def _sanitized_rejection_reason(raw: str | None) -> str | None:
    """Bounded, redacted single-line rejection/error reason for diagnostics."""
    return redaction.sanitized_reason(raw)



@app.get("/activity/public", response_model=PublicActivityResponse)
def list_public_activity(
    service: ExperimentService = Depends(get_experiment_service),
) -> PublicActivityResponse:
    result = []
    for entry in service.list_activity(limit=100):
        try:
            experiment = service.get_experiment(entry["entity_id"])
        except ExperimentNotFoundError:
            continue
        status = entry.get("to_status") or experiment.status
        result.append({
            "timestamp": entry["timestamp"],
            "phase": _public_phase(status),
            "status": status,
            "message": f"Experiment phase is {_public_phase(status)}.",
            "mailbox_task_id": _mailbox_task_id(service, experiment.id),
            "experiment_id": experiment.id,
            "failure_reason": _failure_reason(experiment) if status in _PUBLIC_PHASES and _public_phase(status) in {"failed", "needs_review"} else None,
            "test_exit_code": experiment.test_exit_code,
            "changed_files": _changed_file_names(experiment.git_status),
            "test_summary": _test_summary(experiment),
        })
    return PublicActivityResponse(count=len(result), activity=result)


@app.get("/activity", response_model=DiagnosticActivityResponse)
def list_activity(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=ACTIVITY_DEFAULT_LIMIT, ge=LIMIT_MIN, le=ACTIVITY_MAX_LIMIT),
    experiment_id: str | None = Query(default=None),
    mailbox_task_id: str | None = Query(default=None),
    service: ExperimentService = Depends(get_experiment_service),
) -> DiagnosticActivityResponse:
    authorize(x_api_key)
    if mailbox_task_id:
        approval_service = _resolve_mailbox_task_service()
        try:
            approval = approval_service.get(mailbox_task_id)
        except ApprovalNotFoundError:
            return DiagnosticActivityResponse(count=0, activity=[])
        if experiment_id and approval.experiment_id != experiment_id:
            return DiagnosticActivityResponse(count=0, activity=[])
        experiment_id = approval.experiment_id
        if not experiment_id:
            return DiagnosticActivityResponse(count=0, activity=[])
    entries = service.list_activity(limit=limit, experiment_id=experiment_id)
    diagnostics = []
    for entry in entries:
        experiment = service.get_experiment(entry["entity_id"])
        diagnostics.append({
            **entry,
            "message": None,
            "mailbox_task_id": _mailbox_task_id(service, experiment.id),
            "phase": _public_phase(entry.get("to_status") or experiment.status),
            "policy_rule": experiment.policy_rule,
            "rejection_reason": _sanitized_rejection_reason(experiment.error),
            "test_command": experiment.test_command,
            "test_exit_code": experiment.test_exit_code,
            "changed_files": _changed_file_names(experiment.git_status),
            "test_output_lines": _diagnostic_lines(experiment.test_output),
        })
    return DiagnosticActivityResponse(count=len(diagnostics), activity=diagnostics)


# ---------------------------------------------------------------------------
# GET /diagnostics/public — single public, strictly allowlisted endpoint for
# remote diagnosis. Built exclusively from the explicit allowlist below;
# never derived from a generic to_dict()/model_dump() of internal records.
#
# Excluded by construction (never touched by this handler): API keys,
# tokens, credentials, headers, prompts/task bodies, environment variables,
# source code/diffs/file contents, stack traces with internal paths, and
# unrestricted/raw command output.
# ---------------------------------------------------------------------------

DIAGNOSTICS_DEFAULT_LIMIT = 10
DIAGNOSTICS_MAX_LIMIT = 100

PUBLIC_LOGS_DEFAULT_LIMIT = 100
PUBLIC_LOGS_MAX_LIMIT = 500


@lru_cache(maxsize=1)
def _git_revision() -> str | None:
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


@lru_cache(maxsize=1)
def _deployment_time() -> str | None:
    """Process start time, used as a proxy for 'when this build was deployed'."""
    try:
        return datetime.fromtimestamp(_PROCESS_START_TIME, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except Exception:
        return None


def _phase_duration_ms(started_at: str | None, ended_at: str | None) -> int | None:
    if not started_at or not ended_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        return max(0, int((end - start).total_seconds() * 1000))
    except (ValueError, TypeError):
        return None


def _experiment_lifecycle(experiment) -> list[dict[str, Any]]:
    phases = (
        ("research", experiment.research_started_at, experiment.research_ended_at),
        ("selecting", experiment.selection_started_at, experiment.selection_ended_at),
        ("implementing", experiment.implementation_started_at, experiment.implementation_ended_at),
    )
    lifecycle = []
    for name, started_at, ended_at in phases:
        if not started_at and not ended_at:
            continue
        lifecycle.append({
            "phase": name,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": _phase_duration_ms(started_at, ended_at),
        })
    return lifecycle


def _aggregate_usage(experiment) -> dict[str, int] | None:
    """Sum token usage across all phases; never returns raw prompts/messages."""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    found = False
    for usage in (
        experiment.research_usage,
        experiment.selection_usage,
        experiment.reformat_retry_usage,
        experiment.policy_replan_usage,
        experiment.implementation_usage,
    ):
        if not usage:
            continue
        found = True
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        totals["total_tokens"] += int(usage.get("total_tokens", 0) or (input_tokens + output_tokens))
    return totals if found else None


def _estimated_cost(usage: dict[str, int] | None) -> dict[str, Any] | None:
    """Best-effort estimated cost from configured model prices; omitted when unavailable."""
    if not usage:
        return None
    provider = os.environ.get("AGENT_COST_PROVIDER", "azure")
    model = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    try:
        conn = get_experiment_service()._conn  # same shared connection/db as run_store
        price = get_applicable_price(conn, provider, model, datetime.now(timezone.utc).isoformat())
    except Exception:
        price = None
    if not price:
        return {"status": "unavailable"}
    cost = calculate_estimated_cost(
        usage["input_tokens"],
        usage["output_tokens"],
        price["input_price_per_million_tokens"],
        price["output_price_per_million_tokens"],
    )
    return {"status": "ok", "amount": round(cost, 6), "currency": price["currency"]}


def _diagnostic_experiment(service: ExperimentService, experiment) -> PublicDiagnosticExperiment:
    diagnostic_messages = []
    for entry in service.list_activity(limit=20, experiment_id=experiment.id):
        message = redaction.sanitized_reason(entry.get("message"))
        if message:
            diagnostic_messages.append(message)

    usage = _aggregate_usage(experiment)
    return PublicDiagnosticExperiment(
        id=experiment.id,
        mailbox_task_id=_mailbox_task_id(service, experiment.id),
        phase=_public_phase(experiment.status),
        status=experiment.status,
        created_at=experiment.created_at,
        ended_at=experiment.ended_at,
        lifecycle=_experiment_lifecycle(experiment),
        rejection_reason=redaction.sanitized_reason(experiment.error),
        policy_rule=experiment.policy_rule,
        test_command=redaction.test_command_name(experiment.test_command),
        test_exit_code=experiment.test_exit_code,
        failure_summary=_failure_reason(experiment),
        changed_files=_changed_file_names(experiment.git_status),
        diagnostic_messages=diagnostic_messages[:20],
        usage=usage,
        estimated_cost=_estimated_cost(usage),
    )


def _diagnostic_mailbox_task(approval) -> PublicDiagnosticMailboxTask:
    return PublicDiagnosticMailboxTask(
        id=approval.id,
        status=approval.status,
        received_at=approval.received_at,
        experiment_id=approval.experiment_id,
    )


# ---------------------------------------------------------------------------
# Operation Chain endpoints (authenticated, sequential operation execution)
# ---------------------------------------------------------------------------


@app.post("/operations/chains", response_model=OperationChainResult, status_code=202)
def start_operation_chain(
    request: OperationChainRequest,
    x_api_key: str | None = Header(default=None),
    service: OperationChainService = Depends(get_operation_chain_service),
) -> OperationChainResult:
    authorize(x_api_key)
    try:
        result = service.start_chain(request)
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid chain: {str(exc)}")


@app.get("/operations/chains/{request_id}", response_model=OperationChainResult)
def get_operation_chain(
    request_id: str,
    x_api_key: str | None = Header(default=None),
    service: OperationChainService = Depends(get_operation_chain_service),
) -> OperationChainResult:
    authorize(x_api_key)
    result = service.get_chain(request_id)
    if not result:
        raise HTTPException(status_code=404, detail="Chain not found")
    return result


@app.get("/operations/chains", response_model=list[OperationChainResult])
def list_operation_chains(
    limit: int = Query(default=OPERATION_CHAINS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=OPERATION_CHAINS_MAX_LIMIT),
    x_api_key: str | None = Header(default=None),
    service: OperationChainService = Depends(get_operation_chain_service),
) -> list[OperationChainResult]:
    authorize(x_api_key)
    return service.list_chains(limit=limit)


@app.get("/diagnostics/public", response_model=PublicDiagnosticsResponse)
def get_public_diagnostics(
    limit: int = Query(default=DIAGNOSTICS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=DIAGNOSTICS_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
    mailbox_service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> PublicDiagnosticsResponse:
    experiments = service.list_experiments(limit=limit)
    diagnostic_experiments = [_diagnostic_experiment(service, exp) for exp in experiments]

    try:
        pending = mailbox_service.list_pending(limit=200)
        recent = mailbox_service.list_recent(limit=limit)
        mailbox_status = PublicDiagnosticMailboxStatus(
            status="ok",
            pending_count=len(pending),
            latest_tasks=[_diagnostic_mailbox_task(a) for a in recent],
        )
    except Exception:
        mailbox_status = PublicDiagnosticMailboxStatus(status="unavailable", pending_count=0, latest_tasks=[])

    return PublicDiagnosticsResponse(
        service=PublicDiagnosticServiceStatus(
            status="ok",
            git_revision=_git_revision(),
            deployment_time=_deployment_time(),
        ),
        mailbox=mailbox_status,
        experiments=diagnostic_experiments,
        problems=list_problems(),
    )



# ---------------------------------------------------------------------------
# Failure analysis (#145) — manual/debug inspection of stored diagnoses,
# and an endpoint to trigger one on demand. The automatic integrations live
# in modules/mailbox/task_service.py and
# modules/mailbox/finalizing_task_service.py; this surface is for humans
# (or scripts) inspecting/requesting an analysis directly.
# ---------------------------------------------------------------------------

FAILURE_ANALYSES_DEFAULT_LIMIT = 20
FAILURE_ANALYSES_MAX_LIMIT = 100


@app.get("/diagnostics/nexus/liveness", response_model=NexusLivenessResponse)
def get_nexus_liveness(
    x_api_key: str | None = Header(default=None),
    stale_after_seconds: int | None = Query(default=None, ge=0),
    store: NexusTaskStore = Depends(get_nexus_task_store),
) -> NexusLivenessResponse:
    """Who owns which controller work, and when it last reported itself alive.

    Read-only: it reports staleness but never reaps, so an operator can look at
    a suspect session without changing its state. ``stale_after_seconds`` only
    reframes the question - the store clamps it to the same safe floor the
    reaper uses, so no query can make live work look dead (#162).
    """
    authorize(x_api_key)
    snapshot = store.liveness(
        stale_after_seconds if stale_after_seconds else _STALE_HEARTBEAT_SECONDS
    )
    return NexusLivenessResponse(
        **{**snapshot, "claims": [NexusSessionClaim(**c) for c in snapshot["claims"]]}
    )


# ---------------------------------------------------------------------------
# Nexus diagnostics (#188) — first-class, read-only answers to "what happened
# to this work?", so diagnosing a failure never again means composing ad-hoc
# shell commands through the mailbox. Every route here resolves a reference
# through the one shared backend (modules/nexus_diagnostics.py), which the
# operations catalog and the failure-analysis routine also use.
# ---------------------------------------------------------------------------

DIAGNOSTIC_LOG_DEFAULT_LINES = nexus_diagnostics_module.DEFAULT_LOG_LINES
DIAGNOSTIC_LOG_MAX_LINES = nexus_diagnostics_module.MAX_LOG_LINES


def get_nexus_diagnostics() -> Iterator[nexus_diagnostics_module.NexusDiagnostics]:
    """Read-only diagnostic backend, opened and closed per request.

    Per-request like ``get_nexus_task_store``, and for the same reason: this
    serves occasional diagnostic reads, and a process-lifetime handle for them
    would buy nothing. Overridable in tests via
    ``app.dependency_overrides[get_nexus_diagnostics]``.
    """
    try:
        diagnostics = nexus_diagnostics_module.NexusDiagnostics(
            log_dir=_resolve_experiment_service().experiment_log_dir,
            stale_after_seconds=_STALE_HEARTBEAT_SECONDS,
        )
    except nexus_diagnostics_module.DiagnosticsError as exc:
        # The backend opens the database read-only and never creates it, so a
        # deployment that has not written one yet has nothing to diagnose.
        # That is a service condition with a plain cause, not a 500.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        yield diagnostics
    finally:
        diagnostics.close()


def _diagnostic_report_response(
    report: nexus_diagnostics_module.DiagnosticReport,
) -> DiagnosticReportResponse:
    return DiagnosticReportResponse(**report.to_dict())


@app.get("/diagnostics/logs/service/{service}", response_model=DiagnosticServiceLogsResponse)
async def get_diagnostic_service_logs(
    service: str,
    x_api_key: str | None = Header(default=None),
    lines: int = Query(
        default=nexus_diagnostics_module.DEFAULT_SERVICE_LINES,
        ge=LIMIT_MIN,
        le=DIAGNOSTIC_LOG_MAX_LINES,
    ),
    since_minutes: int | None = Query(default=None, ge=1, le=60 * 24 * 7),
    diagnostics: nexus_diagnostics_module.NexusDiagnostics = Depends(get_nexus_diagnostics),
    log_reader: LogReader = Depends(get_log_reader),
) -> DiagnosticServiceLogsResponse:
    """Recent logs for one Nexus service, without constructing ``journalctl``.

    *service* may be a friendly name (``api``, ``mailbox``, ``llm``) or a unit
    name; anything unrecognized is validated as a unit name before it is used,
    so no caller can smuggle a shell fragment through this route.
    """
    authorize(x_api_key)
    diagnostics.log_reader = log_reader
    try:
        result = await diagnostics.service_logs(
            service, lines=lines, since_minutes=since_minutes
        )
    except nexus_diagnostics_module.DiagnosticsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DiagnosticServiceLogsResponse(**result.to_dict())


@app.get("/diagnostics/logs/last-failure", response_model=DiagnosticReportResponse)
def get_diagnostic_last_failure(
    x_api_key: str | None = Header(default=None),
    lines: int = Query(
        default=DIAGNOSTIC_LOG_DEFAULT_LINES, ge=LIMIT_MIN, le=DIAGNOSTIC_LOG_MAX_LINES
    ),
    scan: int = Query(
        default=nexus_diagnostics_module.DEFAULT_LAST_FAILURE_CANDIDATES,
        ge=LIMIT_MIN,
        le=nexus_diagnostics_module.MAX_LAST_FAILURE_CANDIDATES,
    ),
    diagnostics: nexus_diagnostics_module.NexusDiagnostics = Depends(get_nexus_diagnostics),
) -> DiagnosticReportResponse:
    """The most recent failed *or stalled* work, with its error context.

    Stalled counts: a process that died mid-run never wrote a ``failed`` row,
    so a search for explicit failures alone would miss exactly the case this
    endpoint exists for (#186/#187).
    """
    authorize(x_api_key)
    try:
        return _diagnostic_report_response(diagnostics.last_failure(lines=lines, scan=scan))
    except nexus_diagnostics_module.DiagnosticsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/diagnostics/llm-jobs/{job_id}", response_class=PlainTextResponse)
def get_diagnostic_llm_job(
    job_id: str,
    x_api_key: str | None = Header(default=None),
    view: str = Query(default="summary", pattern="^(summary|result|prompt)$"),
    diagnostics: nexus_diagnostics_module.NexusDiagnostics = Depends(get_nexus_diagnostics),
) -> str:
    """``nexus debug llm-job <id> [--result|--prompt]`` (#259), read-only.

    Plain ``key: value`` lines, the same the ``nexus_llm_job`` operation
    returns, so the output reads directly in a terminal.
    """
    authorize(x_api_key)
    try:
        job = diagnostics.llm_job(job_id)
    except nexus_diagnostics_module.DiagnosticsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return "\n".join(nexus_diagnostics_module.render_llm_job(job, view=view)) + "\n"


@app.get("/diagnostics/status/{reference:path}", response_model=DiagnosticReportResponse)
def get_diagnostic_status(
    reference: str,
    x_api_key: str | None = Header(default=None),
    diagnostics: nexus_diagnostics_module.NexusDiagnostics = Depends(get_nexus_diagnostics),
) -> DiagnosticReportResponse:
    """Lifecycle state for one piece of work, with no log bodies.

    *reference* may be a mailbox task id, an experiment id, a controller
    session id, an issue or PR number (``188``, ``#188``, ``PR #195``), an
    inbox message filename, or a unique prefix of any id.
    """
    authorize(x_api_key)
    try:
        return _diagnostic_report_response(diagnostics.status_report(reference))
    except nexus_diagnostics_module.DiagnosticsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/diagnostics/logs/{reference:path}", response_model=DiagnosticReportResponse)
def get_diagnostic_logs(
    reference: str,
    x_api_key: str | None = Header(default=None),
    lines: int = Query(
        default=DIAGNOSTIC_LOG_DEFAULT_LINES, ge=LIMIT_MIN, le=DIAGNOSTIC_LOG_MAX_LINES
    ),
    diagnostics: nexus_diagnostics_module.NexusDiagnostics = Depends(get_nexus_diagnostics),
) -> DiagnosticReportResponse:
    """Lifecycle state *and* the bounded logs that explain it, for one reference.

    Registered after the fixed ``/diagnostics/logs/service/...`` and
    ``/diagnostics/logs/last-failure`` routes on purpose: a ``:path``
    parameter would otherwise swallow both.
    """
    authorize(x_api_key)
    try:
        return _diagnostic_report_response(diagnostics.diagnose(reference, lines=lines))
    except nexus_diagnostics_module.DiagnosticsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/diagnostics/failure-analyses", response_model=FailureAnalysesResponse)
def list_failure_analyses(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=FAILURE_ANALYSES_DEFAULT_LIMIT, ge=LIMIT_MIN, le=FAILURE_ANALYSES_MAX_LIMIT),
    subject_type: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    service: FailureAnalysisService = Depends(get_failure_analysis_service),
) -> FailureAnalysesResponse:
    authorize(x_api_key)
    entries = service.list_recent(limit=limit, subject_type=subject_type, subject_id=subject_id)
    return FailureAnalysesResponse(count=len(entries), analyses=entries)


@app.get("/diagnostics/failure-analyses/{analysis_id}", response_model=FailureAnalysisEntry)
def get_failure_analysis(
    analysis_id: str,
    x_api_key: str | None = Header(default=None),
    service: FailureAnalysisService = Depends(get_failure_analysis_service),
) -> FailureAnalysisEntry:
    authorize(x_api_key)
    entry = service.get(analysis_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="failure analysis not found")
    return FailureAnalysisEntry(**entry)


@app.post("/diagnostics/failure-analyses/analyze", response_model=FailureAnalysisEntry, status_code=201)
def trigger_failure_analysis(
    request: FailureAnalysisRequest,
    x_api_key: str | None = Header(default=None),
    service: FailureAnalysisService = Depends(get_failure_analysis_service),
) -> FailureAnalysisEntry:
    authorize(x_api_key)
    if not request.experiment_id and not request.mailbox_task_id:
        raise HTTPException(
            status_code=400, detail="experiment_id or mailbox_task_id is required"
        )
    subject_id = request.experiment_id or request.mailbox_task_id
    analysis = service.analyze_from_logs(
        subject_type=request.subject_type,
        subject_id=subject_id,
        experiment_id=request.experiment_id,
        mailbox_task_id=request.mailbox_task_id,
        failure={"reason": request.reason or "manual analysis request"},
    )
    return FailureAnalysisEntry(**analysis.to_dict())


@app.get("/logs/public", response_model=UnifiedLogsResponse)
async def get_public_unified_logs(
    level: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    source: str | None = Query(default=None),
    search: str | None = Query(default=None),
    experiment_id: str | None = Query(default=None),
    mailbox_task_id: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=UNIFIED_LOGS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=UNIFIED_LOGS_MAX_LIMIT),
    include_journal: bool = Query(default=True),
    service: ExperimentService = Depends(get_experiment_service),
    log_reader: LogReader = Depends(get_log_reader),
) -> UnifiedLogsResponse:
    """Backwards-compatible alias of ``GET /logs``.

    Both routes now return the identical, complete timeline: reading logs
    needs neither an API key nor a reduced allowlist (see DECISIONS.md
    D-012 - this is a single-operator experiment VM, and analysis is the
    point of the log surface). Kept as a separate path only so existing
    clients and bookmarks continue to work; it delegates rather than
    maintaining a second read implementation that could drift.
    """
    return await get_unified_logs(
        level=level,
        phase=phase,
        source=source,
        search=search,
        experiment_id=experiment_id,
        mailbox_task_id=mailbox_task_id,
        job_id=job_id,
        session_id=session_id,
        cursor=cursor,
        limit=limit,
        include_journal=include_journal,
        service=service,
        log_reader=log_reader,
    )


# ── Maintenance API ───────────────────────────────────────────────────────────
#
# All /maintenance/* endpoints require:
#   - The normal X-Api-Key header (AGENT_API_KEY)
#   - A second X-Maintenance-Token header (MAINTENANCE_TOKEN)
#   - MAINTENANCE_ENABLED=true (env var)
#   - The current UTC time must be before MAINTENANCE_EXPIRY (if set)
#
# These endpoints are intended as a temporary remote-maintenance bridge for
# PR #21 deployment and must be disabled again after use (unset MAINTENANCE_ENABLED).


@app.get("/maintenance/status", response_model=MaintenanceStatusResponse)
def get_maintenance_status(
    x_api_key: str | None = Header(default=None),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MaintenanceStatusResponse:
    """Return whether the maintenance service is currently enabled."""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    expiry = os.environ.get("MAINTENANCE_EXPIRY") or None
    return MaintenanceStatusResponse(
        enabled=service.is_enabled(),
        expiry=expiry,
    )


@app.post("/maintenance/execute", response_model=MaintenanceExecuteResponse)
def maintenance_execute(
    body: MaintenanceExecuteRequest,
    x_api_key: str | None = Header(default=None),
    x_maintenance_token: str | None = Header(default=None),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MaintenanceExecuteResponse:
    """Execute a named maintenance operation.

    Returns the operation result as a JSON object.  Background operations
    (``run_router_test``, ``run_tests``) return a ``task_id`` that can be
    polled via ``router_task_status`` / ``test_task_status``.
    """
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        result = service.execute(body.operation, x_maintenance_token, body.args)
    except MaintenanceDisabledError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except MaintenanceAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except MaintenanceOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return MaintenanceExecuteResponse(operation=body.operation, result=result)


@app.get("/maintenance/audit", response_model=MaintenanceAuditResponse)
def get_maintenance_audit(
    x_api_key: str | None = Header(default=None),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MaintenanceAuditResponse:
    """Return the in-memory maintenance audit log."""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    entries = service.audit_log
    return MaintenanceAuditResponse(
        entries=[
            {"timestamp": e.timestamp, "operation": e.operation,
             "args": e.args, "outcome": e.outcome}
            for e in entries
        ],
        total=len(entries),
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _op_response(result, operation: str) -> OperationResponse:
    from modules.operations_executor import OperationResult
    return OperationResponse(
        success=result.success,
        status=result.status,
        operation=operation,
        timestamp=result.timestamp,
        data=result.data,
        error=result.error,
        duration_ms=result.duration_ms,
    )


# ---------------------------------------------------------------------------
# Git Operations  (/operations/git/*)
# ---------------------------------------------------------------------------

@app.post("/operations/git/pull", response_model=OperationResponse)
async def operations_git_pull(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("git/pull", {})
    return _op_response(result, "git/pull")


@app.get("/operations/git/status", response_model=OperationResponse)
async def operations_git_status(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("git/status", {})
    return _op_response(result, "git/status")


@app.post("/operations/git/commit-and-push", response_model=OperationResponse)
async def operations_git_commit_push(
    request: GitCommitPushRequest,
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute(
        "git/commit-and-push",
        {"message": request.message, "files": request.files},
    )
    return _op_response(result, "git/commit-and-push")


# ---------------------------------------------------------------------------
# Service Management  (/operations/services/*)
# ---------------------------------------------------------------------------

@app.post("/operations/services/restart", response_model=OperationResponse)
async def operations_services_restart(
    request: ServiceRestartRequest,
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("services/restart", {"service": request.service})
    return _op_response(result, "services/restart")


@app.get("/operations/services/status", response_model=OperationResponse)
async def operations_services_status(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("services/status", {})
    return _op_response(result, "services/status")


@app.post("/operations/services/llama/reload-model", response_model=OperationResponse)
async def operations_llama_reload_model(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("services/llama/reload-model", {})
    return _op_response(result, "services/llama/reload-model")


# ---------------------------------------------------------------------------
# Logging  (/operations/logs/*)
# ---------------------------------------------------------------------------

@app.get("/operations/logs/tail", response_model=OperationResponse)
async def operations_logs_tail(
    service: str = Query(...),
    lines: int = Query(default=50, ge=1, le=500),
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("logs/tail", {"service": service, "lines": lines})
    return _op_response(result, "logs/tail")


@app.get("/operations/logs/errors", response_model=OperationResponse)
async def operations_logs_errors(
    service: str = Query(...),
    since_minutes: int = Query(default=60, ge=1, le=1440),
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute(
        "logs/errors", {"service": service, "since_minutes": since_minutes}
    )
    return _op_response(result, "logs/errors")


# ---------------------------------------------------------------------------
# Self-Healing  (/operations/healing/*)
# ---------------------------------------------------------------------------

@app.post("/operations/healing/fix-locks", response_model=OperationResponse)
async def operations_healing_fix_locks(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("healing/fix-locks", {})
    return _op_response(result, "healing/fix-locks")


@app.post("/operations/healing/cleanup-temps", response_model=OperationResponse)
async def operations_healing_cleanup_temps(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("healing/cleanup-temps", {})
    return _op_response(result, "healing/cleanup-temps")


@app.get("/operations/healing/diagnose", response_model=OperationResponse)
async def operations_healing_diagnose(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("healing/diagnose", {})
    return _op_response(result, "healing/diagnose")


# ---------------------------------------------------------------------------
# Model Management  (/operations/models/*)
# ---------------------------------------------------------------------------

@app.get("/operations/models/status", response_model=OperationResponse)
async def operations_models_status(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("models/status", {})
    return _op_response(result, "models/status")


@app.post("/operations/models/test", response_model=OperationResponse)
async def operations_models_test(
    request: LlamaModelTestRequest,
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute(
        "models/test", {"model": request.model, "prompt": request.prompt}
    )
    return _op_response(result, "models/test")


# ---------------------------------------------------------------------------
# Context / Config  (/operations/context/*)
# ---------------------------------------------------------------------------

@app.get("/operations/context/env", response_model=OperationResponse)
async def operations_context_env(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("context/env", {})
    return _op_response(result, "context/env")


@app.post("/operations/context/set-config", response_model=OperationResponse)
async def operations_context_set_config(
    request: SetConfigRequest,
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute(
        "context/set-config", {"key": request.key, "value": request.value}
    )
    return _op_response(result, "context/set-config")
