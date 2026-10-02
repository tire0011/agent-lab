"""#297: the knowledge collector escalates when memory and the current issue disagree."""

from __future__ import annotations

import asyncio
import json
import math
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modules.local_llm_jobs import LocalLlmJobService
from modules.task_orchestrator import ControllerBudgetExceededError, NexusTaskOrchestrator

TITLE = "Code change in ExperimentService: startup recovery reruns an interrupted experiment once"
OLD_MEMORY = "GitHub issue #288: An experiment interrupted by a restart should be resumed."


def _logprobs(p_yes: float) -> str:
    top = [
        {"token": "Yes", "logprob": math.log(p_yes)},
        {"token": "No", "logprob": math.log(1 - p_yes)},
    ]
    first = top[0] if p_yes >= 0.5 else top[1]
    return json.dumps({"choices": [{"logprobs": {"content": [{**first, "top_logprobs": top}]}}]})


def _briefing(text: str) -> str:
    return json.dumps({"action": "briefing", "source": "none", "query": "", "briefing": text})


def _issue_observation() -> dict:
    meta = {"number": 288, "title": TITLE, "state": "open"}
    return {
        "tool": "operation",
        "operation": "github_issue",
        "result": {
            "operation": "github_issue",
            "success": True,
            "output_lines": [f"issue: {json.dumps(meta)}", "body[1]: current text"],
        },
    }


def _run(*, p_yes: float | None, memory: list | None = None, external=None, issue_ok: bool = True):
    orchestrator = object.__new__(NexusTaskOrchestrator)
    orchestrator.task_store = MagicMock()
    orchestrator.task_store.append_event.return_value = 1
    orchestrator.memory = MagicMock()
    orchestrator.memory.search.return_value = [{"content": OLD_MEMORY}] if memory is None else memory
    orchestrator.llm_jobs = MagicMock()
    orchestrator.llm_jobs.submit.return_value = "probe-job"
    orchestrator.llm_jobs.wait = AsyncMock(return_value=_logprobs(p_yes) if p_yes is not None else "")
    local = AsyncMock(return_value=(_briefing("local summary"), "local"))
    external = external or AsyncMock(return_value=(_briefing("external summary"), "azure_openai"))
    observation = _issue_observation()
    observation["result"]["success"] = issue_ok
    patches = [
        patch.object(
            orchestrator,
            "_resolve_find_tool",
            AsyncMock(return_value={"tool": "find_tool", "status": "found", "resolved_tool": "github_issue"}),
        ),
        patch.object(orchestrator, "_execute_resolved_tool", return_value=observation),
        patch.object(orchestrator, "_mark_already_executed", side_effect=lambda source, _executed: source),
        patch.object(orchestrator, "_state_token", return_value="token"),
        patch.object(orchestrator, "_schema_completion", local),
        patch.object(orchestrator, "_external_fallback_completion", external),
    ]
    for item in patches:
        item.start()
    try:
        result = asyncio.run(
            orchestrator._run_knowledge_collector(
                SimpleNamespace(id="s1"),
                "solve github issue #288",
                SimpleNamespace(current_input_chars=4000, observation_chars=20000),
            )
        )
    finally:
        for item in patches:
            item.stop()
    events = [call.kwargs for call in orchestrator.task_store.append_event.call_args_list]
    collection = next(event for event in events if event.get("kind") == "knowledge_collection")
    return result, collection["metadata"], orchestrator.llm_jobs, local, external


def test_outdated_memory_lets_an_external_model_write_the_briefing():
    result, metadata, jobs, local, external = _run(p_yes=0.90)
    assert "external summary" in result["knowledge_briefing"]
    assert metadata["provider_used"] == "azure_openai"
    assert metadata["memory_check"]["escalated"] is True
    assert metadata["memory_check"]["p_yes"] == 0.9
    assert external.await_args.kwargs["role"] == "knowledge_collector"
    local.assert_not_awaited()
    assert jobs.submit.call_args.kwargs["top_logprobs"] == 5


