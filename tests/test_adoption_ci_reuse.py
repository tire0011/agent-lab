from __future__ import annotations

import unittest
from unittest.mock import patch

from modules.experiment_adoption_service import STATUS_ADOPTED, STATUS_FAILED
from modules.github_work_tracking import GitHubCIVerification
from modules.verification_runner import run_verification_test as real_run_verification_test
from tests.test_experiment_adoption_service import AdoptionRepoFixture, git


class AdoptionAuthoritativeCIReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)

    def _experiment_with_checkpoint(self, experiment_id: str, checkpoint_sha: str) -> None:
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        ref = self.fx.github_work_tracking.for_experiment(experiment_id)
        self.assertIsNotNone(ref)
        ref.checkpoint_sha = checkpoint_sha
        self.fx.github_work_tracking._persist_experiment(ref)

    def test_green_exact_commit_ci_skips_local_full_suite(self):
        experiment_id = "134a" * 8
        checkpoint_sha = "verified-checkpoint-sha"
        self._experiment_with_checkpoint(experiment_id, checkpoint_sha)
        ci = GitHubCIVerification(
            status="passed",
            commit_sha=checkpoint_sha,
            conclusion="success",
            run_id=33898643539,
            url="https://example.invalid/actions/33898643539",
        )

        with (
            patch.object(self.fx.github_work_tracking, "wait_for_ci", return_value=ci) as wait_ci,
            patch("modules.experiment_adoption_service.run_verification_test") as local_tests,
        ):
            self.fx.adoption_service.start_adoption(experiment_id)
            adoption = self.fx.wait_terminal(experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.test_exit_code, 0)
        self.assertIn("authoritative GitHub Actions", adoption.test_output)
        self.assertIn(checkpoint_sha, adoption.test_output)
        local_tests.assert_not_called()
        wait_ci.assert_called()

    def test_failed_exact_commit_ci_is_authoritative_and_does_not_fallback(self):
        experiment_id = "134b" * 8
        checkpoint_sha = "failed-checkpoint-sha"
        self._experiment_with_checkpoint(experiment_id, checkpoint_sha)
        before = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()
        ci = GitHubCIVerification(
            status="failed",
            commit_sha=checkpoint_sha,
            conclusion="failure",
            run_id=999,
        )

        with (
            patch.object(self.fx.github_work_tracking, "wait_for_ci", return_value=ci),
            patch("modules.experiment_adoption_service.run_verification_test") as local_tests,
        ):
            self.fx.adoption_service.start_adoption(experiment_id)
            adoption = self.fx.wait_terminal(experiment_id)

        after = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()
        self.assertEqual(adoption.status, STATUS_FAILED)
        self.assertIn("GitHub-CI", adoption.reason)
        self.assertEqual(before, after)
        local_tests.assert_not_called()

    def test_unavailable_exact_commit_ci_uses_local_fallback(self):
        experiment_id = "134c" * 8
        checkpoint_sha = "unavailable-checkpoint-sha"
        self._experiment_with_checkpoint(experiment_id, checkpoint_sha)
        ci = GitHubCIVerification(
            status="unavailable",
            commit_sha=checkpoint_sha,
            reason="GitHub Actions temporarily unavailable",
        )

        with (
            patch.object(self.fx.github_work_tracking, "wait_for_ci", return_value=ci),
            patch(
                "modules.experiment_adoption_service.run_verification_test",
                side_effect=real_run_verification_test,
            ) as local_tests,
        ):
            self.fx.adoption_service.start_adoption(experiment_id)
            adoption = self.fx.wait_terminal(experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.test_exit_code, 0)
        local_tests.assert_called_once()


if __name__ == "__main__":
    unittest.main()
