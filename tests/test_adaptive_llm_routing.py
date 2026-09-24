"""Focused tests for Nexus adaptive LLM routing and fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.task_orchestrator import NexusTaskOrchestrator


class _Result:
    def __init__(self, provider: str, success: bool, error: str = "") -> None:
        self.provider = provider
        self.success = success
        self.error = error

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "success": self.success,
            "content": "ok" if self.success else "",
            "metadata": {} if self.success else {"error": self.error},
        }


class _Delegation:
    def __init__(self, outcomes: dict[str, bool]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def catalog(self) -> list[dict]:
        return [{"provider": provider} for provider in self.outcomes]

    async def delegate(self, provider: str, task: str, *, context: str = "") -> _Result:
        self.calls.append(provider)
        success = self.outcomes[provider]
        return _Result(provider, success, "quota_or_provider_error")


def _orchestrator(outcomes: dict[str, bool]) -> NexusTaskOrchestrator:
    orchestrator = object.__new__(NexusTaskOrchestrator)
    orchestrator.delegation = _Delegation(outcomes)
    return orchestrator


@pytest.mark.asyncio
async def test_azure_failure_falls_back_to_local_before_codex():
    orchestrator = _orchestrator(
        {"azure_openai": False, "local_worker": True, "codex_readonly": True}
    )

    result = await orchestrator._delegate_with_fallback(
        "azure_openai", "implement a medium change", reason="programming task"
    )

    assert orchestrator.delegation.calls == ["azure_openai", "local_worker"]
    assert result["requested_provider"] == "azure_openai"
    assert result["provider_used"] == "local_worker"
    assert result["fallback_count"] == 1


@pytest.mark.asyncio
async def test_local_failure_escalates_to_azure_then_codex():
    orchestrator = _orchestrator(
        {"local_worker": False, "azure_openai": False, "codex_readonly": True}
    )

    result = await orchestrator._delegate_with_fallback("local_worker", "harder task")

    assert orchestrator.delegation.calls == [
        "local_worker",
        "azure_openai",
        "codex_readonly",
    ]
    assert result["provider_used"] == "codex_readonly"
    assert result["fallback_count"] == 2


@pytest.mark.asyncio
async def test_all_provider_failures_are_returned_as_observation():
    orchestrator = _orchestrator(
        {"azure_openai": False, "local_worker": False, "codex_readonly": False}
    )

    result = await orchestrator._delegate_with_fallback("azure_openai", "task")

    assert result["provider_used"] is None
    assert result["result"]["success"] is False
    assert [item["provider"] for item in result["attempts"]] == [
        "azure_openai",
        "local_worker",
        "codex_readonly",
    ]


def test_prompt_keeps_local_model_as_real_development_path():
    orchestrator = object.__new__(NexusTaskOrchestrator)
    orchestrator.operations_registry = SimpleNamespace(catalog=lambda: [])
    orchestrator.delegation = SimpleNamespace(catalog=lambda: [])
    session = SimpleNamespace(source_type="task", source_id="1", title="test")

    prompt = orchestrator._prompt(session, "fix a small file", [], [])

    assert "You are not a tool router" in prompt
    assert "Nexus owns the task" in prompt
    assert "use `command` or `python_run` directly" in prompt
    assert "must not be discovered through find_tool" in prompt
    assert "medium programming work" in prompt
    assert "Codex implementation" in prompt
    assert "quota/token limited" in prompt
