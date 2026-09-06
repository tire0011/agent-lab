"""Tests for modules/mailbox/command_recovery.py.

Covers the outcomes a mailbox command's interrupted-worker recovery must
distinguish (Issue #123, tightened per PR #124 review):

- git convergence alone is *never* enough to claim success unless the
  command explicitly declared ``recovery_services: none`` (i.e. that git
  convergence is its complete expected state);
- when a command declares ``recovery_services``, success additionally
  requires all of them to be active, and a declared-but-inactive service
  is a verified failure — this is the "pull converged but the restart
  never completed" case the review flagged;
- a repo that moved but never converged is always a verified failure;
- anything with no usable evidence is reported as unknown, never guessed.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.mailbox.command_recovery import (
    capture_pre_start_state,
    evaluate_interrupted_command,
    parse_expected_services,
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


class TestParseExpectedServices(unittest.TestCase):
    def test_absent_field_is_no_declaration(self):
        self.assertIsNone(parse_expected_services({}))
        self.assertIsNone(parse_expected_services(None))

    def test_none_literal_declares_git_alone_is_complete(self):
        self.assertEqual(parse_expected_services({"recovery_services": "none"}), [])
        self.assertEqual(parse_expected_services({"recovery_services": "None"}), [])

    def test_comma_separated_list_is_parsed_and_trimmed(self):
        self.assertEqual(
            parse_expected_services({"recovery_services": "agent-api, nexus-mailbox.timer"}),
            ["agent-api", "nexus-mailbox.timer"],
        )


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
    def _prepared_job(
        self, tmp: Path, expected_services: list[str] | None = None
    ) -> tuple[Path, Path]:
        repo = _make_repo_with_remote(tmp)
        state_dir = tmp / "job"
        state_dir.mkdir()
        write_recovery_state(state_dir, repo, expected_services)
        return repo, state_dir

    def test_git_convergence_alone_is_unknown_without_a_declaration(self):
        """The review finding for PR #124: a bare git-converged state must
        not be promoted to success unless the command explicitly declared
        that git convergence is its complete expected state.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir = self._prepared_job(Path(tmp))  # no declaration
            _git(["commit", "--allow-empty", "-m", "recovered"], repo)
            _git(["push", "origin", "HEAD:master"], repo)

            outcome = evaluate_interrupted_command(state_dir, repo)

        self.assertEqual(outcome.status, "unknown")
        self.assertIn("recovery_services", outcome.evidence)

    def test_verified_success_when_command_declares_git_alone_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir = self._prepared_job(Path(tmp), expected_services=[])
            _git(["commit", "--allow-empty", "-m", "recovered"], repo)
            _git(["push", "origin", "HEAD:master"], repo)

            outcome = evaluate_interrupted_command(state_dir, repo)

        self.assertEqual(outcome.status, "succeeded")
        self.assertIn("clean", outcome.evidence)

    def test_verified_success_when_declared_services_are_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir = self._prepared_job(Path(tmp), expected_services=["agent-api"])
            _git(["commit", "--allow-empty", "-m", "recovered"], repo)
            _git(["push", "origin", "HEAD:master"], repo)

            outcome = evaluate_interrupted_command(
                state_dir, repo, service_checker=lambda names: True
            )

        self.assertEqual(outcome.status, "succeeded")
        self.assertIn("agent-api", outcome.evidence)

    def test_verified_failure_when_git_converges_but_declared_service_is_inactive(self):
        """The concrete review scenario: refresh.sh's pull succeeds (git
        converges) but the subsequent service restart fails or never runs.
        This must be reported as failed, not a false success.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir = self._prepared_job(Path(tmp), expected_services=["agent-api"])
            _git(["commit", "--allow-empty", "-m", "recovered"], repo)
            _git(["push", "origin", "HEAD:master"], repo)

            outcome = evaluate_interrupted_command(
                state_dir, repo, service_checker=lambda names: False
            )

        self.assertEqual(outcome.status, "failed")
        self.assertIn("agent-api", outcome.evidence)

    def test_unknown_when_declared_services_state_cannot_be_determined(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir = self._prepared_job(Path(tmp), expected_services=["agent-api"])
            _git(["commit", "--allow-empty", "-m", "recovered"], repo)
            _git(["push", "origin", "HEAD:master"], repo)

            outcome = evaluate_interrupted_command(
                state_dir, repo, service_checker=lambda names: None
            )

        self.assertEqual(outcome.status, "unknown")

    def test_verified_failure_when_repo_moved_but_did_not_reach_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir = self._prepared_job(Path(tmp), expected_services=[])
            _git(["commit", "--allow-empty", "-m", "unpushed"], repo)
            # Deliberately not pushed: local HEAD now diverges from upstream.

            outcome = evaluate_interrupted_command(state_dir, repo)

        self.assertEqual(outcome.status, "failed")

    def test_unknown_when_head_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, state_dir = self._prepared_job(Path(tmp), expected_services=[])

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
