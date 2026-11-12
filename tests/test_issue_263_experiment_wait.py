"""#263 step 3: Nexus pauses on its experiment and continues with the result."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.experiment_wait import experiment_result, run_then_resume
from modules.nexus_task_store import RESTING_STATUSES, NexusTaskStore
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_NEW_EXPERIMENT,
    NexusTaskOrchestrator,
)
from tests.test_issue_111_controller_protocol import _Client, _Delegation, _Registry


class WaitingStoreTests(unittest.TestCase):
    def test_a_parked_session_is_resting_and_claimed_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NexusTaskStore(db_path=str(Path(tmp) / "n.db"))
            try:
                session = store.get_or_create(source_type="task", source_id="263")
                self.assertTrue(store.wait_for_experiment(session.id, "exp-1"))
                parked = store.get(session.id)
                self.assertEqual(parked.status, "waiting_for_experiment")
                self.assertIn(parked.status, RESTING_STATUSES)
                self.assertEqual(parked.waiting_experiment_id, "exp-1")
                self.assertEqual(
                    [s.id for s in store.sessions_waiting_for_experiments("exp-1")],
                    [session.id],
                )
                self.assertEqual(store.sessions_waiting_for_experiments("exp-2"), [])

                # Event path and polling safety net may both see the end.
                self.assertFalse(
                    store.claim_waiting_for_experiment(session.id, "exp-2", worker_id="w")
                )
                self.assertTrue(
                    store.claim_waiting_for_experiment(session.id, "exp-1", worker_id="w")
                )
                self.assertFalse(
                    store.claim_waiting_for_experiment(session.id, "exp-1", worker_id="w2")
                )
                self.assertEqual(store.get(session.id).status, "active")
            finally:
                store.close()

    def test_parking_persists_mutation_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NexusTaskStore(db_path=str(Path(tmp) / "n.db"))
            try:
                allowed = store.get_or_create(source_type="chat", source_id="allowed")
                denied = store.get_or_create(source_type="chat", source_id="denied")
                store.wait_for_experiment(allowed.id, "exp-a", allow_mutation=True)
                store.wait_for_experiment(denied.id, "exp-b", allow_mutation=False)
                self.assertTrue(store.get(allowed.id).allow_mutation)
                self.assertFalse(store.get(denied.id).allow_mutation)
            finally:
                store.close()


class ExperimentResultTests(unittest.TestCase):
    def test_success_carries_status_and_pr_without_reason(self):
        result = experiment_result(
            SimpleNamespace(id="exp-1", status="succeeded", error=None, test_exit_code=0),
            SimpleNamespace(pr_number=42),
        )
        self.assertEqual(
            result,
            {"tool": "new_experiment", "experiment_id": "exp-1", "status": "succeeded",
             "pr_number": 42, "reason": None},
        )

    def test_failure_says_why(self):
        tests_red = experiment_result(
            SimpleNamespace(id="e", status="failed", error=None, test_exit_code=1,
                            test_output="FAILED tests/test_x.py::test_y"),
        )
        self.assertIn("tests failed (exit 1)", tests_red["reason"])
        self.assertIn("test_x.py::test_y", tests_red["reason"])
        self.assertIsNone(tests_red["pr_number"])
        errored = experiment_result(
            SimpleNamespace(id="e", status="needs_review", error="no change produced",
                            test_exit_code=None),
        )
        self.assertEqual(errored["reason"], "no change produced")


class ResumeTests(unittest.TestCase):
    def test_the_same_session_continues_with_the_result_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = _Registry(str(Path(tmp) / "state.db"), tmp)
            client = _Client([
                '{"action":"new_experiment","arguments":{"goal":"add a kind filter"},'
                '"reason":"implement"}',
                '{"action":"answer","message":"PR #42 is ready","reason":"done"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=MagicMock(),
                llama_client=client,
                delegation_service=_Delegation(),
            )
            try:
                first = orchestrator.run(
                    "solve github issue #238", source_type="task", source_id="263-wait"
                )
                self.assertEqual(first.action, ACTION_NEW_EXPERIMENT)
                orchestrator.task_store.wait_for_experiment(first.session_id, "exp-1")

                result = {"tool": "new_experiment", "experiment_id": "exp-1",
                          "status": "succeeded", "pr_number": 42, "reason": None}
                resumed = orchestrator.resume_after_experiment("exp-1", result)
                again = orchestrator.resume_after_experiment("exp-1", result)
                session = orchestrator.task_store.get(first.session_id)
            finally:
                orchestrator.close()

        self.assertEqual([d.action for d in resumed], [ACTION_ANSWER])
        self.assertEqual(resumed[0].session_id, first.session_id)
        self.assertEqual(again, [])
        # The result is an observation of this run, and the original task
        # is still what the controller works on.
        continuation = client.prompts[1]
        self.assertIn('"pr_number":42', continuation.partition("OBSERVATIONS FROM TOOLS")[2])
        self.assertIn("solve github issue #238", continuation)
        self.assertNotEqual(session.status, "waiting_for_experiment")


class RunThenResumeTests(unittest.TestCase):
    def test_the_session_continues_even_when_the_run_raises(self):
        resume = MagicMock()

        def run():
            raise RuntimeError("pipeline crashed")

        with self.assertRaises(RuntimeError):
            run_then_resume(run, resume, "exp-1")
        resume.assert_called_once()

    def test_a_failing_resume_is_logged_not_raised(self):
        resume = MagicMock(side_effect=RuntimeError("llm down"))
        with self.assertLogs("modules.experiment_wait", level="ERROR"):
            run_then_resume(lambda: None, resume, "exp-1")


if __name__ == "__main__":
    unittest.main()
