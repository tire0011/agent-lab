"""A review that blocks the merge sends the issue straight back to the coder."""

from __future__ import annotations

import sqlite3
import unittest

from modules.nexus_initiative import REVIEW_RETRY_REASON
from tests.test_nexus_initiative import InitiativeFixture

ISSUES = [
    {"number": 7, "author": {"login": "tire0011"}, "title": "Something new", "body": ""},
    {"number": 418, "author": {"login": "tire0011"}, "title": "Refuse a second experiment", "body": ""},
]


class ReviewRetryTests(unittest.TestCase):
    def setUp(self):
        self.fx = InitiativeFixture()
        self.fx.add_experiment("e1", "succeeded", adoption="failed")
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)", ("e1", 418)
            )
            conn.execute(
                "UPDATE experiment_adoptions SET reason = ? WHERE experiment_id = ?",
                ("Review by codex blocked the merge: not atomic", "e1"),
            )

    def _service(self, **kwargs):
        svc = self.fx.service(issue_lister=lambda: ISSUES, **kwargs)
        self.addCleanup(svc.close)
        return svc

    def test_review_retry_goes_before_new_issues(self):
        decision = self._service().tick()
        self.assertEqual(decision.candidate.reason, REVIEW_RETRY_REASON)
        self.assertEqual(self.fx.controller.issues, [418])

    def test_review_retry_runs_with_an_exhausted_budget_and_costs_nothing(self):
        svc = self._service(daily_budget=1)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO nexus_initiative_log (tick_id, created_at, decision) VALUES (?, datetime('now'), ?)",
                ("old", "dispatched"),
            )
        self.assertEqual(svc._spent_today(), 1)

        self.assertEqual(svc.tick().decision, "dispatched")
        self.assertEqual(self.fx.controller.issues, [418])
        self.assertEqual(svc._spent_today(), 1)

    def test_other_failures_wait_for_the_budget(self):
        svc = self._service(daily_budget=1)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("UPDATE experiment_adoptions SET reason = 'merge conflict'")
            conn.execute(
                "INSERT INTO nexus_initiative_log (tick_id, created_at, decision) VALUES (?, datetime('now'), ?)",
                ("old", "dispatched"),
            )
        self.assertEqual(svc.tick().decision, "budget_exhausted")


if __name__ == "__main__":
    unittest.main()