def test_matching_memory_stays_local():
    result, metadata, _jobs, local, external = _run(p_yes=0.97)
    assert "local summary" in result["knowledge_briefing"]
    assert metadata["provider_used"] == "local"
    assert metadata["memory_check"]["escalated"] is False
    external.assert_not_awaited()
    local.assert_awaited_once()


def test_no_memory_results_means_no_probe():
    _result, metadata, jobs, _local, external = _run(p_yes=0.1, memory=[])
    jobs.submit.assert_not_called()
    assert "memory_check" not in metadata
    external.assert_not_awaited()


def test_no_fetched_issue_means_no_probe():
    _result, metadata, jobs, _local, _external = _run(p_yes=0.1, issue_ok=False)
    jobs.submit.assert_not_called()
    assert "memory_check" not in metadata


def test_escalation_without_an_external_provider_falls_back_to_local():
    failing = AsyncMock(side_effect=ControllerBudgetExceededError("no external provider is configured"))
    result, metadata, _jobs, local, _external = _run(p_yes=0.5, external=failing)
    assert "local summary" in result["knowledge_briefing"]
    assert metadata["memory_check"]["escalated"] is True
    assert "no external provider" in metadata["memory_check"]["escalation_failed"]
    local.assert_awaited_once()


def test_a_failed_probe_never_blocks_the_briefing():
    result, metadata, _jobs, local, external = _run(p_yes=None)
    assert "local summary" in result["knowledge_briefing"]
    assert "memory_check" not in metadata
    external.assert_not_awaited()


class _LogprobClient:
    model = "test-local-model"

    def __init__(self):
        self.chat_calls = []

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None, system=None):
        return "reply:" + prompt

    async def chat_completion(self, messages, temperature=0.0, max_tokens=1024, **kwargs):
        self.chat_calls.append({"messages": messages, "max_tokens": max_tokens, **kwargs})
        return {"choices": [{"message": {"content": "Yes"}, "logprobs": {"content": [{"token": "Yes"}]}}]}


def test_the_job_queue_returns_logprobs_only_when_asked():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            client = _LogprobClient()
            service = LocalLlmJobService(
                db_path=str(Path(tmp) / "jobs.db"), llama_client=client, poll_interval=0.01
            )
            try:
                plain = await service.wait(service.submit("plain"))
                probed = await service.wait(service.submit("probe", max_tokens=2, top_logprobs=5))
            finally:
                service.close()
            return client, plain, probed

    client, plain, probed = asyncio.run(scenario())
    assert plain == "reply:plain"
    assert json.loads(probed)["choices"][0]["logprobs"]["content"][0]["token"] == "Yes"
    assert len(client.chat_calls) == 1
    assert client.chat_calls[0]["logprobs"] is True and client.chat_calls[0]["top_logprobs"] == 5


def test_a_large_escalated_prompt_keeps_the_current_issue_and_the_instruction():
    # Review on #298: the external path cuts prompts from the end, which would
    # drop the current issue and the output instruction. Memory shrinks instead.
    from modules.prompt_budget import compute_budget
    from modules.task_orchestrator import _EXTERNAL_PREAMBLE_CHARS, _EXTERNAL_PROMPT_CHARS

    orchestrator = object.__new__(NexusTaskOrchestrator)
    budget = compute_budget(model_context_tokens=32000)
    observation = _issue_observation()
    observation["result"]["output_lines"].append("body[2]: CURRENT-ISSUE-TAIL")
    sources = [
        {"tool": "memory_search", "query": "GitHub issue #288", "results": [{"content": OLD_MEMORY * 400}]},
        observation,
    ]
    request = "Gather all relevant factual information about GitHub issue #288."
    prompt = orchestrator._knowledge_collector_prompt(request, sources, budget, force_briefing=True)
    assert len(prompt) > _EXTERNAL_PROMPT_CHARS

    sized = orchestrator._external_collector_prompt(request, sources, budget, prompt)

    assert len(sized) <= _EXTERNAL_PROMPT_CHARS - _EXTERNAL_PREAMBLE_CHARS
    assert "CURRENT-ISSUE-TAIL" in sized
    assert sized.endswith("Return exactly one JSON object matching the schema.")
