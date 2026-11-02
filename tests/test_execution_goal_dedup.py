"""#495: the execution goal drops user inputs a later input already repeats."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.nexus_controller_service import GOAL_CHAR_CAP, NexusControllerService

ISSUE = "GitHub Issue #386: Fix the stall detector\n\nThe detector misses experiments\nthat never log."


class ExecutionGoalDedupTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = MagicMock()
        self.orchestrator.experiment_service = MagicMock()
        self.orchestrator.experiment_service.db_path = ":memory:"
        self.orchestrator.operations_registry = SimpleNamespace(repo_root=Path("/tmp"))
        self.orchestrator.task_store = MagicMock()
        self.service = NexusControllerService(
            self.orchestrator, github_work_tracking=MagicMock()
        )

    def _goal(self, inputs: list[str]) -> str:
        self.orchestrator.task_store.user_inputs.return_value = inputs
        return self.service._execution_goal("session-1", "fallback")

    def _truncation_events(self) -> list[str]:
        return [
            call.kwargs["content"]
            for call in self.orchestrator.task_store.append_event.call_args_list
            if call.kwargs.get("kind") == "goal_truncated"
        ]

    def test_retry_with_earlier_attempts_keeps_issue_once(self):
        goal = self._goal([ISSUE, ISSUE + "\n\nEarlier attempts: x"])

        self.assertEqual(goal.count("GitHub Issue #386"), 1)
        self.assertTrue(goal.endswith("Earlier attempts: x"))
        self.assertNotIn("User follow-up:", goal)

    def test_chain_of_prefixes_keeps_only_the_last(self):
        first = ISSUE
        second = first + "\n\nEarlier attempts:\n- exp-1 failed"
        third = second + "\n- exp-2 failed"

        goal = self._goal([first, second, third])

        self.assertEqual(goal, third)

    def test_unrelated_followups_are_kept_with_separator(self):
        goal = self._goal([ISSUE, "please also do B"])

        self.assertEqual(goal, f"{ISSUE}\n\nUser follow-up:\nplease also do B")

    def test_inputs_differing_only_in_whitespace_collapse(self):
        spaced = ISSUE.replace("\n\n", "\n \n  ").replace(" the ", "   the ")

        goal = self._goal([ISSUE, spaced])

        self.assertEqual(goal.count("GitHub Issue #386"), 1)
        self.assertNotIn("User follow-up:", goal)

    def test_over_cap_keeps_title_and_newest_tail(self):
        first = "Title line of the task\n" + "a" * 20000
        followup = "b" * 10000 + "\nNEWEST-HANDOVER-END"

        goal = self._goal([first, first, followup])

        self.assertEqual(len(goal), GOAL_CHAR_CAP)
        self.assertTrue(goal.startswith("Title line of the task\n"))
        self.assertTrue(goal.endswith("NEWEST-HANDOVER-END"))
        self.assertIn("b" * 10000, goal)
        events = self._truncation_events()
        self.assertEqual(len(events), 1)
        self.assertIn("1 duplicate input(s) dropped", events[0])


if __name__ == "__main__":
    unittest.main()
