from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.experiment_service import (
    ExperimentAlreadyActiveError,
    ExperimentService,
    STATUS_FAILED,
)
from modules.nexus_controller_service import NexusControllerService
from modules.task_orchestrator import ACTION_NEW_EXPERIMENT, TaskDecision


class ExperimentIssueDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.service = ExperimentService(
            db_path=str(root / "experiments.db"),
            repo_root=str(root),
            worktrees_root=str(root / "worktrees"),
            experiment_log_dir=str(root / "logs"),
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def _count(self) -> int:
        with self.service._lock:
            return self.service._conn.execute(
                "SELECT COUNT(*) FROM experiments"
            ).fetchone()[0]

    def test_active_experiment_blocks_second_row(self) -> None:
        first = self.service.create_experiment("GitHub Issue #7: first attempt")

        with self.assertRaises(ExperimentAlreadyActiveError) as raised:
            self.service.create_experiment("GitHub Issue #007: duplicate attempt")

        self.assertEqual(raised.exception.experiment_id, first.id)
        self.assertEqual(raised.exception.status, "queued")
        self.assertEqual(self._count(), 1)

    def test_terminal_experiment_allows_retry(self) -> None:
        first = self.service.create_experiment("GitHub Issue #7: first attempt")
        self.service._force_status(first.id, STATUS_FAILED, error="test failure")

        second = self.service.create_experiment("GitHub Issue #7: retry")

        self.assertNotEqual(second.id, first.id)
        self.assertEqual(self._count(), 2)

    def test_concurrent_service_instances_produce_one_row(self) -> None:
        second_service = ExperimentService(
            db_path=self.service.db_path,
            repo_root=self.service.repo_root,
            worktrees_root=self.service.worktrees_root,
            experiment_log_dir=self.service.experiment_log_dir,
        )
        barrier = threading.Barrier(2)
        created: list[str] = []
        errors: list[ExperimentAlreadyActiveError] = []
        unexpected: list[BaseException] = []

        def create(service: ExperimentService) -> None:
            barrier.wait()
            try:
                created.append(
                    service.create_experiment("GitHub Issue #7: concurrent").id
                )
            except ExperimentAlreadyActiveError as exc:
                errors.append(exc)
            except BaseException as exc:
                unexpected.append(exc)

        threads = [
            threading.Thread(target=create, args=(self.service,)),
            threading.Thread(target=create, args=(second_service,)),
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        finally:
            second_service.close()

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(unexpected, [])
        self.assertEqual(len(created), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].experiment_id, created[0])
        self.assertEqual(self._count(), 1)

    def test_other_issues_and_non_issue_goals_are_not_blocked(self) -> None:
        goals = (
            "GitHub Issue #7: one",
            "GitHub Issue #8: another issue",
            "ordinary goal",
            "ordinary goal",
        )

        experiments = [self.service.create_experiment(goal) for goal in goals]

        self.assertEqual(len({experiment.id for experiment in experiments}), 4)
        self.assertEqual(self._count(), 4)


class ControllerAlreadyActiveTests(unittest.TestCase):
    def test_controller_returns_already_active_without_binding_or_starting(self) -> None:
        orchestrator = MagicMock()
        orchestrator.experiment_service.db_path = ":memory:"
        orchestrator.operations_registry = SimpleNamespace(repo_root=Path("/tmp"))
        orchestrator.experiment_service.create_experiment.side_effect = (
            ExperimentAlreadyActiveError("existing-id", "implementing")
        )
        github = MagicMock()
        github.ensure_for_source.return_value = SimpleNamespace(
            issue_number=7, branch_name="nexus/7-test"
        )
        controller = NexusControllerService(
            orchestrator, github_work_tracking=github
        )
        decision = TaskDecision(
            action=ACTION_NEW_EXPERIMENT, session_id="session-1"
        )

        orchestrator.run.return_value = decision

        with patch.object(controller, "_start_experiment_thread") as start:
            result = controller.handle_task("GitHub Issue #7: duplicate")

        self.assertEqual(
            result,
            {
                "status": "already_active",
                "experiment_id": "existing-id",
                "experiment_status": "implementing",
            },
        )
        github.bind_experiment.assert_not_called()
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
