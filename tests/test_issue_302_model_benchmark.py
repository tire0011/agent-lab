"""#302: verdicts of the controller model benchmark."""

from __future__ import annotations

from live_tests.test_model_benchmark_controller import SCENARIOS, verdict


def test_reading_the_issue_first_passes():
    assert verdict("301-initial", {"action": "knowledge_collector"})[0]
    assert not verdict("301-initial", {"action": "new_experiment"})[0]


def test_a_code_change_briefing_needs_an_experiment():
    for scenario in ("301-after-briefing", "288-after-briefing"):
        assert verdict(scenario, {"action": "new_experiment"})[0]
        assert not verdict(scenario, {"action": "delegate"})[0]
        assert not verdict(scenario, {"action": "clarification"})[0]


def test_a_question_needs_neither_an_experiment_nor_a_clarification():
    assert verdict("question", {"action": "knowledge_collector"})[0]
    assert not verdict("question", {"action": "new_experiment"})[0]
    assert not verdict("question", {"action": "clarification"})[0]


def test_every_scenario_has_a_task():
    assert all(task for task, _observations in SCENARIOS.values())
