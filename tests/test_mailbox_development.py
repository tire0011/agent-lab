"""Tests for the development mailbox adapter."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules import mailbox
from modules.mailbox import development as mailbox_development
from modules.mailbox.development import development_mode_enabled, install, process_command
from modules.run_store import init_db


def _make_repo_with_remote(tmp: Path) -> Path:
    """Create a git repo at tmp/repo with an ``origin`` remote tracking master."""
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True, capture_output=True)
    (repo / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "HEAD:master"], cwd=str(repo), check=True, capture_output=True
    )
    return repo


def _reap_pid_file(pid_path: Path, timeout: float = 5.0) -> None:
    """Wait for and reap the process recorded in *pid_path*.

    In production each mailbox poll is a fresh OS process, so a detached
    command runner that dies is immediately reaped by init and a later
    ``os.kill(pid, 0)`` correctly reports it as gone. In this test the
    *test process itself* is the runner's parent, so without an explicit
    ``waitpid`` it would remain a zombie — still visible to ``kill(pid, 0)``
    — for as long as the test process runs. This reproduces the production
    condition instead of the test-harness artifact.
    """
    deadline = time.monotonic() + timeout
    pid = None
    while time.monotonic() < deadline:
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                break
            except ValueError:
                pass
        time.sleep(0.02)
    assert pid is not None, "command runner never recorded a pid"

    while time.monotonic() < deadline:
        try:
            reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped_pid == pid:
            return
        time.sleep(0.05)


class TestMailboxDevelopment(unittest.TestCase):
    def _run_command(self, msg, repo: Path, db_msg_id: int = 1):
        result = process_command(msg, repo, None, db_msg_id)
        deadline = time.monotonic() + 5
        while result is mailbox._DEFERRED_PROCESSING:
            if time.monotonic() >= deadline:
                self.fail("command did not finish")
            time.sleep(0.01)
            result = process_command(msg, repo, None, db_msg_id)
        return result

    def test_install_registers_command_type_and_processor(self):
        install(mailbox)
        self.assertIn("command", mailbox.SUPPORTED_TYPES)
        self.assertIs(mailbox._PROCESSORS["command"], process_command)

    def test_development_mode_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(development_mode_enabled())
        with patch.dict(os.environ, {"NEXUS_DEVELOPMENT_MODE": "1"}, clear=True):
            self.assertTrue(development_mode_enabled())

    def test_command_executes_without_development_mode(self):
        msg = SimpleNamespace(body="printf hello", meta={})
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        with tempfile.TemporaryDirectory() as tmp:
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                body, idea_id = self._run_command(msg, Path(tmp))
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("hello", body)
        self.assertIsNone(idea_id)

    def test_internal_command_without_front_matter_uses_default_timeout(self):
        msg = SimpleNamespace(body="printf hello")
        with tempfile.TemporaryDirectory() as tmp:
            body, _ = process_command(msg, Path(tmp), None, 0)
        self.assertIn("**Exit code:** 0", body)
        self.assertIn(
            f"**Deadline:** {mailbox_development.DEFAULT_COMMAND_TIMEOUT_SECONDS}s",
            body,
        )

    def test_command_executes_and_can_change_workspace(self):
        msg = SimpleNamespace(
            body="printf hello && printf changed > created.txt",
            meta={"timeout": "5"},
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        with tempfile.TemporaryDirectory() as tmp:
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                repo = Path(tmp)
                body, _ = self._run_command(msg, repo)
                self.assertEqual((repo / "created.txt").read_text(), "changed")
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("hello", body)

    def test_command_timeout_metadata_changes_persisted_deadline(self):
        msg = SimpleNamespace(body="sleep 30", meta={"timeout": "1"})
        env = {"PATH": os.environ.get("PATH", "")}
        with tempfile.TemporaryDirectory() as tmp:
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                first = process_command(msg, Path(tmp), None, 1)
                self.assertIs(first, mailbox._DEFERRED_PROCESSING)
                self.assertEqual((Path(tmp) / "jobs" / "1" / "timeout").read_text(), "1")
                body, _ = self._run_command(msg, Path(tmp))
        self.assertIn("**Exit code:** 124", body)
        self.assertIn("**Deadline:** 1s", body)
        self.assertIn("**Timed out:** yes", body)
        self.assertIn("[timeout] deadline 1s exceeded", body)

    def test_command_rejects_invalid_or_unbounded_timeout(self):
        for value in ("0", "not-a-number", "21601"):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                process_command(
                    SimpleNamespace(body="true", meta={"timeout": value}),
                    Path("."),
                    None,
                    0,
                )

    def test_nonzero_command_always_returns_a_terminal_report(self):
        msg = SimpleNamespace(body="printf failed >&2; exit 7", meta={"timeout": "5"})
        env = {"PATH": os.environ.get("PATH", "")}
        with tempfile.TemporaryDirectory() as tmp:
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                body, _ = self._run_command(msg, Path(tmp))
        self.assertIn("**Exit code:** 7", body)
        self.assertIn("failed", body)

    def test_running_command_publishes_progress_in_mailbox_state(self):
        msg = SimpleNamespace(body="sleep 30", meta={"timeout": "5"})
        env = {"PATH": os.environ.get("PATH", "")}
        with tempfile.TemporaryDirectory() as tmp:
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            conn = init_db(str(Path(tmp) / "state.db"))
            mailbox.init_mailbox_db(conn)
            cur = conn.execute(
                """
                INSERT INTO mailbox_messages
                    (file_path, source_commit_sha, msg_type, created_by, status, received_at)
                VALUES ('inbox/running.md', 'sha', 'command', 'nexus', 'processing', 'now')
                """
            )
            db_msg_id = cur.lastrowid
            conn.commit()
            with patch.dict(os.environ, env, clear=True):
                self.assertIs(
                    process_command(msg, Path(tmp), conn, db_msg_id),
                    mailbox._DEFERRED_PROCESSING,
                )
                self.assertIs(
                    process_command(msg, Path(tmp), conn, db_msg_id),
                    mailbox._DEFERRED_PROCESSING,
                )
                progress = conn.execute(
                    "SELECT last_error FROM mailbox_messages WHERE id=?", (db_msg_id,)
                ).fetchone()[0]
                pid = int((Path(tmp) / "jobs" / str(db_msg_id) / "pid").read_text())
                os.killpg(pid, 15)
                mailbox_development._ACTIVE_COMMANDS.pop(db_msg_id).wait(timeout=2)
            conn.close()
        self.assertIn("command running:", progress)
        self.assertIn("deadline 5s", progress)

    def test_unknown_front_matter_key_is_reported(self):
        msg = SimpleNamespace(body="true", meta={"mystery_guard": "enabled"})
        with tempfile.TemporaryDirectory() as tmp:
            body, _ = process_command(msg, Path(tmp), None, 0)
        self.assertIn("Unknown front-matter keys: mystery_guard", body)

    def test_command_allows_read_only_service_status(self):
        msg = SimpleNamespace(body="printf 'agent-api status check'", meta={})
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        with tempfile.TemporaryDirectory() as tmp:
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                body, _ = self._run_command(msg, Path(tmp))
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("agent-api status check", body)

    def test_command_recovers_verified_success_when_worker_is_killed_before_exit_code(self):
        """Regression test for Issue #123: a self-restarting command (like
        ``refresh.sh``) whose runner is killed before it records an exit code
        must be recovered as a verified success, not a misleading Exit code
        255 — and a repeated poll must not re-derive or duplicate the reply.

        The command declares ``recovery_services: none``, i.e. that reaching
        a clean, converged git state *is* its complete expected state (per
        the PR #124 review: git convergence alone is not sufficient proof
        unless a command explicitly says so).
        """
        msg = SimpleNamespace(
            body="git commit --allow-empty -m recovered && git push origin HEAD:master && kill -9 $$",
            meta={"recovery_services": "none"},
        )
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo_with_remote(Path(tmp))
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                first = process_command(msg, repo, None, 1)
                self.assertIs(first, mailbox._DEFERRED_PROCESSING)
                _reap_pid_file(Path(tmp) / "jobs" / "1" / "pid")

                deadline = time.monotonic() + 5
                result = process_command(msg, repo, None, 1)
                while result is mailbox._DEFERRED_PROCESSING:
                    if time.monotonic() >= deadline:
                        self.fail("worker was not observed as terminated")
                    time.sleep(0.05)
                    result = process_command(msg, repo, None, 1)
                body, idea_id = result

                # A second poll (simulating a further restart) must return the
                # same cached exit code/stdout/stderr rather than recomputing
                # or duplicating the recovery evidence (only the live-computed
                # "Duration" line is expected to differ between polls).
                second_body, _ = process_command(msg, repo, None, 1)

        self.assertIn("**Exit code:** 0", body)
        self.assertIn("restart recovery: succeeded", body)
        self.assertIsNone(idea_id)

        def _without_duration(text: str) -> str:
            return "\n".join(line for line in text.splitlines() if not line.startswith("**Duration:**"))

        self.assertEqual(_without_duration(body), _without_duration(second_body))

    def test_command_recovery_is_unknown_not_success_without_declared_recovery_services(self):
        """Review finding on PR #124: a command that never declares
        recovery_services must not have git convergence alone promoted to
        success — that only proves a git update step completed, not that a
        multi-step command (e.g. a restart after the pull) finished.
        """
        msg = SimpleNamespace(
            body="git commit --allow-empty -m recovered && git push origin HEAD:master && kill -9 $$",
            meta={},
        )
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo_with_remote(Path(tmp))
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                first = process_command(msg, repo, None, 1)
                self.assertIs(first, mailbox._DEFERRED_PROCESSING)
                _reap_pid_file(Path(tmp) / "jobs" / "1" / "pid")

                deadline = time.monotonic() + 5
                result = process_command(msg, repo, None, 1)
                while result is mailbox._DEFERRED_PROCESSING:
                    if time.monotonic() >= deadline:
                        self.fail("worker was not observed as terminated")
                    time.sleep(0.05)
                    result = process_command(msg, repo, None, 1)
                body, _ = result

        self.assertIn("**Exit code:** 255", body)
        self.assertIn("restart recovery: unknown", body)
        self.assertNotIn("restart recovery: succeeded", body)

    def test_command_recovers_verified_failure_when_worker_is_killed_without_converging(self):
        """A killed worker that changed the repo but never reached a clean
        state matching its upstream must be reported as a real failure with
        evidence, not silently treated as success.
        """
        msg = SimpleNamespace(
            body="git commit --allow-empty -m unpushed && kill -9 $$",
            meta={},
        )
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo_with_remote(Path(tmp))
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                first = process_command(msg, repo, None, 1)
                self.assertIs(first, mailbox._DEFERRED_PROCESSING)
                _reap_pid_file(Path(tmp) / "jobs" / "1" / "pid")

                deadline = time.monotonic() + 5
                result = process_command(msg, repo, None, 1)
                while result is mailbox._DEFERRED_PROCESSING:
                    if time.monotonic() >= deadline:
                        self.fail("worker was not observed as terminated")
                    time.sleep(0.05)
                    result = process_command(msg, repo, None, 1)
                body, _ = result

        self.assertIn("**Exit code:** 1", body)
        self.assertIn("restart recovery: failed", body)

    def test_task_goes_straight_to_nexus(self):
        install(mailbox)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state.db")
            conn = init_db(db_path)
            mailbox.init_mailbox_db(conn)
            now = mailbox._now_iso()
            cur = conn.execute(
                """
                INSERT INTO mailbox_messages
                    (file_path, source_commit_sha, msg_type, created_by, status, received_at)
                VALUES (?, 'sha', 'task', 'rene', 'processing', ?)
                """,
                (str(Path(tmp) / "task.md"), now),
            )
            db_msg_id = cur.lastrowid
            conn.commit()
            msg = SimpleNamespace(
                body="Inspect and improve the project.",
                meta={},
                created_by="rene",
                file_path=Path(tmp) / "task.md",
            )
            with patch.dict(os.environ, {}, clear=True):
                result, _ = mailbox._PROCESSORS["task"](
                    msg, Path(tmp), conn, db_msg_id
                )
            row = conn.execute(
                "SELECT status, approved_at FROM mailbox_task_approvals WHERE mailbox_msg_id=?",
                (db_msg_id,),
            ).fetchone()
            conn.close()
        # #264: no approval state, the worker hands a queued task to Nexus.
        self.assertEqual(row[0], "queued")
        self.assertIsNone(row[1])
        self.assertIn("handed to Nexus", result)
        self.assertNotIn("approv", result)


if __name__ == "__main__":
    unittest.main()


class CommandSenderTrustTests(unittest.TestCase):
    """The poll passes ``sender_trusted`` to every processor (#506)."""

    def test_trusted_sender_keyword_is_accepted(self):
        msg = SimpleNamespace(body="printf hello", meta={})
        with tempfile.TemporaryDirectory() as tmp:
            body, _ = process_command(msg, Path(tmp), None, 0, sender_trusted=True)
        self.assertIn("hello", body)

    def test_untrusted_sender_never_runs_a_shell(self):
        msg = SimpleNamespace(body="printf ran > ran.txt", meta={})
        with tempfile.TemporaryDirectory() as tmp:
            body, _ = process_command(msg, Path(tmp), None, 0, sender_trusted=False)
            self.assertFalse((Path(tmp) / "ran.txt").exists())
        self.assertIn("not a trusted", body)
