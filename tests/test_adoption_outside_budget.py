"""Adopting fresh verified work does not use the daily budget (#538)."""

from __future__ import annotations

import sqlite3
import unittest

from tests.test_nexus_initiative import InitiativeFixture


class AdoptionOutsideBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fx = InitiativeFixture()

    def _adopted(self, name: str) -> None:
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO experiment_adoptions (experiment_id, status) VALUES (?, 'adopted')", (name,))

    def test_the_first_adoption_of_each_experiment_is_free(self):
        svc = self.fx.service(daily_budget=1)
        self.addCleanup(svc.close)
        for name in ("a", "b"):
            self.fx.add_experiment(name, "succeeded")
            self.assertTrue(svc.tick().acted)
            self._adopted(name)
        self.assertEqual(self.fx.adoption.started, ["a", "b"])
        self.assertEqual(svc._spent_today(), 0)

    def test_an_adoption_that_never_started_is_not_retried_for_free(self):
        svc = self.fx.service(daily_budget=1)
        self.addCleanup(svc.close)
        self.fx.add_experiment("a", "succeeded")  # the fake adoption leaves it not_started
        self.assertTrue(svc.tick().acted)  # first adoption: free
        self.assertTrue(svc.tick().acted)  # repeat: counts, uses the budget
        self.assertEqual(svc.tick().decision, "budget_exhausted")
        self.assertEqual(self.fx.adoption.started, ["a", "a"])

    def test_verified_work_is_adopted_when_the_budget_is_used_up(self):
        svc = self.fx.service(daily_budget=1)
        self.addCleanup(svc.close)
        self.fx.add_experiment("stuck", "succeeded", adoption="needs_review")
        self.assertTrue(svc.tick().acted)  # a retried adoption: counts
        self.assertEqual(svc.tick().decision, "budget_exhausted")
        self._adopted("stuck")
        self.fx.add_experiment("fresh", "succeeded")
        decision = svc.tick()
        self.assertTrue(decision.acted, decision.detail)
        self.assertEqual(decision.candidate.ref, "fresh")


if __name__ == "__main__":
    unittest.main()
