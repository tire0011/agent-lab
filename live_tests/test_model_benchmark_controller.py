"""#302: controller decisions of the loaded local model, for comparing models.

Each scenario is one real controller turn on the production prompt; nothing is
executed. The test records action, verdict and seconds, so the same file run
against different models (scripts/model-benchmark.sh) gives a comparable table.
"""

from __future__ import annotations

import pytest

from modules.task_orchestrator import (
    ACTION_CLARIFICATION,
    ACTION_KNOWLEDGE_COLLECTOR,
    ACTION_NEW_EXPERIMENT,
)


def _briefing(text: str) -> dict:
    """A knowledge_collector observation as the controller sees it."""
    return {
        "tool": "knowledge_collector",
        "knowledge_briefing": text,
        "used_sources": ["memory_search", "github_issue"],
        "source_count": 3,
        "deterministic_retrieval": True,
    }


# The briefing the collector wrote in the real #301 run (2026-10-03).
BRIEFING_301 = (
    "GitHub issue #301: Log the answers of the models in the knowledge collector escalation. "
    "Observed: The knowledge collector escalated to the external fallback chain, but the logs and "
    "task events do not provide enough information to diagnose the issue. Wanted: External fallback "
    "logs, knowledge collection event, and memory check. Acceptance criteria: External provider errors "
    "in task events, successful answers in task events, and probe results visible in knowledge "
    "collection event."
)

BRIEFING_288 = (
    "GitHub issue #288: Code change in ExperimentService: startup recovery reruns an interrupted "
    "experiment once\nAfter #281 an experiment interrupted by a service restart is marked failed. "
    "Fix in modules/experiment_service/service.py: on the first interruption rerun the same "
    "experiment once under the same id with a fresh worktree; on a second interruption mark it failed "
    "with 'interrupted by service restart'. No change in the Nexus orchestrator or the mailbox; remove "
    "mailbox-side restart handling. Acceptance criteria: rerun once under the same id and the parked "
    "session receives the rerun's result; a second interruption fails; terminal experiments are not "
    "touched; tests cover it."
)

QUESTION = "What did the #297 experiment find about detecting outdated memory with the local model?"

SCENARIOS = {
    "301-initial": ("solve github issue #301", []),
    "301-after-briefing": ("solve github issue #301", [_briefing(BRIEFING_301)]),
    "288-after-briefing": ("solve github issue #288", [_briefing(BRIEFING_288)]),
    "question": (QUESTION, []),
}


def verdict(scenario: str, decision: dict) -> tuple[bool, str]:
    action = str(decision.get("action", "")).strip().lower()
    if scenario == "301-initial":
        ok = action == ACTION_KNOWLEDGE_COLLECTOR
        return ok, f"expected {ACTION_KNOWLEDGE_COLLECTOR} to read the issue first, got {action!r}"
    if scenario in {"301-after-briefing", "288-after-briefing"}:
        ok = action == ACTION_NEW_EXPERIMENT
        return ok, f"briefing describes a code change; expected {ACTION_NEW_EXPERIMENT}, got {action!r}"
    ok = action not in {ACTION_NEW_EXPERIMENT, ACTION_CLARIFICATION}
    return ok, f"a question needs no experiment and is clear; got {action!r}"


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_controller_decision(scenario, controller_turn, live_result):
    task, observations = SCENARIOS[scenario]
    decision = controller_turn(task, observations)
    ok, reason = verdict(scenario, decision)
    live_result.update(scenario=scenario, action=decision.get("action"), verdict=ok, reason=reason)
    assert ok, reason
