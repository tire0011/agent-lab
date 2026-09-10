"""Regression tests for Issue #144 moved-target-base stale adoption recovery."""

from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules.experiment_adoption_service import STATUS_ADOPTED, STATUS_FAILED
from modules.github_work_tracking import GitHubCIVerification
from modules.task_finalization import TaskFinalizationCoordinator
from modules.verification_runner import run_verification_test as real_run_verification_test
from tests.test_experiment_adoption_service import AdoptionRepoFixture, git


_TIMEOUT_REASON = "Die Integrationstests konnten nicht erfolgreich abgeschlossen werden."


class MovedBaseIntegrationFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)
        self.experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        self.checkpoint_sha = "28399a0ad8dfca091d948b01b788fd6280b8e12b"
        self.adoption_id = "c617b97314ed4da98c539be99130c6c0"
        self.fx.create_experiment(
            experiment_id=self.experiment_id,
            changed_files=["app.py"],
        )
        ref = self.fx.github_work_tracking.for_experiment(self.experiment_id)
        self.assertIsNotNone(ref)
        ref.checkpoint_sha = self.checkpoint_sha
        self.fx.github_work_tracking._persist_experiment(ref)
        self.old_base = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()

    def _green(self) -> GitHubCIVerification:
        return GitHubCIVerification(
            status="passed",
            commit_sha=self.checkpoint_sha,
            conclusion="success",
            run_id=33898643539,
            url="https://example.invalid/actions/runs/33898643539",
        )

    def _historical_timeout(self, *, test_exit_code=None) -> None:
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, experiment_commit_sha,
                    target_branch, test_exit_code, test_output, reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'main', ?, ?, ?, 't', 't')
                """,
                (
                    self.adoption_id,
                    self.experiment_id,
                    STATUS_FAILED,
                    "d757a26a8778135bacf7c1008f9db0eda1e127a1",
                    test_exit_code,
                    "[test command timed out]" if test_exit_code is None else "FAILED",
                    _TIMEOUT_REASON,
                ),
            )

    def _advance_main(self) -> str:
        (self.fx.repo / "unrelated.py").write_text("MOVED = True\n", encoding="utf-8")
        git(self.fx.repo, "add", "unrelated.py")
        git(self.fx.repo, "commit", "-m", "advance main after checkpoint")
        git(self.fx.repo, "push", "origin", "main")
        git(self.fx.repo, "fetch", "origin", "main")
        return git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()

    def _coordinator(self) -> TaskFinalizationCoordinator:
        coordinator = TaskFinalizationCoordinator(
            experiment_service=self.fx.experiment_service,
            github_work_tracking=self.fx.github_work_tracking,
            db_path=self.fx.db_path,
            repo_root=str(self.fx.repo),
            adoption_service=self.fx.adoption_service,
            restart_runner=lambda services: None,
            live_validator=lambda approval, experiment: {"summary": "ok"},
        )
        self.addCleanup(coordinator.close)
        return coordinator

    def _prepare_moved_base(self) -> str:
        self._historical_timeout()
        stored = self.fx.github_work_tracking.record_ci_verification(
            self._green(),
            experiment_id=self.experiment_id,
            base_sha=self.old_base,
        )
        self.assertTrue(stored)
        new_base = self._advance_main()
        self.assertNotEqual(self.old_base, new_base)
        return new_base

    def test_moved_base_is_integration_fallback_not_no_ci_evidence(self):
        new_base = self._prepare_moved_base()
        coordinator = self._coordinator()

        # The moved-base branch must be recognized before the old generic
        # assess_failure path can turn it into terminal no_ci_evidence.
        with patch.object(
            self.fx.adoption_service,
            "assess_failure",
            side_effect=AssertionError("generic stale assessment must not run"),
        ):
            assessment = coordinator._failure_assessment(
                SimpleNamespace(id=self.experiment_id)
            )

        self.assertIsNotNone(assessment)
        self.assertTrue(assessment.retryable)
        self.assertEqual(assessment.category, "integration_fallback_required")
        self.assertEqual(assessment.evidence_commit_sha, self.checkpoint_sha)
        self.assertEqual(assessment.evidence_run_id, 33898643539)
        self.assertEqual(
            self.fx.adoption_service._integration_base_sha(),
            new_base,
        )

    def test_moved_base_passes_current_main_integration_fallback_and_reuses_row(self):
        self._prepare_moved_base()
        coordinator = self._coordinator()
        assessment = coordinator._failure_assessment(SimpleNamespace(id=self.experiment_id))
        self.assertTrue(assessment.retryable)

        unavailable = GitHubCIVerification(
            status="unavailable",
            commit_sha=self.checkpoint_sha,
            reason=(
                "target branch moved since checkpoint verification; "
                "integration fallback is required for the current base"
            ),
        )
        with (
            patch.object(
                self.fx.github_work_tracking,
                "wait_for_ci",
                return_value=unavailable,
            ),
            patch(
                "modules.experiment_adoption_service.run_verification_test",
                side_effect=real_run_verification_test,
            ) as local_tests,
        ):
            self.fx.adoption_service.start_adoption(
                self.experiment_id,
                superseded=assessment,
            )
            adoption = self.fx.wait_terminal(self.experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.id, self.adoption_id)
        self.assertEqual(adoption.stale_retry_attempts, 1)
        local_tests.assert_called_once()
        history = self.fx.adoption_service.failure_history(self.experiment_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["adoption_id"], self.adoption_id)
        self.assertIn("integration fallback", history[0]["superseded_by"])

    def test_real_test_verdict_is_not_reopened_by_moved_base(self):
        self._historical_timeout(test_exit_code=1)
        self.fx.github_work_tracking.record_ci_verification(
            self._green(),
            experiment_id=self.experiment_id,
            base_sha=self.old_base,
        )
        self._advance_main()
        coordinator = self._coordinator()

        sentinel = SimpleNamespace(retryable=False, category="test_verdict")
        with patch.object(
            self.fx.adoption_service,
            "assess_failure",
            return_value=sentinel,
        ) as generic:
            assessment = coordinator._failure_assessment(
                SimpleNamespace(id=self.experiment_id)
            )

        self.assertIs(assessment, sentinel)
        generic.assert_called_once_with(self.experiment_id)


if __name__ == "__main__":
    unittest.main()
