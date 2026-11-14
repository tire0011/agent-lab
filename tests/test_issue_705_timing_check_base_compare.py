"""#705: compare branch-side timing control failures with the experiment base."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import timing_check
from modules.experiment_evaluation import EvaluationResult
from modules.experiment_service import ExperimentService, STATUS_VERIFYING

ROOT = Path(__file__).resolve().parents[1]


class TimingBaseCompareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        (self.repo / "modules").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self.service = ExperimentService(
            db_path=str(root / "db.sqlite"), repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"), test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"),
            # This suite exercises the post-verification timing check.  Keep
            # the preceding normal verification independent of whether the
            # host's default test runner discovers pytest-style functions in
            # this deliberately tiny temporary repository.
            test_command=[sys.executable, "-c", "print('normal tests passed')"],
        )
        quality = patch(
            "modules.experiment_service.evaluate_implementation",
            return_value=EvaluationResult(outcome="pass", reason="ok"),
        )
        quality.start()
        self.addCleanup(quality.stop)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout

    def _install_plugin(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import timing_check_replay
        finally:
            sys.path.pop(0)
        timing_check_replay.ensure_plugin(self.repo, ROOT)

    def _prepare(self, test_source: str):
        self._install_plugin()
        (self.repo / "modules" / "target.py").write_text("VALUE = 0\n", encoding="utf-8")
        (self.repo / "tests" / "test_target.py").write_text(test_source, encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")
        base = self._git("rev-parse", "HEAD").strip()
        experiment = self.service.create_experiment("change target")
        self.service._force_status(experiment.id, STATUS_VERIFYING, base_commit_sha=base)
        (self.repo / "modules" / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
        return experiment.id, base

    def _verify(self, experiment_id: str):
        with patch.dict("os.environ", {"NEXUS_TIMING_CHECK": "1"}):
            return self.service._verify(experiment_id, str(self.repo))

    def _events(self, experiment_id: str) -> list[dict]:
        path = Path(self.service.experiment_log_dir) / f"{experiment_id}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _worktrees(self) -> list[str]:
        return [
            line for line in self._git("worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")
        ]

    def test_failure_on_base_and_branch_succeeds_and_logs_event(self) -> None:
        experiment_id, _ = self._prepare(
            "import os\nfrom modules.target import VALUE\n"
            "def test_host():\n"
            "    assert 'NEXUS_TEST_CLOCK_DRIFT_MS' not in os.environ\n"
        )
        result = self._verify(experiment_id)
        self.assertEqual(result.status, "succeeded", result.error)
        event = next(
            row for row in self._events(experiment_id)
            if row["event"] == "timing_check_preexisting_host_failures"
        )
        test_id = "tests/test_target.py::test_host"
        self.assertIn(test_id, event["test_ids"])
        tails = json.loads(event["control_failure_tails"])
        self.assertIn("AssertionError", tails[test_id])
        self.assertEqual(len(self._worktrees()), 1)

    def test_branch_only_failure_is_failed_with_tail(self) -> None:
        experiment_id, _ = self._prepare(
            "from modules.target import VALUE\n"
            "def test_value():\n"
            "    assert VALUE == 0, 'branch-only sentinel'\n"
        )
        result = self._verify(experiment_id)
        self.assertEqual(result.status, "failed")
        self.assertIn("host_regression: tests/test_target.py::test_value", result.error)
        self.assertIn("branch-only sentinel", result.error)
        self.assertLessEqual(len(result.error.splitlines()), 41)
        self.assertGreater(len(result.error.splitlines()), 1)
        self.assertEqual(len(self._worktrees()), 1)

    def test_base_crash_is_needs_review_and_worktree_is_removed(self) -> None:
        experiment_id, _ = self._prepare(
            "import os\nfrom modules.target import VALUE\n"
            "def test_host():\n"
            "    assert 'NEXUS_TEST_CLOCK_DRIFT_MS' not in os.environ\n"
        )
        real = timing_check.run_timing_check
        calls = 0

        def crash_base(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("base crashed")
            return real(*args, **kwargs)

        with patch("modules.timing_check.run_timing_check", side_effect=crash_base):
            result = self._verify(experiment_id)
        self.assertEqual(result.status, "needs_review")
        self.assertIn("base crashed", result.error)
        self.assertEqual(len(self._worktrees()), 1)

    def test_cleanup_on_keyboard_interrupt_and_repeated_result(self) -> None:
        experiment_id, base = self._prepare(
            "import os\nfrom modules.target import VALUE\n"
            "def test_host():\n"
            "    assert 'NEXUS_TEST_CLOCK_DRIFT_MS' not in os.environ\n"
        )
        test_id = "tests/test_target.py::test_host"
        branch = timing_check.TimingCheckRun(
            f"timing_sensitive: {test_id}", "FAILED sentinel\n"
        )
        with patch(
            "modules.timing_check.run_timing_check",
            side_effect=[branch, KeyboardInterrupt()],
        ), self.assertRaises(KeyboardInterrupt):
            timing_check.control_check(self.repo, [test_id], base_commit=base)
        self.assertEqual(len(self._worktrees()), 1)

        first = self._verify(experiment_id)
        self.service._force_status(experiment_id, STATUS_VERIFYING)
        second = self._verify(experiment_id)
        self.assertEqual((first.status, first.error), (second.status, second.error))
        self.assertEqual(len(self._worktrees()), 1)


if __name__ == "__main__":
    unittest.main()
