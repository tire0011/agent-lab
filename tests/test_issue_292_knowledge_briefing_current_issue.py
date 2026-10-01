"""#292: the knowledge briefing follows the current issue, not older memory."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modules.task_orchestrator import NexusTaskOrchestrator, TaskOrchestratorError

CURRENT_TITLE = "Code change in ExperimentService: startup recovery reruns an interrupted experiment once"
OLD_MEMORY = "GitHub issue #288: An experiment interrupted by a restart should be resumed."


def _issue_observation(title: str = CURRENT_TITLE) -> dict:
    meta = {"number": 288, "title": title, "state": "open"}
    return {
        "tool": "operation",
        "operation": "github_issue",
        "result": {
            "operation": "github_issue",
            "success": True,
            "output_lines": [f"issue: {json.dumps(meta)}", "body[1]: current text"],
        },
    }


def _orchestrator(briefing: str, *, fetch_fails: bool = False) -> tuple[NexusTaskOrchestrator, dict, list]:
    orchestrator = object.__new__(NexusTaskOrchestrator)
    orchestrator.task_store = MagicMock()
    orchestrator.task_store.append_event.return_value = 1
    orchestrator.memory = MagicMock()
    orchestrator.memory.search.return_value = [{"content": OLD_MEMORY}]
    seen: dict = {}

    async def completion(prompt, **_kwargs):
        seen["prompt"] = prompt
        return json.dumps({"action": "briefing", "source": "none", "query": "", "briefing": briefing}), "local"

    patches = [
        patch.object(
            orchestrator,
            "_resolve_find_tool",
            AsyncMock(side_effect=TaskOrchestratorError("github unavailable"))
            if fetch_fails
            else AsyncMock(return_value={"tool": "find_tool", "status": "found", "resolved_tool": "github_issue"}),
        ),
        patch.object(orchestrator, "_execute_resolved_tool", return_value=_issue_observation()),
        patch.object(orchestrator, "_mark_already_executed", side_effect=lambda source, _executed: source),
        patch.object(orchestrator, "_state_token", return_value="token"),
        patch.object(orchestrator, "_schema_completion", side_effect=completion),
    ]
    for item in patches:
        item.start()
    return orchestrator, seen, patches


def _run(briefing: str, *, fetch_fails: bool = False):
    orchestrator, seen, patches = _orchestrator(briefing, fetch_fails=fetch_fails)
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
    return result, seen["prompt"]


def test_memory_is_listed_first_and_marked_as_possibly_outdated():
    _result, prompt = _run("summary")
    assert prompt.index(OLD_MEMORY) < prompt.index("current text")
    assert "older version of this issue" in prompt
    assert "the current source wins" in prompt


def test_briefing_starts_with_the_current_title_verbatim():
    result, _prompt = _run("An experiment interrupted by a restart should be resumed.")
    assert result["knowledge_briefing"].startswith(f"GitHub issue #288: {CURRENT_TITLE}\n")


def test_a_briefing_that_already_has_the_title_is_not_prefixed_twice():
    heading = f"GitHub issue #288: {CURRENT_TITLE}"
    result, _prompt = _run(f"{heading}. More facts.")
    assert result["knowledge_briefing"].count(heading) == 1


def test_no_heading_without_a_successfully_fetched_issue():
    failed = _issue_observation()
    failed["result"]["success"] = False
    assert NexusTaskOrchestrator._fetched_issue_heading([failed]) is None
    assert NexusTaskOrchestrator._fetched_issue_heading([]) is None


def test_a_failed_fetch_keeps_the_previous_source_order_and_briefing():
    # Review on #293: without a current issue nothing is reordered or marked.
    result, prompt = _run("summary from memory", fetch_fails=True)
    assert prompt.index("knowledge_source_error") < prompt.index(OLD_MEMORY)
    assert "older version of this issue" not in prompt
    assert result["knowledge_briefing"] == "summary from memory"
