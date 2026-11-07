from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.experiment_adoption_service import STATUS_ADOPTED, STATUS_FAILED
from modules.github_work_tracking import (
    GitHubCIVerification,
    GitHubWorkTrackingError,
    GitHubWorkTrackingService,
)
from modules.verification_runner import (
    run_verification_test_monitored as real_run_verification_test,
)
from tests.test_experiment_adoption_service import AdoptionRepoFixture, git



# modules.experiment_adoption_service was split into modules/experiment_adoption_parts/
# (#574): a patched name is looked up in the parts module that uses it, so one mock
# is installed in every parts module that looks the name up at call time.
_ADOPTION_LOOKUPS = {
    "run_verification_test_monitored": (
        "modules.experiment_adoption_parts.pipeline",
    ),

}


@contextmanager
def _patch_adoption(name: str, **kwargs):
    mock = MagicMock(**kwargs)
    with ExitStack() as stack:
        for module in _ADOPTION_LOOKUPS[name]:
            stack.enter_context(patch(f"{module}.{name}", new=mock))
        yield mock

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
            _patch_adoption("run_verification_test_monitored") as local_tests,
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
            _patch_adoption("run_verification_test_monitored") as local_tests,
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
            _patch_adoption(
                "run_verification_test_monitored",
                side_effect=real_run_verification_test,
            ) as local_tests,
        ):
            self.fx.adoption_service.start_adoption(experiment_id)
            adoption = self.fx.wait_terminal(experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.test_exit_code, 0)
        local_tests.assert_called_once()


class GitHubCIAuthorityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = GitHubWorkTrackingService(
            db_path=str(Path(self.tmp.name) / "state.db"),
            repo_root=self.tmp.name,
            repository="tire0011/agent-lab",
            transport=lambda method, path, payload: {},
        )
        self.addCleanup(self.service.close)
        self.ref = SimpleNamespace(
            checkpoint_sha="checkpoint-sha",
            branch_name="nexus/134-test",
        )

    def _request(
        self,
        *,
        conclusion: str = "success",
        target_sha: str = "base-sha",
        event: str = "push",
    ):
        def fake(method, path, payload=None):
            if path.endswith("/commits/checkpoint-sha"):
                return {"parents": [{"sha": "base-sha"}]}
            if "/git/ref/heads/main" in path:
                return {"object": {"sha": target_sha}}
            if "/actions/workflows/" in path:
                return {
                    "workflow_runs": [
                        {
                            "id": 42,
                            "head_sha": "checkpoint-sha",
                            "event": event,
                            "status": "completed",
                            "conclusion": conclusion,
                            "html_url": "https://example.invalid/run/42",
                        }
                    ]
                }
            raise AssertionError(path)

        return fake

    def test_success_is_authoritative_only_when_target_base_is_unchanged(self):
        with (
            patch.object(self.service, "ensure_for_experiment", return_value=self.ref),
            patch.object(self.service, "_request", side_effect=self._request()),
        ):
            result = self.service.wait_for_ci(
                "exp", start_timeout_seconds=0, completion_timeout_seconds=0, poll_interval_seconds=0.01
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.commit_sha, "checkpoint-sha")
        self.assertEqual(result.conclusion, "success")

    def test_repaired_checkpoint_with_non_main_parent_is_still_authoritative(self):
        # A repair checkpoint's direct parent is the previous checkpoint
        # commit, not `main` - that must never block recognizing CI for
        # the exact checkpoint SHA (#210).
        with (
            patch.object(self.service, "ensure_for_experiment", return_value=self.ref),
            patch.object(self.service, "_request", side_effect=self._request(target_sha="new-main-sha")),
        ):
            result = self.service.wait_for_ci(
                "exp", start_timeout_seconds=0, completion_timeout_seconds=0, poll_interval_seconds=0.01
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.commit_sha, "checkpoint-sha")
        self.assertEqual(result.conclusion, "success")

    def test_pull_request_event_ci_success_is_authoritative(self):
        with (
            patch.object(self.service, "ensure_for_experiment", return_value=self.ref),
            patch.object(self.service, "_request", side_effect=self._request(event="pull_request")),
        ):
            result = self.service.wait_for_ci(
                "exp", start_timeout_seconds=0, completion_timeout_seconds=0, poll_interval_seconds=0.01
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.commit_sha, "checkpoint-sha")

    def test_cancelled_ci_is_not_authoritative_failure(self):
        with (
            patch.object(self.service, "ensure_for_experiment", return_value=self.ref),
            patch.object(self.service, "_request", side_effect=self._request(conclusion="cancelled")),
        ):
            result = self.service.wait_for_ci(
                "exp", start_timeout_seconds=0, completion_timeout_seconds=0, poll_interval_seconds=0.01
            )

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.conclusion, "cancelled")
        self.assertIn("without authoritative result", result.reason)

    def test_explicit_failure_remains_authoritative(self):
        with (
            patch.object(self.service, "ensure_for_experiment", return_value=self.ref),
            patch.object(self.service, "_request", side_effect=self._request(conclusion="failure")),
        ):
            result = self.service.wait_for_ci(
                "exp", start_timeout_seconds=0, completion_timeout_seconds=0, poll_interval_seconds=0.01
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.conclusion, "failure")

    def test_identity_lookup_error_becomes_unavailable(self):
        with patch.object(
            self.service,
            "ensure_for_experiment",
            side_effect=GitHubWorkTrackingError("temporary API failure"),
        ):
            result = self.service.wait_for_ci(
                "exp", start_timeout_seconds=0, completion_timeout_seconds=0, poll_interval_seconds=0.01
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("identity unavailable", result.reason)


if __name__ == "__main__":
    unittest.main()
