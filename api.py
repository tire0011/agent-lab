import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import chat as chat_module
from data_sources import InvalidIdeaTextError, add_idea, list_ideas, list_problems
from modules.codex_task_service import (
    CodexTaskService,
    TaskNotFoundError,
    TaskNotRunnableError,
)
from modules.mailbox_task_service import (
    ApprovalNotFoundError,
    ApprovalStateError,
    MailboxTaskService,
    MailboxTaskWorker,
)
from modules.experiment_service import ExperimentNotFoundError, ExperimentService
from modules import redaction
from modules.run_store import calculate_estimated_cost, get_applicable_price
from models import (
    DiagnosticActivityResponse,
    ChatRequest,
    ChatResponse,
    CodexTaskCreateRequest,
    CodexTaskListResponse,
    CodexTaskResponse,
    CodexTaskSummary,
    ExperimentDetail,
    ExperimentsResponse,
    HealthResponse,
    Idea,
    IdeaCreateRequest,
    IdeasResponse,
    MailboxTaskApproval,
    MailboxTaskApprovalsResponse,
    MailboxTaskRejectRequest,
    PublicDiagnosticExperiment,
    PublicDiagnosticMailboxStatus,
    PublicDiagnosticMailboxTask,
    PublicDiagnosticServiceStatus,
    PublicDiagnosticsResponse,
    Problem,
    ProblemsResponse,
    PublicActivityResponse,
    PublicExperimentSummary,
    StatusResponse,
)

_PROCESS_START_TIME = time.time()



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
CODEX_LIST_DEFAULT_LIMIT = 20
CODEX_LIST_MAX_LIMIT = 100
CODEX_LIST_PROMPT_PREVIEW_CHARS = 80


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
    return MailboxTaskService(experiment_service=_resolve_experiment_service())


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Mark orphaned 'running' Codex tasks as failed exactly once at startup.
    # Never restarts execution and never starts a Codex process.
    _resolve_codex_service().mark_interrupted_tasks_failed()
    mts = _resolve_mailbox_task_service()
    mts.recover_after_restart()
    worker = MailboxTaskWorker(mts)
    worker.start()
    yield


app = FastAPI(lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

API_KEY = os.environ["AGENT_API_KEY"]


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


def authorize(x_api_key: str | None):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/health")
def health():
    return {"status": "ok"}

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


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message must not be empty")

    reply, intent = chat_module.answer(message)
    return ChatResponse(reply=reply, intent=intent)


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
    limit: int = Query(default=CODEX_LIST_DEFAULT_LIMIT, ge=1, le=CODEX_LIST_MAX_LIMIT),
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
    limit: int = Query(default=20, ge=1, le=100),
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


# ---------------------------------------------------------------------------
# Experiment and activity log endpoints (public allowlisted + authenticated)
#
# Thin routes over the existing ExperimentService/run_store persistence;
# no experiment orchestration or new SQLite access is added here.
# ---------------------------------------------------------------------------


@app.get("/experiments", response_model=ExperimentsResponse)
def list_experiments(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=20, ge=1, le=100),
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
    return ExperimentDetail(**experiment.to_dict())


_PUBLIC_PHASES = {
    "queued": "queued", "selecting": "selecting", "implementing": "implementing",
    "verifying": "verifying", "succeeded": "succeeded", "failed": "failed",
    "needs_review": "needs_review", "timed_out": "failed", "rejected": "needs_review",
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
    limit: int = Query(default=50, ge=1, le=200),
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

_DIAGNOSTICS_DEFAULT_LIMIT = 10
_DIAGNOSTICS_MAX_LIMIT = 100


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


@app.get("/diagnostics/public", response_model=PublicDiagnosticsResponse)
def get_public_diagnostics(
    limit: int = Query(default=_DIAGNOSTICS_DEFAULT_LIMIT, ge=1, le=_DIAGNOSTICS_MAX_LIMIT),
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

