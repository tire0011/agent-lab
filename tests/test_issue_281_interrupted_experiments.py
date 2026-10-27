"""#281/#371: interrupted experiments must not stay active forever.

The experiment thread dies with the ``agent-api`` process. At startup every
experiment still in ``selecting``/``implementing``/``verifying`` is therefore
orphaned. Legacy ``selecting`` rows are marked ``failed``; ``implementing`` and
``verifying`` rows become ``interrupted``. Every reconciled row records the
restart explanation so the Nexus session waiting on it can resume safely.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import api
from modules.experiment_service import (
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_SELECTING,
    STATUS_SUCCEEDED,
    STATUS_VERIFYING,
    ExperimentService,
)
from modules.mailbox.task_service import MailboxTaskService
from modules.nexus_task_store import NexusTaskStore


def _make_experiment_service(tmp: str) -> ExperimentService:
    return ExperimentService(
        selection_worker=MagicMock(),
        worker_factory=lambda _path: MagicMock(),
        plan_worker_factory=lambda _path: MagicMock(),
        db_path=str(Path(tmp) / "agent.db"),
        repo_root=tmp,
        worktrees_root=str(Path(tmp) / "worktrees"),
        experiment_log_dir=str(Path(tmp) / "logs"),
    )


def _with_status(service: ExperimentService, status: str) -> str:
    experiment = service.create_experiment("goal")
    with service._conn:
        service._conn.execute(
            "UPDATE experiments SET status = ? WHERE id = ?", (status, experiment.id)
        )
    return experiment.id


class MarkInterruptedExperimentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = _make_experiment_service(self.tmp.name)
        self.addCleanup(self.service.close)

    def test_active_experiments_are_reconciled_to_terminal_states(self):
        ids = {
            status: _with_status(self.service, status)
            for status in (STATUS_SELECTING, STATUS_IMPLEMENTING, STATUS_VERIFYING)
        }

        marked = self.service.mark_interrupted_experiments_failed()

        self.assertEqual(set(marked), set(ids.values()))
        expected = {
            STATUS_SELECTING: STATUS_FAILED,
            STATUS_IMPLEMENTING: STATUS_INTERRUPTED,
            STATUS_VERIFYING: STATUS_INTERRUPTED,
        }
        for original_status, experiment_id in ids.items():
            experiment = self.service.get_experiment(experiment_id)
            self.assertEqual(experiment.status, expected[original_status])
            self.assertEqual(experiment.error, "interrupted by service restart")
            self.assertIsNotNone(experiment.ended_at)

    def test_queued_and_terminal_experiments_are_untouched(self):
        untouched = {
            status: _with_status(self.service, status)
            for status in (
                STATUS_QUEUED,
                STATUS_SUCCEEDED,
                STATUS_NEEDS_REVIEW,
                STATUS_INTERRUPTED,
            )
        }

        self.assertEqual(self.service.mark_interrupted_experiments_failed(), [])

        for status, experiment_id in untouched.items():
            experiment = self.service.get_experiment(experiment_id)
            self.assertEqual(experiment.status, status)
            self.assertIsNone(experiment.error)

    def test_idempotent_and_recorded_in_activity(self):
        experiment_ids = {
            status: _with_status(self.service, status)
            for status in (STATUS_IMPLEMENTING, STATUS_VERIFYING)
        }

        self.assertEqual(
            set(self.service.mark_interrupted_experiments_failed()),
            set(experiment_ids.values()),
        )
        restarted = _make_experiment_service(self.tmp.name)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.mark_interrupted_experiments_failed(), [])

        for original_status, experiment_id in experiment_ids.items():
            changes = [
                entry
                for entry in restarted.list_activity(experiment_id=experiment_id)
                if entry.get("event") == "status_changed"
            ]
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["from_status"], original_status)
            self.assertEqual(changes[0]["to_status"], STATUS_INTERRUPTED)
            self.assertEqual(changes[0]["message"], "interrupted by service restart")

    def test_never_restarts_work(self):
        _with_status(self.service, STATUS_IMPLEMENTING)
        with patch.object(self.service, "run_experiment") as run, \
                patch.object(self.service, "_verify") as verify:
            self.service.mark_interrupted_experiments_failed()
        run.assert_not_called()
        verify.assert_not_called()

    def test_reconciliation_clears_worker_bookkeeping_and_preserves_worktree(self):
        experiment_id = _with_status(self.service, STATUS_IMPLEMENTING)
        worktree = Path(self.tmp.name) / "worktrees" / experiment_id
        worktree.mkdir(parents=True)
        marker = worktree / "unfinished.txt"
        marker.write_text("retain me", encoding="utf-8")
        with self.service._conn:
            self.service._conn.execute(
                "UPDATE experiments SET worktree_path = ? WHERE id = ?",
                (str(worktree), experiment_id),
            )
        worker_handle = MagicMock()
        self.service._active_processes[experiment_id] = worker_handle

        self.assertEqual(
            self.service.mark_interrupted_experiments_failed(), [experiment_id]
        )

        self.assertNotIn(experiment_id, self.service._active_processes)
        self.assertEqual(marker.read_text(encoding="utf-8"), "retain me")
        self.assertEqual(
            self.service.get_experiment(experiment_id).worktree_path, str(worktree)
        )
        worker_handle.assert_not_called()


class WaitingSessionResumesTests(unittest.TestCase):
    """After the mark, the existing #263 safety net resumes the waiting session."""

    def test_waiting_session_resumes_once_experiment_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            experiments = _make_experiment_service(tmp)
            store = NexusTaskStore(db_path=str(Path(tmp) / "agent.db"))
            try:
                experiment_id = _with_status(experiments, STATUS_IMPLEMENTING)
                session = store.get_or_create(source_type="task", source_id="281")
                self.assertTrue(store.wait_for_experiment(session.id, experiment_id))

                mailbox = MailboxTaskService.__new__(MailboxTaskService)
                mailbox.experiment_service = experiments
                mailbox._nexus_task_store = store
                mailbox._after_experiment = MagicMock()

                # Before the fix the session waits forever on an active experiment.
                mailbox.resume_finished_experiments()
                mailbox._after_experiment.assert_not_called()

                experiments.mark_interrupted_experiments_failed()
                mailbox.resume_finished_experiments()
                mailbox._after_experiment.assert_called_once_with(experiment_id)
            finally:
                store.close()
                experiments.close()


