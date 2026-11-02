"""Planner history shows each experiment's merge outcome (#439)."""

from __future__ import annotations

import sqlite3
import unittest

from modules.nexus_planner import planning_prompt
from tests.test_nexus_initiative import InitiativeFixture


class RecentExperimentAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = InitiativeFixture()

    def _recent(self):
        svc = self.fx.service()
        self.addCleanup(svc.close)
        return svc._recent_experiments()

    def _set_reason(self, exp_id: str, reason: str) -> None:
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("UPDATE experiment_adoptions SET reason=? WHERE experiment_id=?", (reason, exp_id))

    def test_review_blocked_merge_is_not_read_as_done(self):
        self.fx.add_experiment("e1", "succeeded", adoption="failed")
        self._set_reason("e1", "Review by codex blocked the merge: X")
        recent = self._recent()
        self.assertEqual(recent[0]["adoption_status"], "failed")
        prompt = planning_prompt([], recent)
        self.assertIn("- succeeded / merge: blocked by review: Goal for e1 (Review by codex blocked the merge: X)", prompt)

    def test_other_failed_adoption_keeps_failed_label(self):
        self.fx.add_experiment("e1", "succeeded", adoption="failed")
        self._set_reason("e1", "tests failed")
        prompt = planning_prompt([], self._recent())
        self.assertIn("- succeeded / merge: failed: Goal for e1 (tests failed)", prompt)

    def test_adopted_is_rendered_as_merged(self):
        self.fx.add_experiment("e1", "succeeded", adoption="adopted")
        self.fx.add_experiment("e2", "succeeded", adoption="succeeded", ended="2000-01-01T00:00:00+00:00")
        prompt = planning_prompt([], self._recent())
        self.assertIn("- succeeded / merge: merged: Goal for e1", prompt)
        self.assertIn("- succeeded / merge: merged: Goal for e2", prompt)

    def test_no_adoption_row_is_rendered_as_none(self):
        self.fx.add_experiment("e1", "succeeded")
        prompt = planning_prompt([], self._recent())
        self.assertIn("- succeeded / merge: none: Goal for e1", prompt)

    def test_needs_review_experiment_shows_its_adoption(self):
        self.fx.add_experiment("e1", "needs_review", adoption="needs_review")
        prompt = planning_prompt([], self._recent())
        self.assertIn("- needs_review / merge: needs_review: Goal for e1", prompt)

    def test_only_the_newest_adoption_row_is_used(self):
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.executescript(
                "DROP TABLE experiment_adoptions;"
                "CREATE TABLE experiment_adoptions (experiment_id TEXT, status TEXT, reason TEXT);"
            )
            conn.execute("INSERT INTO experiment_adoptions VALUES ('e1', 'failed', 'Review by codex blocked the merge: old')")
            conn.execute("INSERT INTO experiment_adoptions VALUES ('e1', 'adopted', NULL)")
        self.fx.add_experiment("e1", "succeeded")
        recent = self._recent()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["adoption_status"], "adopted")
        self.assertIn("merge: merged", planning_prompt([], recent))

    def test_missing_adoption_table_keeps_the_old_output(self):
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("DROP TABLE experiment_adoptions")
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO experiments (id, goal, status, ended_at, error) VALUES ('e1', 'Goal', 'failed', '2026-10-01', 'boom')"
            )
        recent = self._recent()
        self.assertEqual(recent, [{"title": "Goal", "status": "failed", "error": "boom"}])
        prompt = planning_prompt([], recent)
        self.assertIn("- failed: Goal (boom)", prompt)
        self.assertNotIn("merge:", prompt)

    def test_long_reason_is_cut_to_200_characters(self):
        self.fx.add_experiment("e1", "succeeded", adoption="failed")
        self._set_reason("e1", "Review by codex blocked the merge:\n  " + "y" * 500)
        reason = self._recent()[0]["adoption_reason"]
        self.assertEqual(len(reason), 200)
        self.assertTrue(reason.startswith("Review by codex blocked the merge: yyy"))


if __name__ == "__main__":
    unittest.main()
