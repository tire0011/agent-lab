"""Pydantic models for the agent-lab API.

Kept intentionally small: one module for request/response shapes so routes
in api.py stay thin and data adapters in data_sources.py stay storage-only.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    version: str


class StatusResponse(BaseModel):
    agent_status: str
    api_version: str
    components: list[str]


class Problem(BaseModel):
    id: str
    source: str
    summary: str
    detail: str
    timestamp: str


class ProblemsResponse(BaseModel):
    count: int
    problems: list[Problem]


class Idea(BaseModel):
    id: str
    section: str
    text: str
    state: str = "new"


class IdeasResponse(BaseModel):
    count: int
    ideas: list[Idea]


class IdeaCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=120)


class ChatResponse(BaseModel):
    reply: str
    intent: str
    session_id: str
    action: str
    experiment_id: str | None = None
    status: str


class ChatJobResponse(BaseModel):
    job_id: str
    status: str
    reply: str | None = None
    intent: str | None = None
    session_id: str | None = None
    action: str | None = None
    experiment_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Codex task models
#
# ``working_dir`` and ``sandbox`` are never accepted from the client: they
# are fixed server-side (see api.py) to keep Codex task execution read-only
# and confined to the repository root. ``timeout_seconds`` may only be
# lowered by the client; the upper bound is enforced by ``le=`` below.
# ---------------------------------------------------------------------------


class CodexTaskCreateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=16 * 1024)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)


class CodexTaskResponse(BaseModel):
    id: str
    status: str
    prompt: str
    working_dir: str
    sandbox: str
    provider: str = "codex"
    timeout_seconds: int
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    thread_id: str | None = None
    exit_code: int | None = None
    runtime_ms: int | None = None
    timed_out: bool | None = None
    usage: dict[str, Any] | None = None
    final_message: str | None = None
    error: str | None = None


class CodexTaskSummary(BaseModel):
    """Smaller listing shape: omits the full (potentially large) prompt and
    final_message fields present on ``CodexTaskResponse``, but keeps a short
    ``prompt_preview`` so list output remains useful without printing a
    long prompt."""

    id: str
    status: str
    working_dir: str
    sandbox: str
    provider: str = "codex"
    timeout_seconds: int
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    thread_id: str | None = None
    exit_code: int | None = None
    runtime_ms: int | None = None
    timed_out: bool | None = None
    error: str | None = None
    prompt_preview: str


class CodexTaskListResponse(BaseModel):
    count: int
    tasks: list[CodexTaskSummary]


# ---------------------------------------------------------------------------
# Mailbox task approval models
# ---------------------------------------------------------------------------


class MailboxTaskApproval(BaseModel):
    id: str
    mailbox_msg_id: int
    task_body: str
    sender: str
    file_path: str
    received_at: str
    status: str
    approved_at: str | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None
    experiment_id: str | None = None
    outbox_file: str | None = None
    result_summary: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class MailboxTaskApprovalsResponse(BaseModel):
    count: int
    tasks: list[MailboxTaskApproval]


class MailboxTaskRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------------------
# Experiment activity log models
# ---------------------------------------------------------------------------




class ExperimentAdoptionFailure(BaseModel):
    id: str
    adoption_id: str
    status: str
    reason: str | None = None
    test_exit_code: int | None = None
    test_output_lines: list[str] = Field(default_factory=list)
    experiment_commit_sha: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    superseded_at: str
    superseded_by: str


class ExperimentAdoptionStatus(BaseModel):
    id: str | None = None
    experiment_id: str
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    experiment_commit_sha: str | None = None
    integrated_commit_sha: str | None = None
    target_branch: str = "main"
    expected_files: list[str] = Field(default_factory=list)
    actual_files: list[str] = Field(default_factory=list)
    test_exit_code: int | None = None
    test_output_lines: list[str] = Field(default_factory=list)
    reason: str | None = None
    local_sync_pending: bool = False
    diagnostic_worktree_path: str | None = None
    recovery_attempts: int = 0
    stale_retry_attempts: int = 0
    # Failures this adoption row already superseded. There is one row per
    # experiment, so a retry overwrites the failure that preceded it; this
    # is where the earlier reason stays readable (Issue #136).
    previous_failures: list[ExperimentAdoptionFailure] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class OperationsExecuteArgs(BaseModel):
    experiment_id: str | None = None
    limit: int | None = None


class OperationsExecuteRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=80)
    args: OperationsExecuteArgs = Field(default_factory=OperationsExecuteArgs)
    invocation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Stable key for an operation attempt. Callers that retry after a timeout "
            "or restart must reuse the same value to receive the recorded result."
        ),
    )


class OperationArgumentDefinition(BaseModel):
    name: str
    type: str
    description: str
    required: bool = False
    default: int | str | None = None
    minimum: int | None = None
    maximum: int | None = None


class OperationCatalogEntry(BaseModel):
    operation: str
    description: str
    arguments: list[OperationArgumentDefinition] = Field(default_factory=list)
    # Whether calling this capability can change Nexus state. Surfaced so the
    # boundary a read-only role filters on is visible wherever the catalog is
    # read, not only inside the tool router.
    mutates_state: bool = False


class OperationsCatalogResponse(BaseModel):
    count: int
    operations: list[OperationCatalogEntry] = Field(default_factory=list)


class OperationsExecuteResponse(BaseModel):
    operation: str
    success: bool
    output_lines: list[str] = Field(default_factory=list)
    duration_ms: int
    exit_code: int | None = None
    error_summary: str | None = None


class ExperimentSummary(BaseModel):
    """Compact experiment listing shape (omits large text/JSON fields)."""

    id: str
    goal: str
    status: str
    created_at: str
    selection_started_at: str | None = None
    selection_ended_at: str | None = None
    implementation_provider: str | None = None
    implementation_started_at: str | None = None
    implementation_ended_at: str | None = None
    ended_at: str | None = None
    cancelled_at: str | None = None
    cancelled_by: str | None = None
    branch_name: str | None = None
    test_exit_code: int | None = None
    error: str | None = None
    failure_phase: str | None = None
    failure_type: str | None = None


class ExperimentDetail(BaseModel):
    """Full experiment record, mirroring ``modules.experiment_service.Experiment``."""

    id: str
    goal: str
    status: str
    created_at: str
    selection_started_at: str | None = None
    selection_ended_at: str | None = None
    implementation_provider: str | None = None
    implementation_started_at: str | None = None
    implementation_ended_at: str | None = None
    ended_at: str | None = None
    cancelled_at: str | None = None
    cancelled_by: str | None = None
    plan: dict[str, Any] | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    research_task_id: str | None = None
    research_started_at: str | None = None
    research_ended_at: str | None = None
    research_runtime_ms: int | None = None
    research_usage: dict[str, Any] | None = None
    research_raw_summary: str | None = None
    research_diagnostic_log: str | None = None
    selection_task_id: str | None = None
    reformat_retry_task_id: str | None = None
    policy_replan_task_id: str | None = None
    implementation_task_id: str | None = None
    selection_runtime_ms: int | None = None
    reformat_retry_runtime_ms: int | None = None
    policy_replan_runtime_ms: int | None = None
    implementation_runtime_ms: int | None = None
    selection_usage: dict[str, Any] | None = None
    reformat_retry_usage: dict[str, Any] | None = None
    policy_replan_usage: dict[str, Any] | None = None
    implementation_usage: dict[str, Any] | None = None
    selection_raw_message: str | None = None
    reformat_raw_message: str | None = None
    policy_replan_raw_message: str | None = None
    selection_diagnostic_log: str | None = None
    reformat_diagnostic_log: str | None = None
    policy_replan_diagnostic_log: str | None = None
    test_command: list[str] | None = None
    test_exit_code: int | None = None
    test_output: str | None = None
    test_failure_tail: list[str] = Field(default_factory=list)
    git_status: str | None = None
    diff_stat: str | None = None
    diff_text: str | None = None
    error: str | None = None
    failure_phase: str | None = None
    failure_type: str | None = None
    policy_rule: str | None = None
    quality_evaluation: dict[str, Any] | None = None
    adoption: ExperimentAdoptionStatus | None = None


class ExperimentsResponse(BaseModel):
    count: int
    experiments: list[ExperimentSummary]


class ActivityEntry(BaseModel):
    id: int
    timestamp: str
    entity_type: str
    entity_id: str
    event: str
    from_status: str | None = None
    to_status: str | None = None
    message: str | None = None


class ActivityResponse(BaseModel):
    count: int
    activity: list[ActivityEntry]


class DiagnosticActivityEntry(ActivityEntry):
    mailbox_task_id: str | None = None
    phase: str | None = None
    policy_rule: str | None = None
    rejection_reason: str | None = None
    test_command: list[str] | None = None
    test_exit_code: int | None = None
    changed_files: list[str] = Field(default_factory=list)
    test_output_lines: list[str] = Field(default_factory=list)


class DiagnosticActivityResponse(BaseModel):
    count: int
    activity: list[DiagnosticActivityEntry]


class PublicActivityEntry(BaseModel):
    timestamp: str
    phase: str
    status: str
    message: str
    mailbox_task_id: str | None = None
    experiment_id: str
    failure_reason: str | None = None
    test_exit_code: int | None = None
    changed_files: list[str] = Field(default_factory=list)
    test_summary: str | None = None


class PublicActivityResponse(BaseModel):
    count: int
    activity: list[PublicActivityEntry]


class PublicExperimentUsage(BaseModel):
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class PublicExperimentSummary(BaseModel):
    id: str
    mailbox_task_id: str | None = None
    phase: str
    status: str
    created_at: str
    ended_at: str | None = None
    failure_reason: str | None = None
    test_exit_code: int | None = None
    changed_files: list[str] = Field(default_factory=list)
    test_summary: str | None = None
    usage: PublicExperimentUsage | None = None


# ---------------------------------------------------------------------------
# Public diagnostics model (GET /diagnostics/public)
#
# Every field here is drawn from an explicit allowlist assembled in api.py;
# nothing is derived from a generic ``to_dict()``/``model_dump()`` call, so
# adding a new internal field to ``Experiment`` or ``MailboxTaskApproval``
# can never silently widen this public response.
# ---------------------------------------------------------------------------


class DiagnosticLifecyclePhase(BaseModel):
    phase: str
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None


class PublicDiagnosticMailboxTask(BaseModel):
    id: str
    status: str
    received_at: str
    experiment_id: str | None = None


class PublicDiagnosticExperiment(BaseModel):
    id: str
    mailbox_task_id: str | None = None
    phase: str
    status: str
    created_at: str
    ended_at: str | None = None
    lifecycle: list[DiagnosticLifecyclePhase] = Field(default_factory=list)
    rejection_reason: str | None = None
    policy_rule: str | None = None
    test_command: str | None = None
    test_exit_code: int | None = None
    failure_summary: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    diagnostic_messages: list[str] = Field(default_factory=list)
    usage: dict[str, int] | None = None
    estimated_cost: dict[str, Any] | None = None


class PublicDiagnosticServiceStatus(BaseModel):
    status: str
    git_revision: str | None = None
    deployment_time: str | None = None


class PublicDiagnosticMailboxStatus(BaseModel):
    status: str
    pending_count: int
    latest_tasks: list[PublicDiagnosticMailboxTask] = Field(default_factory=list)


class PublicDiagnosticsResponse(BaseModel):
    service: PublicDiagnosticServiceStatus
    mailbox: PublicDiagnosticMailboxStatus
    experiments: list[PublicDiagnosticExperiment] = Field(default_factory=list)
    problems: list[Problem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Mailbox inbox/outbox listing (public, sanitized).
#
# Every field is drawn from an explicit allowlist assembled in api.py.
# ``preview`` is always short and redacted; full prompts, task bodies,
# absolute file paths, and unfiltered file/command output are never
# included here (see the "Aufträge"/task-approval and "Logs" endpoints for
# authenticated, more detailed views).
# ---------------------------------------------------------------------------


class MailboxMessageEntry(BaseModel):
    id: str
    timestamp: str
    type: str
    status: str
    sender: str | None = None
    title: str
    preview: str | None = None


class MailboxMessagesResponse(BaseModel):
    inbox_count: int
    outbox_count: int
    inbox: list[MailboxMessageEntry] = Field(default_factory=list)
    outbox: list[MailboxMessageEntry] = Field(default_factory=list)


class ExperimentLogEntry(BaseModel):
    """One structured, defensively re-sanitized line from an experiment's
    JSONL log (``state/experiment-logs/<experiment_id>.jsonl``). Explicit
    allowlist only - never a generic serialization of the raw log dict, so
    an unexpected/legacy field written by an older logger version cannot
    leak into the API response."""

    timestamp: str
    level: str
    logger: str
    message: str | None = None
    experiment_id: str | None = None
    mailbox_task_id: str | None = None
    job_id: str | None = None
    session_id: str | None = None
    phase: str
    event: str
    duration_ms: int | None = None
    command: str | None = None
    cwd: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    stacktrace: str | None = None
    truncated: bool | None = None
    source: str = "experiment"


class ExperimentLogsResponse(BaseModel):
    count: int
    experiment_id: str
    entries: list[ExperimentLogEntry] = Field(default_factory=list)
    next_cursor: str | None = None


class UnifiedLogsResponse(BaseModel):
    """One chronological Nexus log timeline: structured JSONL entries
    (experiments, mailbox tasks, chat jobs, system) merged with the
    ``agent-api``/``nexus-mailbox``/``llama-server`` systemd journals.

    Backs the Logs tab's default view; entries use the same explicit
    allowlist as ``ExperimentLogsResponse``, plus ``source``.
    """

    count: int
    entries: list[ExperimentLogEntry] = Field(default_factory=list)
    next_cursor: str | None = None
    source_errors: dict[str, str] | None = None
    # True when the scan stopped at its byte/file budget before reaching
    # the oldest history, so a caller can say "there may be older matches"
    # instead of presenting a partial search as an exhaustive one.
    truncated: bool = False


class PublicLogEntry(BaseModel):
    """One structured log line, safe for the unauthenticated Logs tab.

    A strict subset of ``ExperimentLogEntry``'s allowlist: never includes
    command/cwd/stdout/stderr/exception/stacktrace/job_id/session_id, since
    those may carry internal detail not meant for an unauthenticated
    caller. ``message`` is still the write- and read-path redacted text
    (see ``modules.redaction``), matching what ``/diagnostics/public``
    already exposes via ``diagnostic_messages``/``failure_summary``.
    """

    timestamp: str
    level: str
    phase: str
    event: str
    message: str | None = None
    experiment_id: str | None = None
    mailbox_task_id: str | None = None
    truncated: bool | None = None
    source: str = "experiment"


class LogTraceResponse(BaseModel):
    """Everything Nexus did for one order, reconstructed from its logs.

    ``reference`` echoes what was asked for; ``correlation_ids`` reports
    which experiment/mailbox-task/session/job ids that reference turned
    out to span, so an operator can see *why* these entries belong
    together. ``summary`` is derived from ``entries`` alone, so it can
    never contradict the timeline beneath it.
    """

    reference: str
    found: bool
    correlation_ids: dict[str, list[str]] = Field(default_factory=dict)
    summary: dict[str, object] = Field(default_factory=dict)
    count: int = 0
    entries: list[ExperimentLogEntry] = Field(default_factory=list)
    truncated: bool = False


class PublicUnifiedLogsResponse(BaseModel):
    """Public, unauthenticated equivalent of ``UnifiedLogsResponse``.

    Backs the Logs tab's default view when no API key has been entered,
    so the fallback is the same central multi-source feed (experiments,
    mailbox tasks, system) rather than only per-experiment diagnostics.
    """

    count: int
    entries: list[PublicLogEntry] = Field(default_factory=list)
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Operation Chain models
# ---------------------------------------------------------------------------


class OperationChainStep(BaseModel):
    id: str = Field(..., min_length=1, max_length=80, pattern="^[a-zA-Z0-9_-]+$")
    action: str = Field(..., min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class OperationChainRequest(BaseModel):
    steps: list[OperationChainStep] = Field(..., min_items=1, max_items=10)
    stop_on_error: bool = False
    request_id: str | None = None

    @staticmethod
    def validate_unique_ids(v: list[OperationChainStep]) -> list[OperationChainStep]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step IDs")
        return v


class OperationChainStepResult(BaseModel):
    id: str
    action: str
    status: str  # "success", "failed", "skipped"
    started_at: str
    duration_ms: int
    output: list[str] = Field(default_factory=list)
    error: str | None = None
    truncated: bool = False


class OperationChainResult(BaseModel):
    request_id: str
    overall_status: str  # "queued", "running", "completed", "completed_with_errors", "failed", "rejected", "interrupted"
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    steps_successful: int
    steps_failed: int
    steps: list[OperationChainStepResult] = Field(default_factory=list)
    rejection_reason: str | None = None


# ── Maintenance API models ────────────────────────────────────────────────────

class MaintenanceExecuteRequest(BaseModel):
    operation: str
    args: dict = Field(default_factory=dict)


class MaintenanceExecuteResponse(BaseModel):
    operation: str
    result: dict


class MaintenanceAuditEntry(BaseModel):
    timestamp: str
    operation: str
    args: dict
    outcome: str


class MaintenanceAuditResponse(BaseModel):
    entries: list[MaintenanceAuditEntry]
    total: int


class MaintenanceStatusResponse(BaseModel):
    enabled: bool
    expiry: str | None = None


# ---------------------------------------------------------------------------
# Self-Healing / Operations Executor models
# ---------------------------------------------------------------------------


class GitCommitPushRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)
    files: list[str] = Field(..., min_length=1, max_length=50)


class ServiceRestartRequest(BaseModel):
    service: str = Field(..., min_length=1, max_length=50)


class LlamaModelTestRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=50)
    prompt: str = Field(default="ping", min_length=1, max_length=500)


class SetConfigRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=100, pattern="^[A-Z_][A-Z0-9_]*$")
    value: str = Field(..., max_length=1000)


class OperationResponse(BaseModel):
    """Standardised wrapper for all /operations/* responses."""
    success: bool
    status: str
    operation: str
    timestamp: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Failure analysis (#145): the reusable LLM-powered diagnosis service.
# ---------------------------------------------------------------------------


class FailureAnalysisRootCause(BaseModel):
    cause: str
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class FailureAnalysisAction(BaseModel):
    type: str
    target: str = ""


class FailureAnalysisEntry(BaseModel):
    id: str
    subject_type: str
    subject_id: str
    created_at: str
    experiment_id: str | None = None
    mailbox_task_id: str | None = None
    provider: str
    model: str | None = None
    summary: str
    category: str
    confidence: float = 0.0
    likely_root_causes: list[FailureAnalysisRootCause] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    recommended_checks: list[str] = Field(default_factory=list)
    recommended_actions: list[FailureAnalysisAction] = Field(default_factory=list)
    can_self_recover: bool = False
    needs_external_model: bool = False
    needs_human: bool = False
    escalation_reason: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class NexusSessionClaim(BaseModel):
    """One claimed unit of controller work and how alive it looks (#162)."""

    session_id: str
    source_type: str
    source_id: str | None = None
    title: str | None = None
    status: str
    worker_id: str | None = None
    claimed_at: str | None = None
    last_heartbeat_at: str | None = None
    heartbeat_age_seconds: float | None = None
    stale: bool = False


class NexusLivenessResponse(BaseModel):
    """Heartbeat/liveness state, so "is this still running?" is answerable.

    ``unclaimed_working_sessions`` counts rows in a working status that carry
    no heartbeat at all - pre-heartbeat rows, or a caller that never claimed
    its work. They are reported rather than hidden: that gap is precisely what
    heartbeats exist to close.
    """

    generated_at: str
    stale_after_seconds: int
    heartbeat_interval_seconds: int
    live_claims: int
    stale_claims: int
    unclaimed_working_sessions: int
    resting_statuses: list[str] = Field(default_factory=list)
    counts_by_status: dict[str, int] = Field(default_factory=dict)
    claims: list[NexusSessionClaim] = Field(default_factory=list)


class DiagnosticProgress(BaseModel):
    """Is this work still running, was it interrupted, or did it finish? (#188)

    ``source`` names the signal ``last_progress_at`` came from - a controller
    heartbeat, a structured log entry, or the row's own ``updated_at`` - so a
    caller can tell an explicit "I am alive" from a merely witnessed side
    effect. See ``modules.nexus_diagnostics``.
    """

    state: str
    reason: str
    last_progress_at: str | None = None
    source: str | None = None
    age_seconds: float | None = None
    stale_after_seconds: int = 0
    heartbeat_interval_seconds: int = 0


class DiagnosticLifecycle(BaseModel):
    """One unit of work's lifecycle state, joined across every subsystem.

    Deliberately flat and stably named: a caller answering "where did this
    stop?" should never need to know which of the mailbox, experiment,
    adoption, GitHub-link or controller-session tables an answer came out of.
    """

    subject_type: str
    subject_id: str
    lifecycle: str
    status: str | None = None
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    ended_at: str | None = None
    branch: str | None = None
    commit: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    worker_id: str | None = None
    service: str | None = None
    last_error: str | None = None
    progress: DiagnosticProgress | None = None
    correlation_ids: dict[str, str] = Field(default_factory=dict)
    related: dict[str, Any] = Field(default_factory=dict)


class DiagnosticLogEntry(BaseModel):
    """A log line compacted to what a diagnosis reads, redacted and bounded."""

    model_config = ConfigDict(extra="allow")

    timestamp: str = ""
    level: str = ""
    phase: str = ""
    event: str = ""
    message: str = ""
    source: str = ""


class DiagnosticReportResponse(BaseModel):
    """``nexus logs <reference>``: lifecycle plus the logs that explain it."""

    generated_at: str
    reference: str
    lifecycle: DiagnosticLifecycle
    log_entries: list[DiagnosticLogEntry] = Field(default_factory=list)
    log_sources: list[str] = Field(default_factory=list)
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)


class DiagnosticServiceLogsResponse(BaseModel):
    """``nexus logs service <name>``: a bounded journal slice.

    ``error`` is populated rather than raised when the journal cannot be read
    (no systemd, unknown unit, a timeout): an unreadable journal is itself a
    finding, and which unit was consulted over what window still stands.
    """

    generated_at: str
    service: str
    requested: str
    lines: int
    since: str | None = None
    entries: list[DiagnosticLogEntry] = Field(default_factory=list)
    error: str | None = None


class FailureAnalysesResponse(BaseModel):
    count: int
    analyses: list[FailureAnalysisEntry]


class FailureAnalysisRequest(BaseModel):
    """Manual/debug request to analyze a specific Nexus failure (#145)."""

    subject_type: str = Field(..., min_length=1, max_length=80)
    experiment_id: str | None = Field(default=None, max_length=128)
    mailbox_task_id: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=2000)
