import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules.experiment_service import ExperimentService
from modules.verification_runner import VerificationRunResult


class BaselineRecheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "a@b.test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_sample.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            " def test_a(self): self.fail('already broken')\n"
            " def test_b(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.base = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        self.service = ExperimentService(
            selection_worker=MagicMock(), plan_worker_factory=MagicMock(),
            worker_factory=MagicMock(), db_path=str(Path(self.temp.name) / "db.sqlite"),
            repo_root=str(self.repo), worktrees_root=str(Path(self.temp.name) / "worktrees"),
            test_python_executable=sys.executable,
        )
        self.addCleanup(self.service.close)
        experiment = self.service.create_experiment(goal="test")
        self.experiment_id = experiment.id
        self.service._force_status(self.experiment_id, "verifying", base_commit_sha=self.base)
        self.elog = self.service.get_experiment_logger(self.experiment_id)

    def test_existing_failure_is_classified_and_worktree_removed(self):
        result = self.service._recheck_failures_on_base(
            self.experiment_id, ("test_sample.T.test_a",), {"app.py"}, self.elog
        )
        self.assertEqual(result, {"test_sample.T.test_a": "pre_existing"})
        worktrees = subprocess.check_output(
            ["git", "-C", str(self.repo), "worktree", "list", "--porcelain"], text=True
        )
        self.assertNotIn("nexus-baseline-", worktrees)

    def test_verify_ends_needs_review_for_only_pre_existing_failure(self):
        experiment_worktree = Path(self.temp.name) / "experiment"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-qb", "experiment-test",
             str(experiment_worktree), self.base],
            check=True,
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force",
                 str(experiment_worktree)], capture_output=True
            )
        )
        (experiment_worktree / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        result = self.service._verify(
            self.experiment_id, str(experiment_worktree), self.elog
        )
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(
            result.test_failure_classification,
            {"test_sample.T.test_a": "pre_existing"},
        )
        self.assertEqual(
            result.error,
            "pre-existing failures on base: test_sample.T.test_a",
        )

    def test_passing_on_base_is_regression(self):
        result = self.service._recheck_failures_on_base(
            self.experiment_id, ("test_sample.T.test_b",), {"app.py"}, self.elog
        )
        self.assertEqual(result, {"test_sample.T.test_b": "regression"})

    def test_stall_observed_for_exact_baseline_id_is_pre_existing(self):
        stalled = VerificationRunResult(
            None, "", "", True, None, 10, stalled=True,
            progress={"current_test": "tests.test_sample.T.test_a"},
            failed_test_ids=("tests.test_sample.T.test_a",),
        )
        with patch(
            "modules.experiment_service.service.run_verification_test_monitored",
            return_value=stalled,
        ):
            result = self.service._recheck_failures_on_base(
                self.experiment_id, ("test_sample.T.test_a",), {"app.py"}, self.elog
            )
        self.assertEqual(result, {"test_sample.T.test_a": "pre_existing"})

    def test_changed_test_file_skips_recheck(self):
        with patch("modules.experiment_service.service.run_verification_test_monitored") as runner:
            result = self.service._recheck_failures_on_base(
                self.experiment_id,
                ("test_sample.T.test_a",),
                {"tests/test_sample.py"},
                self.elog,
            )
        self.assertIsNone(result)
        runner.assert_not_called()

    def test_loader_failure_is_never_pre_existing(self):
        result = self.service._recheck_failures_on_base(
            self.experiment_id,
            ("unittest.loader._FailedTest.test_sample",),
            {"app.py"},
            self.elog,
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
