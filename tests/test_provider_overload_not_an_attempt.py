"""An experiment that failed on an overloaded provider is neither an attempt nor spent budget."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from modules.coding_provider import is_provider_overload
from tests.test_nexus_initiative import InitiativeFixture

OVERLOAD = "API Error: 529 Overloaded. This is a server-side issue, usually temporary"
ISSUE = [{"number": 232, "title": "Staged controller view", "body": ""}]


class ProviderOverloadTests(unittest.TestCase):
    def setUp(self):
        self.fx = InitiativeFixture()

    def _attempt(self, exp_id, error):
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO experiments (id, goal, status, ended_at, created_at, error) VALUES (?,?,?,?,?,?)",
                (exp_id, "g", "failed", now, now, error),
            )
            conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)", (exp_id, 232)
            )

    def test_detects_overload_errors(self):
        self.assertTrue(is_provider_overload(OVERLOAD))
        self.assertTrue(is_provider_overload("HTTP 429 rate limit exceeded"))
        self.assertFalse(is_provider_overload("tests failed"))

    def test_a_failing_test_named_after_an_overload_is_no_overload(self):
        """The error names the failing tests now (#472); those still cost an attempt."""
        from modules.nexus_initiative import _is_provider_overload

        self.assertFalse(
            _is_provider_overload(
                "failed", "tests failed (exit code 1): tests/test_a.py::test_rate_limit_resets"
            )
        )
        self.assertTrue(_is_provider_overload("failed", "API Error: 529 Overloaded"))

    def test_overloaded_runs_do_not_use_up_the_issue(self):
        for i in range(3):
            self._attempt(f"e{i}", OVERLOAD)
        svc = self.fx.service(issue_lister=lambda: ISSUE)
        self.addCleanup(svc.close)
        self.assertEqual(svc.tick().decision, "dispatched")
        self.assertEqual(self.fx.controller.issues, [232])

    def test_overloaded_runs_give_their_budget_back(self):
        svc = self.fx.service(daily_budget=1)
        self.addCleanup(svc.close)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO nexus_initiative_log (tick_id, created_at, decision) VALUES (?, ?, ?)",
                ("t1", datetime.now(timezone.utc).isoformat(), "dispatched"),
            )
        self._attempt("e1", OVERLOAD)
        self.assertEqual(svc._spent_today(), 0)


if __name__ == "__main__":
    unittest.main()
