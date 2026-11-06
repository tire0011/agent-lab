"""#464: a work link without an experiment does not block its issue forever."""

from __future__ import annotations

import sqlite3
import unittest

from tests.test_nexus_initiative import InitiativeFixture

ISSUE = [{"number": 455, "author": {"login": "tire0011"}, "title": "Adoption of a retry", "body": ""}]


class UnboundWorkLinkTests(unittest.TestCase):
    def setUp(self):
        self.fx = InitiativeFixture()
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("CREATE TABLE github_work_links (issue_number INTEGER, experiment_id TEXT)")
            conn.execute("INSERT INTO github_work_links VALUES (455, NULL)")

    def _link(self, exp_id, status):
        self.fx.add_experiment(exp_id, status)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)", (exp_id, 455)
            )

    def _tick(self):
        svc = self.fx.service(issue_lister=lambda: ISSUE)
        self.addCleanup(svc.close)
        return svc.tick()

    def test_failed_attempt_is_retried_despite_the_unbound_link(self):
        self._link("e1", "needs_review")
        self.assertEqual(self._tick().decision, "dispatched")
        self.assertEqual(self.fx.controller.issues, [455])

    def test_running_experiment_still_blocks(self):
        self._link("e1", "succeeded")
        self._tick()
        self.assertEqual(self.fx.controller.issues, [])

    def test_dangling_experiment_id_still_blocks(self):
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("INSERT INTO github_work_links VALUES (455, 'gone')")
        self._tick()
        self.assertEqual(self.fx.controller.issues, [])


if __name__ == "__main__":
    unittest.main()
