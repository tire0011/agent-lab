"""Tests for modules/mailbox/command_recovery.py.

Covers the three outcomes a mailbox command's interrupted-worker recovery
must distinguish (Issue #123): a verified success (repo converged to a
clean commit matching its upstream), a verified failure (repo moved but
did not converge), and an unknown outcome when there is no usable git
evidence at all.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.mailbox.command_recovery import (
    capture_pre_start_state,
    evaluate_interrupted_command,
    write_recovery_state,
)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_repo_with_remote(tmp: Path) -> Path:
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp / "repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.email", "test@test.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("# test\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "init"], repo)
    _git(["remote", "add", "origin", str(remote)], repo)
    _git(["push", "-u", "origin", "HEAD:master"], repo)
    return repo


class TestCaptureAndPersistState(unittest.TestCase):
    def test_non_git_directory_records_no_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = capture_pre_start_state(Path(tmp))
        self.assertIsNone(state["pre_commit"])

    def test_git_directory_records_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo_with_remote(Path(tmp))
            state = capture_pre_start_state(repo)
        self.assertIsNotNone(state["pre_commit"])

    def test_write_recovery_state_is_readable_by_evaluate(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo_with_remote(Path(tmp))
            state_dir = Path(tmp) / "job"
            state_dir.mkdir()
            write_recovery_state(state_dir, repo)
            self.assertTrue((state_dir / "recovery.json").exists())


class TestEvaluateInterruptedCommand(unittest.TestCase):
    def _prepared_job(self, tmp: Path) -> tuple[Path, Path]:
        repo = _make_repo_with_remote(tmp)
        state_dir = tmp / "job"
        state_dir.mkdir()
        write_recovery_state(state_dir, repo)
        return repo, state_dir

    def test_verified_success_when_repo_converges_to_clean_upstream_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir = self._prepared_job(Path(tmp))
            _git(["commit", "--allow-empty", "-m", "recovered"], repo)
            _git(["push", "origin", "HEAD:master"], repo)

            outcome = evaluate_interrupted_command(state_dir, repo)

        self.assertEqual(outcome.status, "succeeded")
        self.assertIn("clean", outcome.evidence)

    def test_verified_failure_when_repo_moved_but_did_not_reach_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir = self._prepared_job(Path(tmp))
            _git(["commit", "--allow-empty", "-m", "unpushed"], repo)
            # Deliberately not pushed: local HEAD now diverges from upstream.

            outcome = evaluate_interrupted_command(state_dir, repo)

        self.assertEqual(outcome.status, "failed")

    def test_unknown_when_head_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir = self._prepared_job(Path(tmp))

            outcome = evaluate_interrupted_command(state_dir, repo)

        self.assertEqual(outcome.status, "unknown")

    def test_unknown_when_not_a_git_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            not_a_repo = Path(tmp) / "plain"
            not_a_repo.mkdir()
            state_dir = Path(tmp) / "job"
            state_dir.mkdir()

            outcome = evaluate_interrupted_command(state_dir, not_a_repo)

        self.assertEqual(outcome.status, "unknown")

    def test_unknown_when_no_baseline_was_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo_with_remote(Path(tmp))
            state_dir = Path(tmp) / "job"
            state_dir.mkdir()
            # No write_recovery_state() call: no recovery.json baseline exists.
            _git(["commit", "--allow-empty", "-m", "changed"], repo)

            outcome = evaluate_interrupted_command(state_dir, repo)

        self.assertEqual(outcome.status, "unknown")


if __name__ == "__main__":
    unittest.main()
