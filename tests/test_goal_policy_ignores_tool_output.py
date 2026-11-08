"""The goal keyword check judges the task, not quoted tool output (#544)."""

from __future__ import annotations

import unittest

from modules.experiment_handover import EARLIER_ATTEMPTS_HEADING, HANDOVER_HEADING
from modules.experiment_service.policy import _check_goal_policy

TASK = "GitHub Issue #276: Local model as a coding agent for new_experiment"


def _goal(nexus_goal: str, attempts: str = "", earlier: str = "") -> str:
    handover = f"{HANDOVER_HEADING}\n\nNexus goal:\n{nexus_goal}"
    if attempts:
        handover += f"\n\nEarlier attempts in this run:\n{attempts}"
    text = TASK + (f"\n\n{EARLIER_ATTEMPTS_HEADING}\n{earlier}" if earlier else "")
    return f"{text}\n\n{handover}"


class GoalPolicyToolOutputTests(unittest.TestCase):
    def test_a_quoted_docstring_in_tool_output_does_not_reject(self):
        # Experiment 955fecd9: grep output quoted "never ``shell=True``".
        goal = _goal(
            "Add a local coding provider.",
            attempts="- command grep -rn worker modules: modules/claude_worker.py:4:``shell=True``)",
        )
        self.assertIsNone(_check_goal_policy(goal))

    def test_an_old_rejection_reason_does_not_reject_the_retry(self):
        goal = _goal("Add a local coding provider.", earlier="- 955fecd9: goal mentions disallowed keyword: 'shell=true'")
        self.assertIsNone(_check_goal_policy(goal))

    def test_the_task_itself_is_still_checked(self):
        self.assertIsNotNone(_check_goal_policy("Run the worker with shell=True"))

    def test_nexus_own_goal_is_still_checked(self):
        goal = _goal("Clean up with rm -rf /agent/state", attempts="- command ls: ok")
        self.assertIsNotNone(_check_goal_policy(goal))


if __name__ == "__main__":
    unittest.main()
