from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import modules.experiment_service as experiment_service_module
import modules.mailbox as mailbox_module
import modules.mailbox_poll_runner as mailbox_poll_runner_module
import modules.operation_chain_publisher as operation_chain_publisher_module
import modules.operation_chain_service as operation_chain_service_module
import modules.operations_registry as operations_registry_module
import modules.outbox_publisher as outbox_publisher_module
from modules.mailbox_config import MailboxConfig
from modules.mailbox_poll_runner import _sync_mailbox_repo, run_mailbox_poll_once


class _CloseTracking:
    close_calls: list[str] = []


class _FakeMailboxLock:
    release_calls = 0

    def __init__(self, _path: Path):
        pass

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        type(self).release_calls += 1


class _FakeExperimentService:
    raise_on_close = False

    def __init__(self, **_kwargs):
        pass

    def close(self) -> None:
        _CloseTracking.close_calls.append("experiment_service")
        if type(self).raise_on_close:
            raise RuntimeError("experiment service close failure")


class _FakeOperationsRegistry:
    def __init__(self, **_kwargs):
        pass

    def close(self) -> None:
        _CloseTracking.close_calls.append("operations_registry")


class _FakeOperationChainService:
    def __init__(self, **_kwargs):
        pass

    def close(self) -> None:
        _CloseTracking.close_calls.append("operation_chain_service")


class _FakeOperationChainPublisher:
    def __init__(self, **_kwargs):
        pass

    def publish_pending(self) -> None:
        pass

    def close(self) -> None:
        _CloseTracking.close_calls.append("operation_chain_publisher")


class _FakeTaskApprovalOutboxPublisher:
    def __init__(self, **_kwargs):
        pass

    def publish_pending(self) -> None:
        pass

    def close(self) -> None:
        _CloseTracking.close_calls.append("task_approval_outbox_publisher")


class _FakeMailboxProcessor:
    def __init__(self, **_kwargs):
        pass

    def retry_pending_git_commits(self) -> None:
        pass

    def ensure_clean_worktree(self) -> bool:
        return True

    def process_inbox(self) -> list[dict[str, str]]:
        return [{"status": "done"}]

    def close(self) -> None:
        _CloseTracking.close_calls.append("processor")
        raise RuntimeError("processor close failure")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _configure_git(repo: Path) -> None:
    _git(repo, "config", "user.name", "Mailbox Test")
    _git(repo, "config", "user.email", "mailbox-test@example.invalid")


class MailboxSyncTests(unittest.TestCase):
    def test_sync_rebases_local_outbox_commit_onto_remote_inbox_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            seed = root / "seed"
            local = root / "local"
            peer = root / "peer"

            subprocess.run(
                ["git", "init", "--bare", "--initial-branch=main", str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "clone", str(remote), str(seed)],
                check=True,
                capture_output=True,
                text=True,
            )
            _configure_git(seed)
            (seed / "inbox").mkdir()
            (seed / "outbox").mkdir()
            (seed / "inbox" / ".gitkeep").write_text("", encoding="utf-8")
            (seed / "outbox" / ".gitkeep").write_text("", encoding="utf-8")
            _git(seed, "add", "inbox/.gitkeep", "outbox/.gitkeep")
            _git(seed, "commit", "-m", "seed mailbox")
            _git(seed, "push", "-u", "origin", "main")

            for clone in (local, peer):
                subprocess.run(
                    ["git", "clone", str(remote), str(clone)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                _configure_git(clone)

            (local / "outbox" / "local.md").write_text("local outbox\n", encoding="utf-8")
            _git(local, "add", "outbox/local.md")
            _git(local, "commit", "-m", "local outbox")

            (peer / "inbox" / "remote.md").write_text("remote inbox\n", encoding="utf-8")
            _git(peer, "add", "inbox/remote.md")
            _git(peer, "commit", "-m", "remote inbox")
            _git(peer, "push")

            self.assertTrue(_sync_mailbox_repo(mailbox_module, local))
            self.assertTrue((local / "inbox" / "remote.md").exists())
            self.assertTrue((local / "outbox" / "local.md").exists())
            self.assertEqual(
                _git(local, "rev-list", "--left-right", "--count", "HEAD...@{u}").stdout.strip(),
                "0\t0",
            )


class MailboxPollRunnerCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        _CloseTracking.close_calls = []
        _FakeMailboxLock.release_calls = 0
        _FakeExperimentService.raise_on_close = False

    def test_close_failures_do_not_block_other_cleanup_or_lock_release(self) -> None:
        _FakeExperimentService.raise_on_close = True
        cfg = MailboxConfig.from_paths(Path("/code"), Path("/mailbox"))
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "mailbox.lock"
            with patch.object(mailbox_module, "MailboxLock", _FakeMailboxLock), patch.object(
                experiment_service_module,
                "ExperimentService",
                _FakeExperimentService,
            ), patch.object(
                operations_registry_module,
                "OperationsRegistry",
                _FakeOperationsRegistry,
            ), patch.object(
                operation_chain_service_module,
                "OperationChainService",
                _FakeOperationChainService,
            ), patch.object(
                mailbox_module,
                "MailboxProcessor",
                _FakeMailboxProcessor,
            ), patch.object(
                operation_chain_publisher_module,
                "OperationChainPublisher",
                _FakeOperationChainPublisher,
            ), patch.object(
                outbox_publisher_module,
                "TaskApprovalOutboxPublisher",
                _FakeTaskApprovalOutboxPublisher,
            ), patch.object(
                mailbox_poll_runner_module,
                "_sync_mailbox_repo",
                return_value=True,
            ):
                result = run_mailbox_poll_once(
                    db_path=str(Path(tmp) / "agent.db"),
                    mailbox_config=cfg,
                    lock_path=lock_path,
                    dry_run=False,
                )

        self.assertEqual(result.status, "error")
        self.assertIn("cleanup_failed=processor", result.error or "")
        self.assertIn("experiment_service", result.error or "")
        self.assertEqual(
            _CloseTracking.close_calls,
            [
                "processor",
                "operation_chain_service",
                "operation_chain_publisher",
                "task_approval_outbox_publisher",
                "operations_registry",
                "experiment_service",
            ],
        )
        self.assertEqual(_FakeMailboxLock.release_calls, 1)


if __name__ == "__main__":
    unittest.main()