class StartupHookTests(unittest.TestCase):
    def test_lifespan_marks_experiments_before_mailbox_recovery(self):
        calls: list[str] = []
        experiments = MagicMock()
        experiments.mark_interrupted_experiments_failed.side_effect = (
            lambda: calls.append("experiments") or ["abc"]
        )
        mailbox = MagicMock()
        mailbox.github_work_tracking.readiness_error.return_value = None
        mailbox.recover_after_restart.side_effect = lambda: calls.append("mailbox")
        codex = MagicMock()
        codex.mark_interrupted_tasks_failed.return_value = []

        api.app.dependency_overrides[api.get_experiment_service] = lambda: experiments
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        api.app.dependency_overrides[api.get_codex_service] = lambda: codex
        api.app.dependency_overrides[api.get_experiment_adoption_service] = MagicMock
        api.app.dependency_overrides[api.get_operation_chain_service] = MagicMock
        self.addCleanup(api.app.dependency_overrides.clear)

        with patch.object(api, "_resume_blocked_capabilities_after_startup"), \
                patch.object(api, "MailboxTaskWorker"):
            with TestClient(api.app):
                pass

        self.assertEqual(calls, ["experiments", "mailbox"])

    def test_lifespan_survives_a_failing_mark(self):
        experiments = MagicMock()
        experiments.mark_interrupted_experiments_failed.side_effect = RuntimeError("db locked")
        mailbox = MagicMock()
        mailbox.github_work_tracking.readiness_error.return_value = None
        codex = MagicMock()
        codex.mark_interrupted_tasks_failed.return_value = []

        api.app.dependency_overrides[api.get_experiment_service] = lambda: experiments
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        api.app.dependency_overrides[api.get_codex_service] = lambda: codex
        api.app.dependency_overrides[api.get_experiment_adoption_service] = MagicMock
        api.app.dependency_overrides[api.get_operation_chain_service] = MagicMock
        self.addCleanup(api.app.dependency_overrides.clear)

        with patch.object(api, "_resume_blocked_capabilities_after_startup"), \
                patch.object(api, "MailboxTaskWorker"):
            with TestClient(api.app):
                pass

        mailbox.recover_after_restart.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
