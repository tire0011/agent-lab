from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import HTTPException

os.environ.setdefault("AGENT_MAILBOX_REPO_PATH", "/tmp/nexus-cancel-test-mailbox")
import api
from modules.codex_worker import CodexResult
from modules.experiment_service import (
    STATUS_CANCELLED,
    STATUS_SUCCEEDED,
    ExperimentNotCancellableError,
    ExperimentService,
)


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "nexus@example.test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Nexus Test"], check=True)
    (path / "tests").mkdir()
    (path / "tests" / "test_ok.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True)


class BlockingWorker:
    provider = "codex"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.process: subprocess.Popen | None = None

    def run(self, *, on_process=None, **kwargs):
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            start_new_session=True,
        )
        if on_process:
            on_process(self.process)
        self.started.set()
        code = self.process.wait()
        if on_process:
            on_process(None)
        return CodexResult(
            success=False, exit_code=code, stdout="", stderr="", runtime_ms=1,
            timed_out=False, stdout_truncated=False, stderr_truncated=False,
            thread_id=None, final_message=None, usage=None, task=kwargs["task"],
            working_dir=kwargs["working_dir"], sandbox=kwargs["sandbox"],
            error="worker terminated", provider="codex",
        )


class ExperimentCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        self.worker = BlockingWorker()
        self.service = ExperimentService(
            worker_factory=lambda _path: self.worker,
            db_path=str(root / "runs.db"),
            repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"),
            experiment_log_dir=str(root / "logs"),
        )

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def test_cancel_kills_blocked_worker_and_keeps_worktree(self):
        experiment = self.service.create_experiment("Make a small change")
        thread = threading.Thread(target=self.service.run_experiment, args=(experiment.id,))
        thread.start()
        self.assertTrue(self.worker.started.wait(5))

        cancelled = self.service.cancel_experiment(experiment.id, "nexus-operator")
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(cancelled.status, STATUS_CANCELLED)
        self.assertEqual(cancelled.cancelled_by, "nexus-operator")
        self.assertIsNotNone(cancelled.cancelled_at)
        self.assertIsNotNone(self.worker.process)
        self.assertIsNotNone(self.worker.process.poll())
        self.assertTrue(Path(cancelled.worktree_path).is_dir())
        self.assertEqual(self.service.get_experiment(experiment.id).status, STATUS_CANCELLED)

    def test_finished_experiment_cannot_be_cancelled(self):
        experiment = self.service.create_experiment("done")
        self.service._force_status(experiment.id, STATUS_SUCCEEDED, ended_at="now")
        with self.assertRaises(ExperimentNotCancellableError):
            self.service.cancel_experiment(experiment.id, "api")


class CancellationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MagicMock()

    def test_endpoint_requires_api_key(self):
        with self.assertRaises(HTTPException) as caught:
            api.cancel_experiment("deadbeef", x_api_key=None, service=self.service)
        self.assertEqual(caught.exception.status_code, 401)
        self.service.cancel_experiment.assert_not_called()

    def test_finished_experiment_returns_409(self):
        self.service.cancel_experiment.side_effect = ExperimentNotCancellableError("finished")
        with self.assertRaises(HTTPException) as caught:
            api.cancel_experiment(
                "deadbeef", x_api_key="test-key", x_cancelled_by=None, service=self.service
            )
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
