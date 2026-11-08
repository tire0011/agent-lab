from typing import Any, Iterator

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import PlainTextResponse
from models import (
    DiagnosticReportResponse,
    DiagnosticServiceLogsResponse,
    FailureAnalysesResponse,
    FailureAnalysisEntry,
    FailureAnalysisRequest,
    NexusLivenessResponse,
    NexusSessionClaim,
    PublicDiagnosticExperiment,
    PublicDiagnosticMailboxStatus,
    PublicDiagnosticMailboxTask,
    PublicDiagnosticsResponse,
    PublicDiagnosticServiceStatus,
)
from modules import nexus_diagnostics as nexus_diagnostics_module
from modules.experiment_service import ExperimentService
from modules.failure_analysis_service import FailureAnalysisService
from modules.log_reader import LogReader
from modules.mailbox.task_service import MailboxTaskService
from modules.nexus_task_store import NexusTaskStore

from api import (
    LIMIT_MIN,
    app,
    get_experiment_service,
    get_failure_analysis_service,
    get_log_reader,
    get_mailbox_task_service,
    get_nexus_diagnostics,
    get_nexus_task_store,
)
from datetime import datetime
from datetime import timezone
from fastapi import HTTPException
from modules import redaction
from modules.run_store import calculate_estimated_cost
from modules.run_store import get_applicable_price
import api
import os

router = APIRouter(dependency_overrides_provider=app)


# GET /diagnostics/public: built exclusively from the explicit allowlist below, never
# derived from to_dict()/model_dump() of internal records.
# Excluded by construction: API keys, tokens, credentials, headers, prompts/task bodies,
# env vars, source code/diffs/file contents, stack traces with internal paths, and
# unrestricted/raw command output.

DIAGNOSTICS_DEFAULT_LIMIT = 10
DIAGNOSTICS_MAX_LIMIT = 100


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
        mailbox_task_id=api._mailbox_task_id(service, experiment.id),
        phase=api._public_phase(experiment.status),
        status=experiment.status,
        created_at=experiment.created_at,
        ended_at=experiment.ended_at,
        lifecycle=_experiment_lifecycle(experiment),
        rejection_reason=redaction.sanitized_reason(experiment.error),
        policy_rule=experiment.policy_rule,
        test_command=redaction.test_command_name(experiment.test_command),
        test_exit_code=experiment.test_exit_code,
        failure_summary=api._failure_reason(experiment),
        changed_files=api._changed_file_names(experiment.git_status),
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


@router.get("/diagnostics/public", response_model=PublicDiagnosticsResponse)
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

    checkout_revision = api._read_git_revision()
    return PublicDiagnosticsResponse(
        service=PublicDiagnosticServiceStatus(
            status="ok",
            git_revision=api._STARTUP_REVISION,
            checkout_revision=checkout_revision,
            stale=api._revisions_differ(api._STARTUP_REVISION, checkout_revision),
            deployment_time=api._deployment_time(),
        ),
        mailbox=mailbox_status,
        experiments=diagnostic_experiments,
        problems=api.list_problems(),
    )



# Failure analysis (#145): manual inspection of stored diagnoses and on-demand
# triggers; the automatic integrations live in modules/mailbox/, this surface is
# for humans and scripts.

FAILURE_ANALYSES_DEFAULT_LIMIT = 20
FAILURE_ANALYSES_MAX_LIMIT = 100


@router.get("/diagnostics/nexus/liveness", response_model=NexusLivenessResponse)
def get_nexus_liveness(
    x_api_key: str | None = Header(default=None),
    stale_after_seconds: int | None = Query(default=None, ge=0),
    store: NexusTaskStore = Depends(get_nexus_task_store),
) -> NexusLivenessResponse:
    """Who owns which controller work, and when it last reported itself alive.

    Read-only: reports staleness but never reaps, so an operator can inspect a
    suspect session without changing its state. ``stale_after_seconds`` is clamped
    to the reaper's safe floor, so no query can make live work look dead (#162).
    """
    api.authorize(x_api_key)
    snapshot = store.liveness(
        stale_after_seconds if stale_after_seconds else api._STALE_HEARTBEAT_SECONDS
    )
    return NexusLivenessResponse(
        **{**snapshot, "claims": [NexusSessionClaim(**c) for c in snapshot["claims"]]}
    )


# Nexus diagnostics (#188): first-class read-only answers to "what happened to this
# work?", resolved through the one shared backend (modules/nexus_diagnostics.py).

DIAGNOSTIC_LOG_DEFAULT_LINES = nexus_diagnostics_module.DEFAULT_LOG_LINES
DIAGNOSTIC_LOG_MAX_LINES = nexus_diagnostics_module.MAX_LOG_LINES


