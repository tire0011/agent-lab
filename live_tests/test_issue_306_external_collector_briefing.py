"""#306: an escalated collector call gets a briefing from the external chain.

In both live #301 runs codex_readonly answered action=lookup and the weaker
local model wrote the briefing. This sends the real escalated #301 collector
prompt through the external chain, as production does, and checks the answer.
"""

from __future__ import annotations

import asyncio

import pytest

from live_tests.conftest import live_budget
from live_tests.test_issue_304_briefing_keeps_the_change import ISSUE_301
from modules.task_orchestrator import ControllerBudgetExceededError


def test_escalated_301_collection_returns_a_briefing(orchestrator, live_result):
    task = "solve github issue #301"
    budget = live_budget()
    request = orchestrator._knowledge_request(task)
    sources = [ISSUE_301]
    prompt = orchestrator._knowledge_collector_prompt(request, sources, budget, force_briefing=True)
    try:
        raw, provider = asyncio.run(
            orchestrator._external_fallback_completion(
                orchestrator._external_collector_prompt(request, sources, budget, prompt),
                session_id="live-306",
                budget_plan=budget,
                task_logger=None,
                role="knowledge_collector",
            )
        )
    except ControllerBudgetExceededError as exc:
        pytest.skip(f"no external provider answered: {exc}")
    decision = orchestrator._parse_knowledge_collector(raw)
    live_result.update(provider=provider, action=decision["action"], briefing=decision["briefing"][:1500])
    assert decision["action"] == "briefing", raw[:500]
