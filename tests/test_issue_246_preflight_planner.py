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


def test_preflight_prompt_is_small_information_only_contract():
    orchestrator = _orchestrator()
    prompt = orchestrator._preflight_prompt(
        "solve github issue #238",
        history=[],
        observations=[],
    )

    assert "preflight planner" in prompt.lower()
    assert "do not implement" in prompt.lower()
    assert "need_info=true" in prompt
    assert "kind=memory" in prompt
    assert "one small json object" in prompt.lower()


def test_parse_preflight_accepts_ready_plan():
    orchestrator = _orchestrator()

    plan = orchestrator._parse_preflight(
        '{"need_info":false,"kind":"none","request":"",'
        '"next_goal":"inspect the relevant repository implementation"}'
    )

    assert plan == {
        "need_info": False,
        "kind": "none",
        "request": "",
        "next_goal": "inspect the relevant repository implementation",
    }


def test_parse_preflight_accepts_concrete_read_request():
    orchestrator = _orchestrator()

    plan = orchestrator._parse_preflight(
        '{"need_info":true,"kind":"read_only","request":"fetch GitHub issue #238",'
        '"next_goal":"understand the requested repository change"}'
    )

    assert plan["need_info"] is True
    assert plan["kind"] == "read_only"
    assert plan["request"] == "fetch GitHub issue #238"


def test_parse_preflight_accepts_memory_when_requested():
    orchestrator = _orchestrator()

    plan = orchestrator._parse_preflight(
        '{"need_info":true,"kind":"memory","request":"earlier architecture decision",'
        '"next_goal":"continue with the prior design"}'
    )

    assert plan["need_info"] is True
    assert plan["kind"] == "memory"


def test_parse_preflight_requires_concrete_request_when_info_needed():
    orchestrator = _orchestrator()

    with pytest.raises(TaskOrchestratorError):
        orchestrator._parse_preflight(
            '{"need_info":true,"kind":"read_only","request":"",'
            '"next_goal":"understand issue"}'
        )


def test_parse_preflight_rejects_lookup_when_ready():
    orchestrator = _orchestrator()

    with pytest.raises(TaskOrchestratorError):
        orchestrator._parse_preflight(
            '{"need_info":false,"kind":"memory","request":"old discussion",'
            '"next_goal":"inspect code"}'
        )


def test_parse_preflight_requires_none_when_ready():
    orchestrator = _orchestrator()

    with pytest.raises(TaskOrchestratorError):
        orchestrator._parse_preflight(
            '{"need_info":false,"kind":"read_only","request":"",'
            '"next_goal":"inspect code"}'
        )
