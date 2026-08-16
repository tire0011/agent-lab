"""Pydantic models for the agent-lab API.

Kept intentionally small: one module for request/response shapes so routes
in api.py stay thin and data adapters in data_sources.py stay storage-only.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str


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


class ChatResponse(BaseModel):
    reply: str
    intent: str


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
    created_at: str | None = None
    updated_at: str | None = None


class OperationsExecuteArgs(BaseModel):
    experiment_id: str | None = None
    limit: int | None = None


class OperationsExecuteRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=80)
    args: OperationsExecuteArgs = Field(default_factory=OperationsExecuteArgs)


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
    implementation_started_at: str | None = None
    implementation_ended_at: str | None = None
    ended_at: str | None = None
    branch_name: str | None = None
    test_exit_code: int | None = None
    error: str | None = None


class ExperimentDetail(BaseModel):
    """Full experiment record, mirroring ``modules.experiment_service.Experiment``."""

    id: str
    goal: str
    status: str
    created_at: str
    selection_started_at: str | None = None
    selection_ended_at: str | None = None
    implementation_started_at: str | None = None
    implementation_ended_at: str | None = None
    ended_at: str | None = None
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
    git_status: str | None = None
    diff_stat: str | None = None
    diff_text: str | None = None
    error: str | None = None
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
    experiment_id: str
    mailbox_task_id: str | None = None
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


class ExperimentLogsResponse(BaseModel):
    count: int
    experiment_id: str
    entries: list[ExperimentLogEntry] = Field(default_factory=list)


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
