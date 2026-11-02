"""#374: an issue whose experiment failed is offered again, at most twice.

#235 timed out on a hanging test and was then never picked again.
"""

from __future__ import annotations

import sqlite3
import unittest

from tests.test_nexus_initiative import InitiativeFixture

ISSUE = [{"number": 235, "title": "Self-restart fallback", "body": ""}]


class IssueRetryTests(unittest.TestCase):
    def setUp(self):
        self.fx = InitiativeFixture()

    def _attempt(self, exp_id, status):
        self.fx.add_experiment(exp_id, status)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)",
                (exp_id, 235),
            )

    def _tick(self):
        svc = self.fx.service(issue_lister=lambda: ISSUE)
        self.addCleanup(svc.close)
        return svc.tick()

    def test_one_failed_attempt_is_offered_again(self):
        self._attempt("e1", "timed_out")
        self.assertEqual(self._tick().decision, "dispatched")
        self.assertEqual(self.fx.controller.issues, [235])

    def test_two_failed_attempts_end_it(self):
        self._attempt("e1", "timed_out")
        self._attempt("e2", "failed")
        self.assertEqual(self._tick().decision, "idle")
        self.assertEqual(self.fx.controller.issues, [])

    def test_a_succeeded_experiment_still_blocks(self):
        self._attempt("e1", "succeeded")
        self._tick()
        self.assertEqual(self.fx.controller.issues, [])

    def test_needs_review_counts_as_a_failed_attempt(self):
        # A false gate left #185, #349 and #378 stuck in needs_review.
        self._attempt("e1", "needs_review")
        self.assertEqual(self._tick().decision, "dispatched")
        self.assertEqual(self.fx.controller.issues, [235])

    def test_a_failure_next_to_a_success_still_blocks(self):
        self._attempt("e1", "failed")
        self._attempt("e2", "succeeded")
        self._tick()
        self.assertEqual(self.fx.controller.issues, [])


if __name__ == "__main__":
    unittest.main()