def _diagnostic_report_response(
    report: nexus_diagnostics_module.DiagnosticReport,
) -> DiagnosticReportResponse:
    return DiagnosticReportResponse(**report.to_dict())


@router.get("/diagnostics/logs/service/{service}", response_model=DiagnosticServiceLogsResponse)
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

    *service* may be a friendly name (``api``, ``mailbox``, ``llm``) or a unit name;
    anything unrecognized is validated as a unit name before use, so no caller can
    smuggle a shell fragment through this route.
    """
    api.authorize(x_api_key)
    diagnostics.log_reader = log_reader
    try:
        result = await diagnostics.service_logs(
            service, lines=lines, since_minutes=since_minutes
        )
    except nexus_diagnostics_module.DiagnosticsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DiagnosticServiceLogsResponse(**result.to_dict())


@router.get("/diagnostics/logs/last-failure", response_model=DiagnosticReportResponse)
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

    Stalled counts: a process that died mid-run never wrote a ``failed`` row, so
    searching explicit failures alone would miss exactly this case (#186/#187).
    """
    api.authorize(x_api_key)
    try:
        return _diagnostic_report_response(diagnostics.last_failure(lines=lines, scan=scan))
    except nexus_diagnostics_module.DiagnosticsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/diagnostics/llm-jobs/{job_id}", response_class=PlainTextResponse)
def get_diagnostic_llm_job(
    job_id: str,
    x_api_key: str | None = Header(default=None),
    view: str = Query(default="summary", pattern="^(summary|result|prompt)$"),
    diagnostics: nexus_diagnostics_module.NexusDiagnostics = Depends(get_nexus_diagnostics),
) -> str:
    """``nexus debug llm-job <id> [--result|--prompt]`` (#259), read-only.

    Plain ``key: value`` lines, the same the ``nexus_llm_job`` operation returns.
    """
    api.authorize(x_api_key)
    try:
        job = diagnostics.llm_job(job_id)
    except nexus_diagnostics_module.DiagnosticsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return "\n".join(nexus_diagnostics_module.render_llm_job(job, view=view)) + "\n"


@router.get("/diagnostics/status/{reference:path}", response_model=DiagnosticReportResponse)
def get_diagnostic_status(
    reference: str,
    x_api_key: str | None = Header(default=None),
    diagnostics: nexus_diagnostics_module.NexusDiagnostics = Depends(get_nexus_diagnostics),
) -> DiagnosticReportResponse:
    """Lifecycle state for one piece of work, with no log bodies.

    *reference* may be a mailbox task id, experiment id, controller session id,
    issue or PR number (``188``, ``#188``, ``PR #195``), inbox filename, or a
    unique prefix of any id.
    """
    api.authorize(x_api_key)
    try:
        return _diagnostic_report_response(diagnostics.status_report(reference))
    except nexus_diagnostics_module.DiagnosticsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/diagnostics/logs/{reference:path}", response_model=DiagnosticReportResponse)
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
    ``/diagnostics/logs/last-failure`` routes on purpose: a ``:path`` parameter
    would otherwise swallow both.
    """
    api.authorize(x_api_key)
    try:
        return _diagnostic_report_response(diagnostics.diagnose(reference, lines=lines))
    except nexus_diagnostics_module.DiagnosticsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/diagnostics/failure-analyses", response_model=FailureAnalysesResponse)
def list_failure_analyses(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=FAILURE_ANALYSES_DEFAULT_LIMIT, ge=LIMIT_MIN, le=FAILURE_ANALYSES_MAX_LIMIT),
    subject_type: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    service: FailureAnalysisService = Depends(get_failure_analysis_service),
) -> FailureAnalysesResponse:
    api.authorize(x_api_key)
    entries = service.list_recent(limit=limit, subject_type=subject_type, subject_id=subject_id)
    return FailureAnalysesResponse(count=len(entries), analyses=entries)


@router.get("/diagnostics/failure-analyses/{analysis_id}", response_model=FailureAnalysisEntry)
def get_failure_analysis(
    analysis_id: str,
    x_api_key: str | None = Header(default=None),
    service: FailureAnalysisService = Depends(get_failure_analysis_service),
) -> FailureAnalysisEntry:
    api.authorize(x_api_key)
    entry = service.get(analysis_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="failure analysis not found")
    return FailureAnalysisEntry(**entry)


@router.post("/diagnostics/failure-analyses/analyze", response_model=FailureAnalysisEntry, status_code=201)
def trigger_failure_analysis(
    request: FailureAnalysisRequest,
    x_api_key: str | None = Header(default=None),
    service: FailureAnalysisService = Depends(get_failure_analysis_service),
) -> FailureAnalysisEntry:
    api.authorize(x_api_key)
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
