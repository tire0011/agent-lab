from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.experiment_service import ExperimentService, STATUS_VERIFYING
from modules.verification_runner import VerificationRunResult


class BaselineRecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "app.py").write_text("VALUE = 'good'\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        self.service = ExperimentService(
            db_path=str(root / "db.sqlite"), repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"), test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"), max_repair_iterations=0,
            worker_factory=lambda _: None, plan_worker_factory=lambda _: None,
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def _commit(self, test_source: str) -> None:
        (self.repo / "tests" / "test_sample.py").write_text(test_source, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.repo, check=True)

    def _verify(self):
        experiment = self.service.create_experiment("Change app behavior")
        self.service._force_status(experiment.id, STATUS_VERIFYING)
        return self.service._verify(experiment.id, str(self.repo))

    def test_pre_existing_failure_becomes_needs_review_and_worktree_is_removed(self) -> None:
        self._commit(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_a(self): self.fail('already broken')\n"
        )
        (self.repo / "notes.txt").write_text("unrelated\n", encoding="utf-8")

        result = self._verify()

        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.baseline_test_classifications, {"test_sample.T.test_a": "pre_existing"})
        self.assertEqual(result.error, "pre-existing failures on base: test_sample.T.test_a")
        worktrees = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=self.repo,
            check=True, text=True, capture_output=True,
        ).stdout
        self.assertNotIn("nexus-baseline-", worktrees)

    def test_test_that_passes_on_base_remains_a_regression(self) -> None:
        self._commit(
            "import unittest\nfrom app import VALUE\nclass T(unittest.TestCase):\n"
            "    def test_b(self): self.assertEqual(VALUE, 'good')\n"
        )
        (self.repo / "app.py").write_text("VALUE = 'bad'\n", encoding="utf-8")

        result = self._verify()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.baseline_test_classifications, {"test_sample.T.test_b": "regression"})

    def test_stall_on_base_is_pre_existing_within_deadline(self) -> None:
        self._commit("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        (self.repo / "notes.txt").write_text("unrelated\n", encoding="utf-8")
        stalled = VerificationRunResult(
            None, "", "stalled", True, None, 1, stalled=True,
            progress={"current_test": "test_sample.T.test_a"},
            failed_test_ids=["test_sample.T.test_a"],
        )
        with patch(
            "modules.experiment_service.service.run_verification_test_monitored",
            side_effect=[stalled, stalled],
        ) as runner, patch.dict("os.environ", {"NEXUS_BASELINE_RECHECK_SECONDS": "0.01"}):
            result = self._verify()

        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.baseline_test_classifications["test_sample.T.test_a"], "pre_existing")
        self.assertEqual(runner.call_args_list[1].kwargs["config"].safety_cap_seconds, 0.01)

    def test_changed_failing_test_file_skips_recheck(self) -> None:
        self._commit("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        (self.repo / "tests" / "test_sample.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_a(self): self.fail('new')\n", encoding="utf-8",
        )
        failed = VerificationRunResult(
            1, "", "failed", False, None, 1,
            failed_test_ids=["test_sample.T.test_a"],
        )
        with patch(
            "modules.experiment_service.service.run_verification_test_monitored",
            return_value=failed,
        ) as runner:
            result = self._verify()

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.baseline_test_classifications)
        runner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
