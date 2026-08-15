from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import modules.experiment_service as experiment_service_module
import modules.mailbox as mailbox_module
import modules.operation_chain_publisher as operation_chain_publisher_module
import modules.operation_chain_service as operation_chain_service_module
import modules.operations_registry as operations_registry_module
import modules.outbox_publisher as outbox_publisher_module
from modules.mailbox_config import MailboxConfig
from modules.mailbox_poll_runner import run_mailbox_poll_once


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

    def retry_unpushed_commits(self) -> None:
        pass

    def retry_pending_git_commits(self) -> None:
        pass

    def ensure_clean_worktree(self) -> bool:
        return True

    def pull(self) -> bool:
        return True

    def process_inbox(self) -> list[dict[str, str]]:
        return [{"status": "done"}]

    def close(self) -> None:
        _CloseTracking.close_calls.append("processor")
        raise RuntimeError("processor close failure")


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
