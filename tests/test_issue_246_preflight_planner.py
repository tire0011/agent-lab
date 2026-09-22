"""Tests for issue #246 mini preflight planner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.prompt_budget import compute_budget
from modules.task_orchestrator import (
    TOOL_PREFLIGHT_PLAN,
    NexusTaskOrchestrator,
    TaskOrchestratorError,
)


def _orchestrator() -> NexusTaskOrchestrator:
    orchestrator = object.__new__(NexusTaskOrchestrator)
    orchestrator.operations_registry = SimpleNamespace(catalog=lambda: [])
    orchestrator.delegation = SimpleNamespace(catalog=lambda: [])
    return orchestrator


def _budget():
    """A roomy, non-low-memory budget: shape tests should not hit truncation."""
    return compute_budget(
        mem_available_kb=8_000_000,
        swap_free_kb=4_000_000,
        swap_total_kb=4_000_000,
    )


def test_preflight_prompt_is_small_information_only_contract():
    orchestrator = _orchestrator()
    prompt = orchestrator._preflight_prompt(
        "solve github issue #238",
        history=[],
        observations=[],
        budget_plan=_budget(),
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


# --- Regression tests for the PR #247 review findings -----------------------


def test_preflight_prompt_bounds_oversized_observations():
    """A large round-1 result must not carry unbounded into round 2."""
    orchestrator = _orchestrator()
    budget = _budget()
    huge = [
        {
            "tool": "operation",
            "operation": "service_logs",
            "event_id": 11,
            "result": {"output_lines": ["x" * 400 for _ in range(200)]},
        }
    ]

    prompt = orchestrator._preflight_prompt(
        "solve github issue #238", history=[], observations=huge, budget_plan=budget
    )

    assert len(prompt) <= budget.safe_local_budget_chars
    # The stub keeps the event id so the raw result stays retrievable.
    assert "11" in prompt


def test_mark_already_executed_tells_controller_not_to_call_again():
    marked = NexusTaskOrchestrator._mark_already_executed(
        {"tool": "find_tool", "status": "found", "resolved_tool": "git_status"},
        {"tool": "operation", "event_id": 42},
    )

    assert marked["already_executed"] is True
    assert marked["result_in_event"] == 42
    assert "already run" in marked["next_step"]
    assert marked["resolved_tool"] == "git_status"


def test_preflight_unavailable_plan_is_not_capability_evidence():
    """The stub must not let a terminal missing_capability bypass #244."""
    stub = NexusTaskOrchestrator._preflight_unavailable_plan(
        TaskOrchestratorError("llama-server unreachable")
    )

    assert "error" not in stub
    assert stub["preflight_error"] == "llama-server unreachable"
    assert stub["need_info"] is False
    assert stub["kind"] == "none"
    assert NexusTaskOrchestrator._supports_missing_capability(stub) is False
    assert NexusTaskOrchestrator._is_resolution_evidence(stub) is False


def test_preflight_plan_observations_are_never_resolution_evidence():
    ready = {
        "tool": TOOL_PREFLIGHT_PLAN,
        "need_info": False,
        "kind": "none",
        "next_goal": "inspect the implementation",
    }

    assert NexusTaskOrchestrator._is_resolution_evidence(ready) is False
    assert NexusTaskOrchestrator._supports_missing_capability(ready) is False


@pytest.mark.parametrize(
    "value,expected",
    [("", 2), ("1", 1), ("3", 3), ("9", 4), ("0", 1), ("-4", 1), ("abc", 2), ("2.5", 2)],
)
def test_preflight_max_rounds_tolerates_bad_override(monkeypatch, value, expected):
    """A typo in the override must not fail the turn (it escaped the handler)."""
    monkeypatch.setenv("NEXUS_PREFLIGHT_MAX_ROUNDS", value)

    assert NexusTaskOrchestrator._preflight_max_rounds() == expected


def test_working_statuses_cover_every_blocked_worker_status():
    """A worker that dies mid-inference must stay reapable."""
    from modules.nexus_task_store import WORKING_STATUSES

    assert "waiting_for_preflight" in WORKING_STATUSES
    assert "waiting_for_tool_resolver" in WORKING_STATUSES
    assert "waiting_for_llm" in WORKING_STATUSES


@pytest.mark.asyncio
async def test_preflight_keeps_observations_when_a_later_round_fails():
    """Round 1's fetched data must survive a round-2 failure.

    The loop-guard rows for what round 1 executed outlive the failure, so a
    discarded observation can never be re-fetched: the identical call is a
    refused duplicate from then on.
    """
    orchestrator = _orchestrator()
    orchestrator.task_store = SimpleNamespace(append_event=lambda *a, **k: 7)
    orchestrator.memory = SimpleNamespace(
        search=lambda query, limit=6: [{"content": "issue 238 background"}]
    )

    calls = {"n": 0}

    async def fake_completion(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                '{"need_info":true,"kind":"memory","request":"prior work on 238",'
                '"next_goal":"inspect code"}',
                "local",
            )
        raise TaskOrchestratorError("llama-server unreachable")

    orchestrator._schema_completion = fake_completion

    observations = await orchestrator._run_preflight(
        SimpleNamespace(id="s1"), "solve github issue #238", [], _budget()
    )

    # The memory result from round 1 survived.
    memory_hits = [o for o in observations if o.get("tool") == "memory_search"]
    assert len(memory_hits) == 1
    assert memory_hits[0]["results"] == [{"content": "issue 238 background"}]
    # ...and the failure is reported as a plan stub, not raised.
    stub = observations[-1]
    assert stub["tool"] == TOOL_PREFLIGHT_PLAN
    assert stub["preflight_error"] == "llama-server unreachable"
    assert "error" not in stub


def test_task_orchestrator_module_parses_on_python_311():
    """The module must import on 3.11; CI only pins 3.12.

    A nested same-quote f-string (PEP 701) is a SyntaxError before 3.12, and
    it took the whole module down wherever the runtime is older.
    """
    import ast
    from pathlib import Path

    source = Path("modules/task_orchestrator.py").read_text()

    ast.parse(source, feature_version=(3, 11))
