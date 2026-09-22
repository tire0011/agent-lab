"""Tests for issue #246 mini preflight planner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.task_orchestrator import NexusTaskOrchestrator, TaskOrchestratorError


def _orchestrator() -> NexusTaskOrchestrator:
    orchestrator = object.__new__(NexusTaskOrchestrator)
    orchestrator.operations_registry = SimpleNamespace(catalog=lambda: [])
    orchestrator.delegation = SimpleNamespace(catalog=lambda: [])
    return orchestrator


def test_preflight_prompt_makes_planner_information_only():
    orchestrator = _orchestrator()
    prompt = orchestrator._preflight_prompt(
        "solve github issue #238",
        history=[],
        observations=[],
    )

    assert "preflight planner" in prompt.lower()
    assert "do not implement" in prompt.lower()
    assert "read-only" in prompt.lower()
    assert "historical context" in prompt.lower()
    assert "ready" in prompt.lower()


def test_parse_preflight_accepts_ready_plan():
    orchestrator = _orchestrator()

    plan = orchestrator._parse_preflight(
        '{"status":"ready","known":["root cause known"],"missing":[],'
        '"historical_context_needed":false,"request_kind":"none","request":"",'
        '"next_goal":"inspect the relevant repository implementation",'
        '"reason":"enough current information"}'
    )

    assert plan["status"] == "ready"
    assert plan["request_kind"] == "none"
    assert plan["next_goal"] == "inspect the relevant repository implementation"


def test_parse_preflight_requires_concrete_information_request():
    orchestrator = _orchestrator()

    with pytest.raises(TaskOrchestratorError):
        orchestrator._parse_preflight(
            '{"status":"need_information","known":[],"missing":["issue contents"],'
            '"historical_context_needed":false,"request_kind":"read_only","request":"",'
            '"next_goal":"understand issue","reason":"missing source"}'
        )


def test_parse_preflight_rejects_tool_request_when_ready():
    orchestrator = _orchestrator()

    with pytest.raises(TaskOrchestratorError):
        orchestrator._parse_preflight(
            '{"status":"ready","known":["enough"],"missing":[],'
            '"historical_context_needed":true,"request_kind":"memory",'
            '"request":"old discussion","next_goal":"inspect code","reason":"maybe useful"}'
        )


def test_parse_preflight_memory_requires_historical_context():
    orchestrator = _orchestrator()

    with pytest.raises(TaskOrchestratorError):
        orchestrator._parse_preflight(
            '{"status":"need_information","known":[],"missing":["context"],'
            '"historical_context_needed":false,"request_kind":"memory",'
            '"request":"old discussion","next_goal":"inspect code","reason":"need context"}'
        )
