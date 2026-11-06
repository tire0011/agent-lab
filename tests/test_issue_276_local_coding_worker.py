"""Issue #276: complexity routing and the one-shot local patch worker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules.experiment_service import ExperimentService, STATUS_FAILED
from modules.local_coding_worker import LocalCodingWorker


class _Client:
    def __init__(self, output: str) -> None:
        self.output = output

    async def complete(self, **_kwargs):
        return self.output


class LocalCodingWorkerTests(unittest.TestCase):
    def test_unappliable_patch_returns_a_clear_failed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.py").write_text("value = 1\n", encoding="utf-8")
            # git apply --check only needs a work tree for this malformed context.
            import subprocess
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            diff = (
                "diff --git a/sample.py b/sample.py\n"
                "--- a/sample.py\n+++ b/sample.py\n"
                "@@ -1 +1 @@\n-value = 999\n+value = 2\n"
            )
            result = LocalCodingWorker(client=_Client(diff)).run(
                task="Change sample.py", working_dir=str(root), sandbox="workspace-write"
            )
        self.assertFalse(result.success)
        self.assertIn("patch does not apply", result.error or "")


class ComplexityRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.external = MagicMock()
        self.external.provider = "codex"
        self.service = ExperimentService(
            selection_worker=MagicMock(),
            worker_factory=lambda _path: self.external,
            db_path=str(root / "agent.db"),
            repo_root=str(root),
            worktrees_root=str(root / "worktrees"),
        )
        self.addCleanup(self.service.close)

    def test_complexity_is_normalized_and_persisted(self):
        small = self.service.create_experiment("small task", complexity="small")
        invalid = self.service.create_experiment("unknown task", complexity="tiny")
        missing = self.service.create_experiment("default task")
        self.assertEqual(small.complexity, "small")
        self.assertEqual(invalid.complexity, "large")
        self.assertEqual(missing.complexity, "large")

    @patch("modules.experiment_service.service.LocalCodingWorker")
    def test_small_routes_local_while_large_routes_external(self, local_cls):
        local = MagicMock()
        local.provider = "local"
        local_cls.return_value = local
        small = self.service.create_experiment("small", complexity="small")
        large = self.service.create_experiment("large", complexity="large")
        self.assertIs(self.service._implementation_worker(small, "/tmp/a"), local)
        self.assertIs(self.service._implementation_worker(large, "/tmp/b"), self.external)

    @patch("modules.experiment_service.service.LocalCodingWorker")
    def test_failed_local_attempt_forces_same_issue_to_external(self, local_cls):
        first = self.service.create_experiment("GitHub Issue #276: first", complexity="small")
        self.service._force_status(
            first.id, STATUS_FAILED, implementation_provider="local", error="bad patch"
        )
        retry = self.service.create_experiment("GitHub Issue #276: retry", complexity="small")
        worker = self.service._implementation_worker(retry, "/tmp/retry")
        self.assertIs(worker, self.external)
        local_cls.assert_not_called()

    @patch("modules.experiment_service.service.LocalCodingWorker")
    def test_failed_local_attempt_forces_same_mailbox_task_to_external(self, local_cls):
        # Mailbox goals carry a handover that changes between attempts and no
        # "GitHub Issue #N:" prefix; the task identity must still match.
        first = self.service.create_experiment(
            "Fix the parser\n\nWhat Nexus already found (handover):\n\nfirst notes",
            complexity="small",
            task_key="mailbox:task-1",
        )
        self.service._force_status(
            first.id, STATUS_FAILED, implementation_provider="local", error="bad patch"
        )
        retry = self.service.create_experiment(
            "Fix the parser\n\nWhat Nexus already found (handover):\n\nlocal patch failed",
            complexity="small",
            task_key="mailbox:task-1",
        )
        self.assertEqual(retry.task_key, "mailbox:task-1")
        self.assertIs(self.service._implementation_worker(retry, "/tmp/retry"), self.external)
        local_cls.assert_not_called()

    @patch("modules.experiment_service.service.LocalCodingWorker")
    def test_failed_local_attempt_does_not_affect_other_mailbox_task(self, local_cls):
        local = MagicMock()
        local_cls.return_value = local
        first = self.service.create_experiment(
            "Fix the parser", complexity="small", task_key="mailbox:task-1"
        )
        self.service._force_status(
            first.id, STATUS_FAILED, implementation_provider="local", error="bad patch"
        )
        other = self.service.create_experiment(
            "Fix the parser", complexity="small", task_key="mailbox:task-2"
        )
        self.assertIs(self.service._implementation_worker(other, "/tmp/other"), local)

    @patch("modules.experiment_service.service.LocalCodingWorker")
    def test_failed_local_attempt_matches_goal_without_handover(self, local_cls):
        first = self.service.create_experiment(
            "Fix the parser\n\nWhat Nexus already found (handover):\n\nA",
            complexity="small",
        )
        self.service._force_status(
            first.id, STATUS_FAILED, implementation_provider="local", error="bad patch"
        )
        retry = self.service.create_experiment(
            "Fix the parser\n\nWhat Nexus already found (handover):\n\nB",
            complexity="small",
        )
        self.assertIs(self.service._implementation_worker(retry, "/tmp/retry"), self.external)
        local_cls.assert_not_called()

    @patch("modules.experiment_service.service.LocalCodingWorker")
    def test_succeeded_or_external_failure_keeps_local_for_small(self, local_cls):
        local = MagicMock()
        local_cls.return_value = local
        first = self.service.create_experiment("x", complexity="large", task_key="mailbox:t")
        self.service._force_status(
            first.id, STATUS_FAILED, implementation_provider="codex", error="boom"
        )
        retry = self.service.create_experiment("x", complexity="small", task_key="mailbox:t")
        self.assertIs(self.service._implementation_worker(retry, "/tmp/r"), local)


if __name__ == "__main__":
    unittest.main()
