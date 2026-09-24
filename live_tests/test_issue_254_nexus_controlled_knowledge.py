"""Issue #254: Nexus owns the task and chooses knowledge collection itself.

These are real local-controller decisions on Nexus. Nothing is executed.
"""

from __future__ import annotations


def test_unknown_issue_uses_knowledge_collector(controller_turn, live_result):
    decision = controller_turn("solve github issue #238", [], max_tokens=550)
    live_result.update(scenario="unknown_issue", decision_action=decision.get("action"))
    assert decision.get("action") == "knowledge_collector", decision
    request = str(decision.get("request", ""))
    assert "238" in request, decision


KNOWN_ISSUE = {
    "tool": "knowledge_collector",
    "knowledge_briefing": (
        "GitHub issue #238: NexusMemory._search_task_events indexes memory_search and "
        "other controller bookkeeping. Earlier serialized search results can therefore "
        "be retrieved recursively. The requested fix is to exclude orchestration "
        "bookkeeping from searchable memory. Acceptance includes successive searches "
        "not retrieving earlier memory_search output."
    ),
    "event_id": 343,
}


def test_known_issue_proceeds_to_local_work(controller_turn, live_result):
    decision = controller_turn(
        "solve github issue #238",
        [KNOWN_ISSUE],
        max_tokens=550,
    )
    live_result.update(scenario="known_issue", decision_action=decision.get("action"))
    assert decision.get("action") in {"command", "python_run", "new_experiment"}, decision


COMPLETION_REJECTION = {
    "event": "terminal_decision_rejected",
    "terminal_action": "answer",
    "information_gap": "implementation completion evidence",
    "gap_kind": "completion_evidence",
    "reason": (
        "Implementation-intent tasks cannot finish from read-only context alone; "
        "no state change was observed by the executor after any command. Continue "
        "working on the original task; this meta-gap is not a missing capability."
    ),
    "candidate_operations": [],
    "retryable": True,
    "recommended_next_action": "continue_work",
    "event_id": 345,
}


def test_completion_rejection_continues_original_task(controller_turn, live_result):
    decision = controller_turn(
        "solve github issue #238",
        [KNOWN_ISSUE, COMPLETION_REJECTION],
        max_tokens=550,
    )
    live_result.update(
        scenario="completion_rejection",
        decision_action=decision.get("action"),
        information_gap=decision.get("information_gap"),
        gap_kind=decision.get("gap_kind"),
    )
    assert decision.get("action") in {"command", "python_run", "new_experiment"}, decision
