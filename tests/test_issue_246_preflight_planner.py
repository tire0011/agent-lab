"""Tests for issue #246 knowledge collector."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.task_orchestrator import NexusTaskOrchestrator, TaskOrchestratorError


def _orchestrator() -> NexusTaskOrchestrator:
    orchestrator = object.__new__(NexusTaskOrchestrator)
    orchestrator.operations_registry = SimpleNamespace(catalog=lambda: [])
    orchestrator.delegation = SimpleNamespace(catalog=lambda: [])
    return orchestrator


def test_knowledge_collector_prompt_describes_research_not_planning():
    orchestrator = _orchestrator()
    prompt = orchestrator._knowledge_collector_prompt(
        "solve github issue #238",
        sources=[],
        budget_plan=SimpleNamespace(
            current_input_chars=4000,
            observation_chars=8000,
        ),
    )

    lower = prompt.lower()
    assert "knowledge collector" in lower
    assert "compact factual briefing" in lower
    assert "do not plan the solution" in lower
    assert "remove duplicates" in lower
    assert "lookup" in lower
    assert "briefing" in lower


def test_parse_collector_accepts_read_only_lookup():
    orchestrator = _orchestrator()
    result = orchestrator._parse_knowledge_collector(
        '{"action":"lookup","source":"read_only",'
        '"query":"fetch GitHub issue #238","briefing":""}'
    )
    assert result == {
        "action": "lookup",
        "source": "read_only",
        "query": "fetch GitHub issue #238",
        "briefing": "",
    }


def test_parse_collector_accepts_memory_lookup():
    orchestrator = _orchestrator()
    result = orchestrator._parse_knowledge_collector(
        '{"action":"lookup","source":"memory",'
        '"query":"earlier Nexus controller architecture decision","briefing":""}'
    )
    assert result["action"] == "lookup"
    assert result["source"] == "memory"


def test_parse_collector_accepts_briefing():
    orchestrator = _orchestrator()
    result = orchestrator._parse_knowledge_collector(
        '{"action":"briefing","source":"none","query":"",'
        '"briefing":"Issue #238 is caused by NexusMemory indexing memory_search events."}'
    )
    assert result["action"] == "briefing"
    assert result["source"] == "none"
    assert "NexusMemory" in result["briefing"]


def test_lookup_requires_source_and_query():
    orchestrator = _orchestrator()
    with pytest.raises(TaskOrchestratorError):
        orchestrator._parse_knowledge_collector(
            '{"action":"lookup","source":"none","query":"","briefing":""}'
        )


def test_briefing_rejects_lookup_fields():
    orchestrator = _orchestrator()
    with pytest.raises(TaskOrchestratorError):
        orchestrator._parse_knowledge_collector(
            '{"action":"briefing","source":"memory","query":"old context",'
            '"briefing":"summary"}'
        )


def test_briefing_requires_text():
    orchestrator = _orchestrator()
    with pytest.raises(TaskOrchestratorError):
        orchestrator._parse_knowledge_collector(
            '{"action":"briefing","source":"none","query":"","briefing":""}'
        )


def test_knowledge_request_strips_development_intent_from_github_issue_task():
    orchestrator = _orchestrator()
    request = orchestrator._knowledge_request("solve github issue #238")
    assert request.startswith("Gather all relevant factual information about GitHub issue #238")
    assert "Do not solve or plan the task." in request


def test_knowledge_request_generic_task_is_research_only():
    orchestrator = _orchestrator()
    request = orchestrator._knowledge_request("fix the failing controller test")
    assert "Gather all relevant factual information needed to understand this task." in request
    assert "Do not solve or plan the task." in request
