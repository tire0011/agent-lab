"""#710: persistent timing-check failures are compared with the base commit."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.timing_check import control_check
from modules.verification_env import build_verification_test_env


FAIL = "def test_value():\n    assert False\n"
PASS = "def test_value():\n    assert True\n"
TEST_ID = "tests/test_value.py::test_value"


class TimingCheckBaseCompareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "tests").mkdir(parents=True)
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True,
            capture_output=True,
        ).stdout

    def _base_and_branch(self, base: str, branch: str) -> str:
        path = self.repo / "tests" / "test_value.py"
        path.write_text(base, encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")
        sha = self._git("rev-parse", "HEAD").strip()
        path.write_text(branch, encoding="utf-8")
        return sha

    def _control(self, base: str):
        return control_check(
            self.repo, [TEST_ID], base_commit=base, python=sys.executable,
            env=build_verification_test_env(),
        )

    def _assert_no_worktree(self) -> None:
        listed = self._git("worktree", "list", "--porcelain")
        self.assertEqual(listed.count("worktree "), 1, listed)
        self.assertFalse(list(Path(self.tmp.name).glob("nexus-timing-base-*")))

    def test_failure_on_base_is_preexisting_and_deterministic(self) -> None:
        base = self._base_and_branch(FAIL, FAIL + "# branch\n")
        first = self._control(base)
        second = self._control(base)
        self.assertEqual(first, second)
        self.assertEqual(first.preexisting_host_failures, [TEST_ID])
        self.assertEqual(first.host_regressions, [])
        self._assert_no_worktree()

    def test_pass_on_base_is_host_regression_with_branch_tail(self) -> None:
        base = self._base_and_branch(PASS, FAIL)
        result = self._control(base)
        self.assertEqual(result.host_regressions, [TEST_ID])
        self.assertIn("failed", result.no_drift_output_tail.lower())
        self._assert_no_worktree()

    def test_base_crash_has_no_verdict_and_removes_worktree(self) -> None:
        crash = "import os\ndef test_value():\n    os._exit(3)\n"
        base = self._base_and_branch(crash, FAIL)
        self.assertIsNone(self._control(base))
        self._assert_no_worktree()

    def test_keyboard_interrupt_removes_worktree(self) -> None:
        base = self._base_and_branch(PASS, FAIL)
        real_run = __import__("modules.timing_check", fromlist=["run_timing_check"]).run_timing_check
        calls = 0

        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return real_run(*args, **kwargs)

        with patch("modules.timing_check.run_timing_check", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self._control(base)
        self._assert_no_worktree()


if __name__ == "__main__":
    unittest.main()
