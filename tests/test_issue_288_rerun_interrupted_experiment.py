"""#288: an experiment interrupted by a restart is rerun once, not the end of the task.

At startup ``mark_interrupted_experiments_failed`` (#281) fails the orphaned
experiment; the mailbox recovery then gives it exactly one automatic rerun.
The rerun does not count against the limit of two experiments per task, and
every other failure still goes to Nexus.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules.experiment_service import (
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_QUEUED,
    ExperimentService,
)
from modules.mailbox.task_service import (
    _STATUS_FAILED,
    _STATUS_RUNNING,
    _STATUS_SUBMITTED,
    _STATUS_SUCCEEDED,
)
from modules.nexus_task_store import NexusTaskStore
from modules.task_orchestrator import ACTION_NEW_EXPERIMENT, TaskDecision
from tests.test_mailbox_task_service import _insert_mailbox_msg, _make_service

_RESUME = "modules.mailbox.task_service.resume_with_new_orchestrator"


def _set_status(service: ExperimentService, experiment_id: str, status: str, **fields) -> None:
    assignments = ", ".join(["status = ?"] + [f"{key} = ?" for key in fields])
    with service._conn:
        service._conn.execute(
            f"UPDATE experiments SET {assignments} WHERE id = ?",
            (status, *fields.values(), experiment_id),
        )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.db_path = str(self.tmp / "test.db")
        self.experiments = ExperimentService(
            selection_worker=MagicMock(),
            worker_factory=lambda _path: MagicMock(),
            plan_worker_factory=lambda _path: MagicMock(),
            db_path=self.db_path,
            repo_root=str(self.tmp),
            worktrees_root=str(self.tmp / "worktrees"),
            experiment_log_dir=str(self.tmp / "logs"),
        )
        self.addCleanup(self.experiments.close)


class RerunInterruptedExperimentTests(_Base):
    def _interrupted(self) -> str:
        experiment = self.experiments.create_experiment("fix issue #288")
        _set_status(self.experiments, experiment.id, STATUS_IMPLEMENTING)
        self.assertEqual(
            self.experiments.mark_interrupted_experiments_failed(), [experiment.id]
        )
        return experiment.id

    def _rerun_of(self, experiment_id: str) -> str | None:
        row = self.experiments._conn.execute(
            "SELECT rerun_of FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        return row["rerun_of"]

    def test_interrupted_experiment_gets_a_queued_rerun_with_the_same_goal(self):
        original = self._interrupted()

        rerun = self.experiments.rerun_interrupted_experiment(original)

        self.assertIsNotNone(rerun)
        self.assertNotEqual(rerun.id, original)
        self.assertEqual(rerun.status, STATUS_QUEUED)
        self.assertEqual(rerun.goal, "fix issue #288")
        self.assertEqual(self._rerun_of(rerun.id), original)
        # The interrupted experiment itself stays failed.
        self.assertEqual(self.experiments.get_experiment(original).status, STATUS_FAILED)

    def test_at_most_one_rerun_per_interrupted_experiment(self):
        original = self._interrupted()
        self.assertIsNotNone(self.experiments.rerun_interrupted_experiment(original))
        self.assertIsNone(self.experiments.rerun_interrupted_experiment(original))

    def test_an_interrupted_rerun_is_not_rerun_again(self):
        rerun = self.experiments.rerun_interrupted_experiment(self._interrupted())
        _set_status(self.experiments, rerun.id, STATUS_IMPLEMENTING)
        self.experiments.mark_interrupted_experiments_failed()

        self.assertIsNone(self.experiments.rerun_interrupted_experiment(rerun.id))

    def test_other_failures_are_not_rerun(self):
        experiment = self.experiments.create_experiment("goal")
        _set_status(self.experiments, experiment.id, STATUS_FAILED, error="tests red")
        self.assertIsNone(self.experiments.rerun_interrupted_experiment(experiment.id))

        queued = self.experiments.create_experiment("goal")
        self.assertIsNone(self.experiments.rerun_interrupted_experiment(queued.id))


class MailboxRecoveryTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.svc = _make_service(self.tmp, exp_service=self.experiments)
        self.svc.operations_registry.db_path = self.db_path
        self.started: list[str] = []
        self.svc._start_thread = lambda experiment_id, _target: self.started.append(experiment_id)
        # A terminal task would otherwise ask the local model for a diagnosis.
        self.svc._schedule_failure_analysis = lambda *_args, **_kwargs: None
        self.addCleanup(self.svc.close)
        self.store = NexusTaskStore(db_path=self.db_path)
        self.addCleanup(self.store.close)

        msg_id = _insert_mailbox_msg(self.svc)
        self.approval_id = self.svc.create_approval(
            msg_id, "solve github issue #288", "tester", "/inbox/t.md", "2026-01-01T00:00:00Z"
        ).id
        self.session = self.store.get_or_create(source_type="task", source_id=self.approval_id)
        # Nexus started the first (and so far only) experiment of this task.
        self.store.append_event(
            self.session.id, role="tool", kind=ACTION_NEW_EXPERIMENT,
            content=json.dumps({"experiment_id": "first"}),
        )
        self.original = self._running_experiment()

    def _running_experiment(self) -> str:
        experiment = self.experiments.create_experiment("solve github issue #288")
        _set_status(self.experiments, experiment.id, STATUS_IMPLEMENTING)
        self.svc._conn.execute(
            "UPDATE mailbox_task_approvals SET status=?, experiment_id=? WHERE id=?",
            (_STATUS_RUNNING, experiment.id, self.approval_id),
        )
        self.svc._conn.commit()
        self.store.wait_for_experiment(self.session.id, experiment.id)
        return experiment.id

    def _restart(self) -> None:
        self.experiments.mark_interrupted_experiments_failed()
        self.svc.recover_after_restart()

    def test_interrupted_task_gets_a_new_experiment_and_can_still_succeed(self):
        self._restart()

        approval = self.svc.get(self.approval_id)
        rerun_id = approval.experiment_id
        self.assertNotEqual(rerun_id, self.original)
        # The recovery's own poll already sees the rerun as in progress.
        self.assertEqual(approval.status, _STATUS_RUNNING)
        self.assertEqual(self.started, [rerun_id])
        self.assertEqual(self.experiments.get_experiment(rerun_id).status, STATUS_QUEUED)
        # The waiting Nexus session now waits on the rerun.
        self.assertEqual(self.store.get(self.session.id).waiting_experiment_id, rerun_id)

        _set_status(self.experiments, rerun_id, "succeeded", test_exit_code=0)
        self.svc.poll_running()
        self.assertEqual(self.svc.get(self.approval_id).status, _STATUS_SUCCEEDED)

    def test_rerun_happens_only_once(self):
        self._restart()
        rerun_id = self.svc.get(self.approval_id).experiment_id

        # A second restart cuts off the rerun as well: no third experiment.
        _set_status(self.experiments, rerun_id, STATUS_IMPLEMENTING)
        self._restart()

        approval = self.svc.get(self.approval_id)
        self.assertEqual(approval.experiment_id, rerun_id)
        self.assertEqual(self.started, [rerun_id])
        # Nexus is still waiting on it and decides what follows.
        self.assertEqual(approval.status, _STATUS_RUNNING)
        with patch.object(self.svc, "_after_experiment") as after:
            self.svc.resume_finished_experiments()
        after.assert_called_once_with(rerun_id)

    def test_rerun_does_not_use_up_the_experiment_limit(self):
        self._restart()
        rerun_id = self.svc.get(self.approval_id).experiment_id
        self.assertEqual(
            len(self.store.recent_events(self.session.id, role="tool", kind=ACTION_NEW_EXPERIMENT)),
            1,
        )

        # The rerun fails for an ordinary reason; Nexus may still try once more.
        _set_status(self.experiments, rerun_id, STATUS_FAILED, error="tests red")
        decision = TaskDecision(action=ACTION_NEW_EXPERIMENT, session_id=self.session.id)
        with patch(_RESUME, return_value=[decision]):
            self.svc._after_experiment(rerun_id)

        approval = self.svc.get(self.approval_id)
        self.assertEqual(approval.status, _STATUS_SUBMITTED)
        self.assertNotIn(approval.experiment_id, (self.original, rerun_id))

    def test_other_failures_still_go_to_nexus(self):
        _set_status(self.experiments, self.original, STATUS_FAILED, error="tests red")
        self.svc.recover_after_restart()

        approval = self.svc.get(self.approval_id)
        self.assertEqual(approval.experiment_id, self.original)
        self.assertEqual(self.started, [])
        self.assertEqual(approval.status, _STATUS_RUNNING)
        with patch.object(self.svc, "_after_experiment") as after:
            self.svc.resume_finished_experiments()
        after.assert_called_once_with(self.original)

    def test_other_failures_without_a_deciding_session_end_the_task(self):
        _set_status(self.experiments, self.original, STATUS_FAILED, error="tests red")
        self.store.set_status(self.session.id, "failed")
        self.svc.recover_after_restart()

        self.assertEqual(self.svc.get(self.approval_id).status, _STATUS_FAILED)
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
