"""#301: the knowledge collector escalation logs the models' answers and errors."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.nexus_delegation import DelegationResult
from modules.prompt_budget import compute_budget
from modules.task_orchestrator import ControllerBudgetExceededError, NexusTaskOrchestrator
from tests.test_issue_297_knowledge_escalation import (
    OLD_MEMORY,
    _briefing,
    _issue_observation,
    _logprobs,
)

AZURE_404 = DelegationResult(
    "azure_openai",
    False,
    "",
    {
        "error": "HTTP 404",
        "http_status": 404,
        "deployment": "model-router",
        "error_detail": '{"error":{"code":"DeploymentNotFound"}}',
    },
)


def _orchestrator(*results: DelegationResult) -> NexusTaskOrchestrator:
    orchestrator = object.__new__(NexusTaskOrchestrator)
    orchestrator.delegation = MagicMock()
    orchestrator.delegation.catalog.return_value = [
        {"provider": "azure_openai"},
        {"provider": "codex_readonly"},
    ]
    orchestrator.delegation.delegate = AsyncMock(side_effect=list(results))
    return orchestrator


def _fallback(orchestrator, attempt_log, task_logger=None):
    return asyncio.run(
        orchestrator._external_fallback_completion(
            "COLLECTOR PROMPT",
            session_id="s1",
            budget_plan=compute_budget(model_context_tokens=32000),
            task_logger=task_logger,
            role="knowledge_collector",
            attempt_log=attempt_log,
        )
    )


def test_each_provider_attempt_keeps_its_error_or_answer(caplog):
    codex = DelegationResult("codex_readonly", True, _briefing("external"), {"task_id": "t1"})
    orchestrator = _orchestrator(AZURE_404, codex)
    attempts: list[dict] = []
    task_logger = MagicMock()

    with caplog.at_level(logging.INFO, logger="modules.task_orchestrator"):
        raw, provider = _fallback(orchestrator, attempts, task_logger)

    assert provider == "codex_readonly"
    azure, answered = attempts
    assert azure == {
        "provider": "azure_openai",
        "success": False,
        "error": "HTTP 404",
        "error_detail": '{"error":{"code":"DeploymentNotFound"}}',
        "http_status": 404,
        "deployment": "model-router",
    }
    assert answered["success"] is True and answered["answer"] == raw
    assert answered["task_id"] == "t1"
    messages = [call.args[2] for call in task_logger.info.call_args_list]
    assert "DeploymentNotFound" in messages[0] and "HTTP 404" in messages[0]
    assert "external" in messages[1]
    assert task_logger.info.call_args_list[0].kwargs["deployment"] == "model-router"
    assert "DeploymentNotFound" in caplog.text and "answer=" in caplog.text


def test_when_every_provider_fails_the_error_names_each_reason():
    codex = DelegationResult("codex_readonly", False, "", {"error": "RuntimeError"})
    orchestrator = _orchestrator(AZURE_404, codex)
    attempts: list[dict] = []
    with pytest.raises(ControllerBudgetExceededError) as raised:
        _fallback(orchestrator, attempts)
    assert "azure_openai: HTTP 404" in str(raised.value)
    assert "codex_readonly: RuntimeError" in str(raised.value)
    assert [item["provider"] for item in attempts] == ["azure_openai", "codex_readonly"]


def test_an_answer_with_a_secret_is_redacted_in_the_attempt():
    leaked = DelegationResult("azure_openai", True, "api_key=sk-abcdefghijklmnopqrstuvwxyz123456", {})
    attempts: list[dict] = []
    _fallback(_orchestrator(leaked), attempts)
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in attempts[0]["answer"]


def _collect(*delegations: DelegationResult, p_yes, memory=None):
    """Run the collector with the real external chain and a mocked delegation."""
    orchestrator = _orchestrator(*delegations)
    orchestrator.task_store = MagicMock()
    orchestrator.task_store.append_event.return_value = 1
    orchestrator.memory = MagicMock()
    orchestrator.memory.search.return_value = memory or [{"content": OLD_MEMORY}]
    orchestrator.llm_jobs = MagicMock()
    orchestrator.llm_jobs.submit.return_value = "probe-job"
    p_values = p_yes if isinstance(p_yes, list) else [p_yes]
    orchestrator.llm_jobs.wait = AsyncMock(side_effect=[_logprobs(p) for p in p_values])
    local = AsyncMock(return_value=(_briefing("local summary"), "local"))
    task_logger = MagicMock()
    patches = [
        patch.object(
            orchestrator,
            "_resolve_find_tool",
            AsyncMock(return_value={"tool": "find_tool", "status": "found", "resolved_tool": "github_issue"}),
        ),
        patch.object(orchestrator, "_execute_resolved_tool", return_value=_issue_observation()),
        patch.object(orchestrator, "_mark_already_executed", side_effect=lambda source, _executed: source),
        patch.object(orchestrator, "_state_token", return_value="token"),
        patch.object(orchestrator, "_schema_completion", local),
    ]
    for item in patches:
        item.start()
    try:
        result = asyncio.run(
            orchestrator._run_knowledge_collector(
                SimpleNamespace(id="s1"),
                "solve github issue #288",
                compute_budget(model_context_tokens=32000),
                task_logger=task_logger,
            )
        )
    finally:
        for item in patches:
            item.stop()
    events = [call.kwargs for call in orchestrator.task_store.append_event.call_args_list]
    collection = next(event for event in events if event.get("kind") == "knowledge_collection")
    # The event metadata is stored as JSON; it must stay serializable.
    json.dumps(collection["metadata"])
    return result, collection["metadata"], task_logger


def test_the_collection_event_records_the_external_attempts():
    codex = DelegationResult("codex_readonly", True, _briefing("external summary"), {"task_id": "t1"})
    result, metadata, _task_logger = _collect(AZURE_404, codex, p_yes=0.5)
    assert "external summary" in result["knowledge_briefing"]
    attempts = metadata["memory_check"]["external_attempts"]
    assert attempts[0]["error"] == "HTTP 404" and attempts[0]["deployment"] == "model-router"
    assert "DeploymentNotFound" in attempts[0]["error_detail"]
    assert attempts[1]["provider"] == "codex_readonly" and "external summary" in attempts[1]["answer"]


def test_a_rejected_external_answer_stays_in_the_event():
    lookup = json.dumps({"action": "lookup", "source": "memory", "query": "issue 288", "briefing": ""})
    codex = DelegationResult("codex_readonly", True, lookup, {})
    result, metadata, _task_logger = _collect(AZURE_404, codex, p_yes=0.5)
    check = metadata["memory_check"]
    assert "local summary" in result["knowledge_briefing"]
    assert metadata["provider_used"] == "local"
    assert check["rejected_provider"] == "codex_readonly"
    assert json.loads(check["rejected_answer"])["action"] == "lookup"
    assert "action=lookup" in check["escalation_failed"]


def test_an_escalation_where_every_provider_fails_keeps_the_errors():
    codex = DelegationResult("codex_readonly", False, "", {"error": "RuntimeError"})
    result, metadata, _task_logger = _collect(AZURE_404, codex, p_yes=0.5)
    check = metadata["memory_check"]
    assert "local summary" in result["knowledge_briefing"]
    assert [item["error"] for item in check["external_attempts"]] == ["HTTP 404", "RuntimeError"]
    assert "azure_openai: HTTP 404" in check["escalation_failed"]


def test_the_memory_check_records_every_entry_probe_and_is_logged():
    memory = [
        {"kind": "knowledge_collector", "content": "first note"},
        {"kind": "knowledge_collector", "content": "second note"},
    ]
    _result, metadata, task_logger = _collect(p_yes=[0.97, 0.98], memory=memory)
    check = metadata["memory_check"]
    assert check["escalated"] is False and "external_attempts" not in check
    first, second = check["entries"]
    assert first["entry"] == "first note" and second["entry"] == "second note"
    assert first["p_yes"] == 0.97 and first["token"] == "yes"
    assert first["top_tokens"] == ["'Yes'", "'No'"]
    assert first["reason"].startswith("yes with p_yes=")
    logged = [call for call in task_logger.info.call_args_list if call.args[1] == "knowledge_memory_check"]
    assert len(logged) == 1
    assert logged[0].kwargs["memory_check"]["entries"] == check["entries"]
