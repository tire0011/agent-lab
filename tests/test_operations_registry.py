from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.operations_registry import (
    OperationValidationError,
    OperationsRegistry,
)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True)


class FakeProcessor:
    def __init__(self, repo_path: Path, db_path: str, dry_run: bool = False) -> None:
        self.repo_path = repo_path
        self.db_path = db_path
        self.dry_run = dry_run
        self.closed = False
        self.calls: list[str] = []

    def retry_unpushed_commits(self) -> None:
        self.calls.append("retry_unpushed_commits")

    def retry_pending_git_commits(self) -> None:
        self.calls.append("retry_pending_git_commits")

    def ensure_clean_worktree(self) -> bool:
        self.calls.append("ensure_clean_worktree")
        return True

    def pull(self) -> bool:
        self.calls.append("pull")
        return True

    def process_inbox(self):
        self.calls.append("process_inbox")
        return [{"status": "done", "file": "message.md", "outbox": "reply.md"}]

    def close(self) -> None:
        self.closed = True


class OperationsRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo.parent, "init", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "init")
        self.log_path = root / "exp.jsonl"
        self.meminfo = root / "meminfo"
        self.meminfo.write_text(
            "MemTotal:       1000 kB\nMemFree:         500 kB\nMemAvailable:    700 kB\nSwapTotal:       0 kB\nSwapFree:        0 kB\n",
            encoding="utf-8",
        )
        self.experiment = SimpleNamespace(
            id="abc12345def67890",
            status="succeeded",
            created_at="2026-08-14T12:00:00Z",
            ended_at="2026-08-14T12:10:00Z",
            test_command=["/agent/.venv/bin/python", "-m", "unittest"],
            test_exit_code=0,
            git_status=" M modules/demo.py\n?? nested/token.txt\n",
            error="token=secret /agent/private.py",
        )
        self.exp_service = MagicMock()
        self.exp_service.resolve_experiment.return_value = self.experiment
        self.exp_service.experiment_log_path.return_value = self.log_path
        self.registry = OperationsRegistry(
            experiment_service=self.exp_service,
            db_path=str(root / "agent.db"),
            repo_root=str(self.repo),
            mailbox_lock_path=str(root / "mailbox.lock"),
            mailbox_processor_factory=FakeProcessor,
            meminfo_path=str(self.meminfo),
        )
        self.addCleanup(self.registry.close)

    def test_catalog_lists_allowlisted_operations(self):
        catalog = self.registry.catalog()
        names = {item.name for item in catalog}
        self.assertIn("git_status", names)
        self.assertIn("service_logs", names)
        self.assertIn("mailbox_status", names)
        self.assertNotIn("experiment adopt", names)

    def test_mailbox_status_uses_fixed_units_and_redacts_bounded_output(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="\n".join(["token=secret /agent/private"] + [f"line {i}" for i in range(250)]),
                stderr="password=hunter2 /var/lib/private",
            )

        registry = OperationsRegistry(
            experiment_service=self.exp_service,
            db_path=str(Path(self.tmp.name) / "agent-mailbox-status.db"),
            repo_root=str(self.repo),
            subprocess_runner=runner,
            meminfo_path=str(self.meminfo),
        )
        self.addCleanup(registry.close)

        result = registry.execute("mailbox_status", {})

        self.assertTrue(result.success)
        self.assertEqual(
            [call[0] for call in calls],
            [
                ["systemctl", "status", "nexus-mailbox.service", "--no-pager", "-n", "0"],
                ["systemctl", "status", "nexus-mailbox.timer", "--no-pager", "-n", "0"],
            ],
        )
        self.assertTrue(all(call[1]["shell"] is False for call in calls))
        serialized = json.dumps(result.output_lines)
        self.assertLessEqual(len(result.output_lines), 200)
        self.assertIn("nexus-mailbox.service", serialized)
        self.assertIn("nexus-mailbox.timer", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("/agent", serialized)

    def test_mailbox_status_rejects_arguments_and_safely_reports_nonzero_exit(self):
        runner = MagicMock(return_value=subprocess.CompletedProcess(
            args=[], returncode=3, stdout="token=secret", stderr="password=hunter2 /var/lib/private"
        ))
        registry = OperationsRegistry(
            experiment_service=self.exp_service,
            db_path=str(Path(self.tmp.name) / "agent-mailbox-failure.db"),
            repo_root=str(self.repo),
            subprocess_runner=runner,
            meminfo_path=str(self.meminfo),
        )
        self.addCleanup(registry.close)

        with self.assertRaises(OperationValidationError):
            registry.execute("mailbox_status", {"unit": "ssh.service"})
        runner.assert_not_called()

        result = registry.execute("mailbox_status", {})
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 3)
        serialized = json.dumps(result.to_dict())
        self.assertNotIn("secret", serialized)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("/var/lib", serialized)
        self.assertIn("nexus-mailbox.service", serialized)
        self.assertIn("nexus-mailbox.timer", serialized)

    def test_invalid_operation_string_is_rejected_without_execution(self):
        with self.assertRaises(OperationValidationError):
            self.registry.execute("git status; rm -rf /")
        with self.assertRaises(OperationValidationError):
            self.registry.execute("git status | cat")
        with self.assertRaises(OperationValidationError):
            self.registry.execute("cat /etc/passwd")

    def test_git_operations_work_with_limit_validation(self):
        log_result = self.registry.execute("git_log", {"limit": 1})
        self.assertTrue(log_result.success)
        self.assertEqual(len(log_result.output_lines), 1)
        status_result = self.registry.execute("git_status", {})
        self.assertTrue(status_result.success)
        self.assertEqual(status_result.output_lines, [])
        with self.assertRaises(OperationValidationError):
            self.registry.execute("git_log", {"limit": 1000})

    def test_experiment_show_and_logs_are_sanitized(self):
        self.log_path.write_text(
            json.dumps({
                "timestamp": "2026-08-14T12:00:00Z",
                "level": "INFO",
                "phase": "adoption",
                "event": "detail",
                "message": "token=secret /agent/private.py",
            }) + "\n",
            encoding="utf-8",
        )
        show = self.registry.execute("experiment_show", {"experiment_id": self.experiment.id[:8]})
        self.assertTrue(show.success)
        self.assertNotIn("/agent", json.dumps(show.output_lines))
        self.assertNotIn("secret", json.dumps(show.output_lines))
        logs = self.registry.execute("experiment_logs", {"experiment_id": self.experiment.id[:8], "limit": 5})
        self.assertTrue(logs.success)
        serialized = json.dumps(logs.output_lines)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("/agent", serialized)

    def test_service_command_timeout_is_clean_error(self):
        def timeout_runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=kwargs.get("args") or args[0], timeout=1)

        registry = OperationsRegistry(
            experiment_service=self.exp_service,
            db_path=str(Path(self.tmp.name) / "agent-timeout.db"),
            repo_root=str(self.repo),
            subprocess_runner=timeout_runner,
            meminfo_path=str(self.meminfo),
        )
        self.addCleanup(registry.close)
        result = registry.execute("service_logs", {"limit": 5})
        self.assertFalse(result.success)
        self.assertIn("Zeitlimit", result.error_summary)

    def test_inbox_refresh_uses_single_poll_and_reports_already_running(self):
        result = self.registry.execute("inbox_refresh", {})
        self.assertTrue(result.success)
        self.assertIn("message.md", json.dumps(result.output_lines))
        lock_path = Path(self.tmp.name) / "mailbox.lock"
        from modules.mailbox import MailboxLock

        lock = MailboxLock(lock_path)
        self.assertTrue(lock.acquire())
        try:
            blocked = self.registry.execute("inbox_refresh", {})
        finally:
            lock.release()
        self.assertFalse(blocked.success)
        self.assertIn("läuft bereits", blocked.error_summary)

    def test_disk_and_memory_are_pure_python_operations(self):
        disk = self.registry.execute("disk", {})
        memory = self.registry.execute("memory", {})
        self.assertTrue(disk.success)
        self.assertTrue(memory.success)
        self.assertIn("MemTotal", "\n".join(memory.output_lines))

    def test_operations_are_audited_without_secret_logging(self):
        self.registry.execute("git_log", {"limit": 1})
        row = self.registry._conn.execute(
            "SELECT operation, args_json, success, error_summary FROM operations_audit ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], "git_log")
        self.assertEqual(json.loads(row[1]), {"limit": "1"})
        self.assertEqual(row[2], 1)
        self.assertIsNone(row[3])


if __name__ == "__main__":
    unittest.main()
