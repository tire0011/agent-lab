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
    test_exit_code: int | None = None
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
