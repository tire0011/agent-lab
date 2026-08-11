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


class Todo(BaseModel):
    id: str
    section: str
    text: str


class TodosResponse(BaseModel):
    count: int
    todos: list[Todo]


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
    """Smaller listing shape: omits the (potentially large) prompt and
    final_message fields present on ``CodexTaskResponse``."""

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


class CodexTaskListResponse(BaseModel):
    count: int
    tasks: list[CodexTaskSummary]
