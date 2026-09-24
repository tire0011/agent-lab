"""Issue #253: completion-evidence rejection must not become missing_capability.

This reproduces the state seen in Nexus session
cc428752d90544d0ab36a59b11785408 after issue #238 was read and an
implementation-intent answer was rejected because no state change had occurred.
"""

from __future__ import annotations

TASK = "solve github issue #238"

ISSUE_OBSERVATION = {
    "tool": "operation",
    "operation": "github_issue",
    "args": {"issue_number": 238},
    "result": {
        "success": True,
        "output_lines": [
            "issue: #238 memory_search indexes its own output and feeds the controller recursively nested results",
            "root cause: NexusMemory._search_task_events indexes memory_search/controller bookkeeping",
            "suggested fix: exclude orchestrator bookkeeping from the memory index",
            "acceptance: successive memory_search calls must not retrieve serialized earlier memory_search output",
        ],
    },
    "event_id": 343,
}

COMPLETION_REJECTION = {
    "event": "terminal_decision_rejected",
    "terminal_action": "answer",
    "information_gap": "implementation completion evidence",
    "gap_kind": "completion_evidence",
    "reason": (
        "Implementation-intent tasks cannot finish from read-only context alone; "
        "no state change was observed by the executor after any command."
    ),
    "candidate_operations": [],
    "retryable": True,
    "recommended_next_action": "missing_capability",
    "event_id": 345,
}


def test_completion_rejection_continues_local_development(controller_turn, live_result):
    decision = controller_turn(
        TASK,
        [ISSUE_OBSERVATION, COMPLETION_REJECTION],
        max_tokens=550,
    )

    live_result.update(
        reproduced_action=decision.get("action"),
        information_gap=decision.get("information_gap"),
        gap_kind=decision.get("gap_kind"),
        capability=decision.get("capability"),
    )

    assert decision.get("action") not in {"missing_capability", "clarification"}, (
        "Issue #253 reproduced: completion-evidence rejection was promoted into "
        f"a terminal blocker: {decision}"
    )
    assert decision.get("action") in {"command", "python_run", "new_experiment"}, (
        "After the issue is known and implementation completion was rejected, "
        f"Nexus should continue development, got: {decision}"
    )
