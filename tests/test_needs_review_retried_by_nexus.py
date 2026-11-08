"""A merge stopped at needs_review is retried by Nexus, not left to a human."""

from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace

from modules.experiment_adoption_service import ExperimentAdoptionService
from tests.test_nexus_initiative import InitiativeFixture

ISSUE = [{"number": 398, "author": {"login": "tire0011"}, "title": "Goal policy corpus", "body": "Change: add tests."}]


class NeedsReviewRetriedTests(unittest.TestCase):
    def setUp(self):
        self.fx = InitiativeFixture()
        self.fx.add_experiment("e1", "succeeded", adoption="needs_review")
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)",
                ("e1", 398),
            )
            conn.execute(
                "UPDATE experiment_adoptions SET reason = ? WHERE experiment_id = ?",
                ("Potentiell sensible Dateien werden nicht automatisch übernommen.", "e1"),
            )

    def _tick(self, retryable):
        self.fx.adoption.assess_failure = lambda experiment_id: SimpleNamespace(retryable=retryable)
        svc = self.fx.service(issue_lister=lambda: ISSUE)
        self.addCleanup(svc.close)
        return svc.tick()

    def test_terminal_needs_review_frees_the_issue_with_the_reason(self):
        self.assertEqual(self._tick(False).decision, "dispatched")
        self.assertEqual(self.fx.controller.issues, [398])
        svc = self.fx.service()
        self.addCleanup(svc.close)
        self.assertIn("merge stopped: Potentiell sensible", svc._earlier_attempts(398))

    def test_resumable_needs_review_resumes_the_merge_instead(self):
        decision = self._tick(True)
        self.assertEqual(self.fx.controller.issues, [])
        self.assertEqual(self.fx.adoption.started, ["e1"])
        self.assertTrue(decision.acted)


class SensitivePathRuleTests(unittest.TestCase):
    def _reason(self, path):
        return ExperimentAdoptionService._denied_path_reason(object.__new__(ExperimentAdoptionService), path)

    def test_test_fixture_named_after_secrets_is_allowed(self):
        self.assertIsNone(self._reason("tests/fixtures/goal_policy/reject/fetch_and_print_vault_secrets.md"))

    def test_other_sensitive_paths_still_stop(self):
        self.assertIsNotNone(self._reason("modules/secret_store.py"))
        self.assertIsNotNone(self._reason("tests/.env"))
        self.assertIsNotNone(self._reason("deploy/key.pem"))


if __name__ == "__main__":
    unittest.main()
