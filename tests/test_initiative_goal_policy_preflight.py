"""The initiative tick runs the goal policy before it picks an issue (#408)."""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest import mock

from modules.experiment_service.policy import _check_goal_policy
from modules.nexus_initiative import DECISION_SKIPPED_GOAL_POLICY
from tests.test_nexus_initiative import InitiativeFixture

# The issue suggested 'prints the actual api key', but since #413 a secret
# action in the goal only warns. A disallowed keyword is still rejected.
HARMFUL_BODY = "Clean up the cache by running rm -rf on the state directory."
HARMLESS_BODY = "Add unit tests for the cost monitor."


class RecordingExperimentService:
    def __init__(self) -> None:
        self.goals: list[str] = []

    def submit(self, goal: str, **_: object) -> None:
        self.goals.append(goal)


class GoalPolicyPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = InitiativeFixture()
        self.issues = [
            {"number": 11, "author": {"login": "tire0011"}, "title": "Cache cleanup", "body": HARMFUL_BODY},
            {"number": 12, "author": {"login": "tire0011"}, "title": "Cost monitor tests", "body": HARMLESS_BODY},
        ]
        self.svc = self.fx.service(issue_lister=lambda: [dict(i) for i in self.issues])
        self.svc.experiment_service = RecordingExperimentService()
        self.addCleanup(self.svc.close)

    def _skips(self) -> list[dict]:
        return [
            entry for entry in self.svc.history(limit=200)
            if entry["decision"] == DECISION_SKIPPED_GOAL_POLICY
        ]

    def _link(self, exp_id: str, issue: int, status: str, error: str) -> None:
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO experiments (id, goal, status, ended_at, error) "
                "VALUES (?, ?, ?, '2026-10-01T00:00:00+00:00', ?)",
                (exp_id, f"GitHub Issue #{issue}", status, error),
            )
            conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)",
                (exp_id, issue),
            )

    def test_harmful_issue_is_skipped_and_harmless_one_dispatched(self):
        self.assertIsNotNone(_check_goal_policy(HARMFUL_BODY))

        decision = self.svc.tick()

        self.assertTrue(decision.acted)
        self.assertEqual(decision.candidate.ref, "12")
        self.assertEqual(self.fx.controller.issues, [12])
        self.assertNotIn("11", [c.ref for c in decision.considered])
        self.assertEqual(self.svc.experiment_service.goals, [])
        skips = self._skips()
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["source_ref"], "11")
        self.assertIn("goal mentions disallowed keyword", skips[0]["reason"])
        detail = json.loads(skips[0]["detail"])
        self.assertEqual(len(detail["issue_hash"]), 64)
        self.assertEqual(len(detail["policy_hash"]), 64)

    def test_unchanged_issue_is_not_judged_again(self):
        self.svc.tick()
        self.issues = [i for i in self.issues if i["number"] == 11]

        with mock.patch(
            "modules.nexus_initiative.goal_policy._check_goal_policy",
            side_effect=AssertionError("policy run again for unchanged text"),
        ):
            second = self.svc.tick()

        self.assertEqual(second.decision, "idle")
        self.assertEqual(len(self._skips()), 1)
        self.assertEqual(self.fx.controller.issues, [12])

    def test_changed_body_is_considered_again(self):
        self.svc.tick()
        self.issues = [{"number": 11, "author": {"login": "tire0011"}, "title": "Cache cleanup", "body": "Expire cache entries after a day."}]

        decision = self.svc.tick()

        self.assertTrue(decision.acted)
        self.assertEqual(decision.candidate.ref, "11")
        self.assertEqual(self.fx.controller.issues, [12, 11])

    def test_changed_policy_hash_judges_the_issue_again(self):
        self.svc.tick()
        self.issues = [i for i in self.issues if i["number"] == 11]

        with mock.patch("modules.nexus_initiative._policy_hash", return_value="new-policy"):
            self.assertEqual(self.svc.tick().decision, "idle")

        self.assertEqual(len(self._skips()), 2)

    def test_other_rejection_is_still_retried(self):
        self.issues = [{"number": 12, "author": {"login": "tire0011"}, "title": "Cost monitor tests", "body": HARMLESS_BODY}]
        self._link("e1", 12, "rejected", "plan touches too many files (6); must be one small")

        decision = self.svc.tick()

        self.assertTrue(decision.acted)
        self.assertEqual(self.fx.controller.issues, [12])

    def test_goal_policy_rejection_is_not_retried_with_the_same_text(self):
        self.issues = [i for i in self.issues if i["number"] == 11]
        self._link("e1", 11, "rejected", "goal mentions disallowed keyword: 'rm -rf'")

        self.assertEqual(self.svc.tick().decision, "idle")
        self.assertEqual(self.fx.controller.issues, [])
        self.assertEqual(len(self._skips()), 1)

    def test_retry_after_goal_policy_rejection_does_not_quote_the_reason(self):
        self._link("e1", 12, "rejected", "goal mentions disallowed keyword: 'rm -rf'")

        self.assertEqual(self.svc._earlier_attempts(12), "")

    def test_dry_run_candidates_do_not_record_skips(self):
        found = self.svc.candidates(record_skips=False)

        self.assertEqual([c.ref for c in found], ["12"])
        self.assertEqual(self._skips(), [])


if __name__ == "__main__":
    unittest.main()
