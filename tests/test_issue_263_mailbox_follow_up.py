"""#263 step 4: the mailbox carries out what Nexus decides after a failed experiment."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.mailbox.task_service import (
    _STATUS_FAILED,
    _STATUS_NEXUS_DECIDING,
    _STATUS_RUNNING,
    _STATUS_SUBMITTED,
    _STATUS_SUCCEEDED,
)
from modules.nexus_task_store import NexusTaskStore
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_NEW_EXPERIMENT,
    NexusTaskOrchestrator,
    TaskDecision,
)
from tests.test_mailbox_task_service import _insert_mailbox_msg, _make_exp, _make_service

_RESUME = "modules.mailbox.task_service.resume_with_new_orchestrator"


class MailboxFollowUpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.experiments = {}
        exp_service = MagicMock()
        exp_service.get_experiment.side_effect = lambda experiment_id: self.experiments[experiment_id]
        self.svc = _make_service(self.tmp, exp_service=exp_service)
        self.svc.operations_registry.db_path = str(self.tmp / "test.db")
        self.svc._start_thread = lambda *_args: None
        self.addCleanup(self.svc.close)
        msg_id = _insert_mailbox_msg(self.svc)
        approval = self.svc.create_task(
            msg_id, "solve github issue #238", "tester", "/inbox/t.md", "2026-01-01T00:00:00Z"
        )
        self.approval_id = approval.id
        self._run_experiment("exp1", "failed", error="tests red")
        self.store = NexusTaskStore(db_path=str(self.tmp / "test.db"))
        self.addCleanup(self.store.close)
        self.session = self.store.get_or_create(source_type="task", source_id=self.approval_id)

    def _run_experiment(self, experiment_id, status, **fields):
        self.experiments[experiment_id] = _make_exp(experiment_id, status, **fields)
        self.svc._conn.execute(
            "UPDATE mailbox_task_approvals SET status=?, experiment_id=? WHERE id=?",
            (_STATUS_RUNNING, experiment_id, self.approval_id),
        )
        self.svc._conn.commit()

    def _observed_experiments(self, count):
        for n in range(count):
            self.store.append_event(
                self.session.id,
                role="tool",
                kind=ACTION_NEW_EXPERIMENT,
                content=json.dumps({"experiment_id": f"exp{n + 1}", "status": "failed"}),
            )

    def _decision(self, action, **fields):
        return TaskDecision(action=action, session_id=self.session.id, **fields)

    def _nexus_continues(self, decision, before=None):
        """Stand-in for the resumed Nexus: it decides, then runs its own tool."""

        def resume(*_args, experiment_tool=None, **_kwargs):
            if before is not None:
                before()
            if decision.action == ACTION_NEW_EXPERIMENT:
                self.assertIsNotNone(experiment_tool, "the resumed Nexus needs its tool")
                nexus = SimpleNamespace(experiment_tool=experiment_tool, task_store=self.store)
                session = self.store.get(self.session.id)
                NexusTaskOrchestrator._launch_experiment(
                    nexus, session, "solve github issue #238", decision
                )
            return [decision]

        return resume

    def test_nexus_retry_starts_a_second_experiment_for_the_same_task(self):
        self._observed_experiments(1)
        self.svc.experiment_service.create_experiment.return_value = _make_exp("exp2")
        decision = self._decision(
            ACTION_NEW_EXPERIMENT, experiment_handover="What Nexus already found (handover):\n\nfix the test"
        )
        with patch(_RESUME, side_effect=self._nexus_continues(decision)):
            self.svc._after_experiment("exp1")

        approval = self.svc.get(self.approval_id)
        self.assertEqual(approval.status, _STATUS_SUBMITTED)
        self.assertEqual(approval.experiment_id, "exp2")
        goal = self.svc.experiment_service.create_experiment.call_args[0][0]
        self.assertTrue(goal.startswith("solve github issue #238"))
        self.assertIn("fix the test", goal)
        # The new experiment parks the same session again.
        self.assertEqual(self.store.get(self.session.id).waiting_experiment_id, "exp2")

    def test_the_limit_of_two_experiments_ends_the_task(self):
        self._observed_experiments(2)
        decision = self._decision(ACTION_NEW_EXPERIMENT)
        with patch(_RESUME, side_effect=self._nexus_continues(decision)):
            self.svc._after_experiment("exp1")

        approval = self.svc.get(self.approval_id)
        self.assertEqual(approval.status, _STATUS_FAILED)
        self.assertIn("limit of 2 experiments per task", approval.result_summary)
        self.svc.experiment_service.create_experiment.assert_not_called()

    def test_a_nexus_answer_ends_the_task_with_its_words(self):
        self._observed_experiments(1)
        decision = self._decision(ACTION_ANSWER, message="The fix needs a schema change first.")
        with patch(_RESUME, return_value=[decision]):
            self.svc._after_experiment("exp1")

        approval = self.svc.get(self.approval_id)
        self.assertEqual(approval.status, _STATUS_FAILED)
        self.assertIn("Nexus: The fix needs a schema change first.", approval.result_summary)

    def test_a_succeeded_experiment_keeps_its_existing_path(self):
        self._run_experiment("exp1", "succeeded", test_exit_code=0)
        with patch(_RESUME, return_value=[self._decision(ACTION_ANSWER, message="done")]):
            self.svc._after_experiment("exp1")
        self.assertEqual(self.svc.get(self.approval_id).status, _STATUS_RUNNING)

    def test_no_decision_means_another_path_carries_on(self):
        with patch(_RESUME, return_value=[]):
            self.svc._after_experiment("exp1")
        self.assertEqual(self.svc.get(self.approval_id).status, _STATUS_RUNNING)

    def test_poll_cannot_close_the_task_between_resume_and_follow_up(self):
        # Review on #271: the resume leaves the session action_selected before
        # the mailbox carries out the decision; a poll in that window must not
        # close the task.
        self._observed_experiments(1)
        self.svc.experiment_service.create_experiment.return_value = _make_exp("exp2")

        def poll_in_between():
            self.store.set_status(self.session.id, "action_selected")
            self.svc.poll_running()
            self.assertEqual(self.svc.get(self.approval_id).status, _STATUS_NEXUS_DECIDING)

        resume = self._nexus_continues(self._decision(ACTION_NEW_EXPERIMENT), poll_in_between)
        with patch(_RESUME, side_effect=resume):
            self.svc._after_experiment("exp1")

        approval = self.svc.get(self.approval_id)
        self.assertEqual(approval.status, _STATUS_SUBMITTED)
        self.assertEqual(approval.experiment_id, "exp2")

    def test_a_failed_resume_hands_the_task_back_to_the_ordinary_path(self):
        with patch(_RESUME, side_effect=RuntimeError("model down")):
            with self.assertRaises(RuntimeError):
                self.svc._after_experiment("exp1")
        self.assertEqual(self.svc.get(self.approval_id).status, _STATUS_RUNNING)

    def test_a_restart_hands_an_interrupted_decision_back(self):
        self.svc._claim_decision("exp1")
        self.store.set_status(self.session.id, "abandoned")
        self.svc.recover_after_restart()
        self.assertEqual(self.svc.get(self.approval_id).status, _STATUS_FAILED)

    def test_the_task_store_uses_the_service_database_not_a_mock_attribute(self):
        # #279: a MagicMock operations_registry used to name the database file.
        self.svc.operations_registry.db_path = MagicMock()
        self.svc._nexus_task_store = None
        path = self.svc._with_task_store(lambda store: store.db_path)
        self.addCleanup(self.svc._nexus_task_store.close)
        self.assertEqual(path, self.svc.db_path)

    def test_poll_leaves_a_failed_task_open_while_nexus_decides(self):
        self.store.wait_for_experiment(self.session.id, "exp1")
        self.svc.poll_running()
        self.assertEqual(self.svc.get(self.approval_id).status, _STATUS_RUNNING)

        # Once the session is no longer deciding, the ordinary path closes it.
        self.store.set_status(self.session.id, "failed")
        self.svc.poll_running()
        self.assertEqual(self.svc.get(self.approval_id).status, _STATUS_FAILED)

    def test_poll_still_finishes_a_succeeded_task(self):
        self._run_experiment("exp1", "succeeded", test_exit_code=0)
        self.store.wait_for_experiment(self.session.id, "exp1")
        self.svc.poll_running()
        self.assertEqual(self.svc.get(self.approval_id).status, _STATUS_SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
