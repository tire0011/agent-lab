"""#301: the knowledge collector escalation leaves enough task events to diagnose it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.prompt_budget import compute_budget
from modules.task_orchestrator import ControllerBudgetExceededError, NexusTaskOrchestrator
from tests.test_issue_297_knowledge_escalation import _briefing, _run

NOT_FOUND = "HTTP 404 DeploymentNotFound: The API deployment for this resource does not exist."


def _result(success: bool, content: str = "", error: str = "") -> SimpleNamespace:
    payload = {"success": success, "content": content, "metadata": {"error": error} if error else {}}
    return SimpleNamespace(to_dict=lambda: payload)


def _fallback(*answers):
    orchestrator = object.__new__(NexusTaskOrchestrator)
    orchestrator.task_store = MagicMock()
    orchestrator.delegation = MagicMock()
    orchestrator.delegation.catalog.return_value = [
        {"provider": "azure_openai"},
        {"provider": "codex_readonly"},
    ]
    orchestrator.delegation.delegate = AsyncMock(side_effect=list(answers))
    task_logger = MagicMock()
    coroutine = orchestrator._external_fallback_completion(
        "COLLECTOR PROMPT",
        session_id="s1",
        budget_plan=compute_budget(model_context_tokens=32000),
        task_logger=task_logger,
        role="knowledge_collector",
        local_failure="memory differs from the current issue (unsure: p_yes=0.90 < 0.95)",
    )
    return orchestrator, task_logger, coroutine


def _fallback_events(orchestrator):
    return [
        call.kwargs | {"session_id": call.args[0]}
        for call in orchestrator.task_store.append_event.call_args_list
        if call.kwargs.get("kind") == "external_fallback"
    ]


def test_a_provider_error_and_the_next_providers_answer_are_task_events():
    orchestrator, task_logger, coroutine = _fallback(
        _result(False, error=NOT_FOUND),
        _result(True, content=_briefing("external summary")),
    )
    raw, provider = asyncio.run(coroutine)
    assert provider == "codex_readonly"

    failed, answered = _fallback_events(orchestrator)
    assert failed["session_id"] == "s1"
    assert failed["metadata"]["provider"] == "azure_openai"
    assert failed["metadata"]["success"] is False
    assert "DeploymentNotFound" in failed["metadata"]["error"]
    assert "DeploymentNotFound" in failed["content"]
    assert failed["metadata"]["role"] == "knowledge_collector"
    assert "p_yes=0.90" in failed["metadata"]["reason"]

    assert answered["metadata"]["provider"] == "codex_readonly"
    assert answered["metadata"]["success"] is True
    assert answered["metadata"]["attempt"] == 2
    assert answered["content"] == raw
    assert "error" not in answered["metadata"]

    # The controller's prompt history skips them.
    assert all(event["metadata"]["knowledge_collector_internal"] for event in (failed, answered))
    # The task log line names the error too.
    assert "DeploymentNotFound" in task_logger.info.call_args_list[0].args[2]


def test_all_providers_failing_still_leaves_one_event_each():
    orchestrator, _logger, coroutine = _fallback(
        _result(False, error=NOT_FOUND),
        _result(False, error="codex exited 1"),
    )
    with pytest.raises(ControllerBudgetExceededError):
        asyncio.run(coroutine)
    events = _fallback_events(orchestrator)
    assert [event["metadata"]["error"] for event in events] == [NOT_FOUND, "codex exited 1"]


def test_the_knowledge_collection_event_shows_each_probe():
    memory = [
        {"kind": "knowledge_collector", "content": "matching note"},
        {"kind": "knowledge_collector", "content": "outdated note"},
    ]
    _result_, metadata, _jobs, _local, _external = _run(p_yes=[0.98, 0.90], memory=memory)
    check = metadata["memory_check"]
    assert check["external_provider"] == "azure_openai"
    first, second = check["probes"]
    assert first["memory"] == "matching note" and first["escalate"] is False
    assert first["p_yes"] == 0.98 and first["token"] == "yes"
    assert second["memory"] == "outdated note" and second["escalate"] is True
    assert "p_yes=0.90" in second["reason"]
