"""Pydantic models for the agent-lab API.

Kept intentionally small: one module for request/response shapes so routes
in api.py stay thin and data adapters in data_sources.py stay storage-only.
"""

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
