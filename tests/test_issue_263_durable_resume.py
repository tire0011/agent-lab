"""#263: a parked Nexus session resumes after a restart, whoever owns it.

The experiment thread resumes its session when the experiment ends. A process
restart (or a failed callback) loses that moment; the waiting session is a row
in the task store, so each owner - mailbox, chat, controller - finds its own
parked sessions there and resumes them with the result observation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.experiment_wait import (
    CONTROLLER_EXCLUDED_SOURCE_TYPES,
    ExperimentResumeWorker,
    finished_waiting_experiments,
    resume_finished_experiments,
)
from modules.nexus_chat_service import NexusChatService, _ChatOrchestrator
from modules.nexus_controller_service import NexusControllerService
from modules.nexus_task_store import NexusTaskStore
from modules.task_orchestrator import ACTION_CLARIFICATION, NexusTaskOrchestrator
from tests.test_issue_111_controller_protocol import _Client, _Delegation, _Registry

_ANSWER = '{"action":"clarification","message":"The experiment failed.","reason":"report"}'


def _experiments(statuses: dict[str, str]):
    experiments = MagicMock()
    experiments.get_experiment.side_effect = lambda eid: SimpleNamespace(
        id=eid, status=statuses[eid], error="tests red: test_x" if statuses[eid] != "succeeded" else None,
        test_exit_code=None, test_output="",
    )
    return experiments


def _github():
    github = MagicMock()
    github.for_experiment.return_value = SimpleNamespace(pr_number=12)
    return github


def _park(db: str, source_type: str, source_id: str, experiment_id: str) -> str:
    store = NexusTaskStore(db_path=db)
    try:
        session = store.get_or_create(source_type=source_type, source_id=source_id, title=source_id)
        store.append_event(session.id, role="user", kind="input", content=f"work on {source_id}")
        assert store.wait_for_experiment(session.id, experiment_id)
        return session.id
    finally:
        store.close()


class SourceOwnershipTests(unittest.TestCase):
    def test_each_owner_sees_only_its_own_finished_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "n.db")
            _park(db, "task", "mail-1", "exp-mail")
            _park(db, "chat", "chat-1", "exp-chat")
            _park(db, "issue", "238", "exp-issue")
            _park(db, "idea", "idea-1", "exp-running")
            experiments = _experiments({
                "exp-mail": "failed", "exp-chat": "succeeded",
                "exp-issue": "needs_review", "exp-running": "implementing",
            })
            store = NexusTaskStore(db_path=db)
            try:
                mailbox = finished_waiting_experiments(store, experiments, source_types=("task",))
                chat = finished_waiting_experiments(store, experiments, source_types=("chat",))
                controller = finished_waiting_experiments(
                    store, experiments, exclude_source_types=CONTROLLER_EXCLUDED_SOURCE_TYPES
                )
                seen: list[str] = []
                resume_finished_experiments(
                    store, experiments, seen.append, source_types=("chat",)
                )
            finally:
                store.close()
        self.assertEqual(mailbox, ["exp-mail"])
        self.assertEqual(chat, ["exp-chat"])
        # A still-running experiment is left alone.
        self.assertEqual(controller, ["exp-issue"])
        self.assertEqual(seen, ["exp-chat"])

    def test_a_failing_resume_does_not_stop_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "n.db")
            _park(db, "issue", "1", "exp-a")
            _park(db, "issue", "2", "exp-b")
            experiments = _experiments({"exp-a": "failed", "exp-b": "failed"})
            calls: list[str] = []

            def resume(experiment_id):
                calls.append(experiment_id)
                if experiment_id == "exp-a":
                    raise RuntimeError("boom")

            store = NexusTaskStore(db_path=db)
            try:
                resumed = resume_finished_experiments(store, experiments, resume)
            finally:
                store.close()
        self.assertEqual(calls, ["exp-a", "exp-b"])
        self.assertEqual(resumed, ["exp-b"])

    def test_the_worker_runs_its_steps_at_once(self):
        calls: list[str] = []
        worker = ExperimentResumeWorker(
            [lambda: calls.append("a"), lambda: (_ for _ in ()).throw(RuntimeError("x")),
             lambda: calls.append("c")],
        )
        worker.run_once()
        self.assertEqual(calls, ["a", "c"])


class ControllerRestartRecoveryTests(unittest.TestCase):
    def test_a_fresh_controller_resumes_a_parked_issue_session_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state.db")
            # Before the restart: the session parked on its experiment.
            session_id = _park(db, "issue", "238", "exp-238")
            # The restart marked the experiment terminal; no callback ever ran.
            experiments = _experiments({"exp-238": "interrupted"})
            client = _Client([_ANSWER])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=_Registry(db, tmp),
                experiment_service=experiments,
                llama_client=client,
                delegation_service=_Delegation(),
            )
            try:
                controller = NexusControllerService(orchestrator, github_work_tracking=_github())
                resumed = controller.resume_finished_experiments()
                again = controller.resume_finished_experiments()
                after = orchestrator.task_store.get(session_id)
            finally:
                orchestrator.close()

        self.assertEqual(resumed, ["exp-238"])
        self.assertEqual(again, [])
        self.assertNotEqual(after.status, "waiting_for_experiment")
        self.assertEqual(len(client.prompts), 1)
        observations = client.prompts[0].partition("OBSERVATIONS FROM TOOLS")[2]
        self.assertIn('"status":"interrupted"', observations)
        self.assertIn('"pr_number":12', observations)
        self.assertIn("tests red: test_x", observations)

    def test_the_controller_leaves_mailbox_and_chat_sessions_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state.db")
            mail = _park(db, "task", "m", "exp-m")
            chat = _park(db, "chat", "c", "exp-c")
            experiments = _experiments({"exp-m": "failed", "exp-c": "failed"})
            orchestrator = NexusTaskOrchestrator(
                operations_registry=_Registry(db, tmp),
                experiment_service=experiments,
                llama_client=_Client([]),
                delegation_service=_Delegation(),
            )
            try:
                resumed = NexusControllerService(
                    orchestrator, github_work_tracking=_github()
                ).resume_finished_experiments()
                statuses = [orchestrator.task_store.get(s).status for s in (mail, chat)]
            finally:
                orchestrator.close()
        self.assertEqual(resumed, [])
        self.assertEqual(statuses, ["waiting_for_experiment"] * 2)


class ChatRestartRecoveryTests(unittest.TestCase):
    def test_a_fresh_chat_service_resumes_a_parked_chat_session_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state.db")
            session_id = _park(db, "chat", "chat-7", "exp-chat")
            experiments = _experiments({"exp-chat": "failed"})
            client = _Client([_ANSWER])
            built: list[dict] = []

            def factory(**kwargs):
                built.append(kwargs)
                return _ChatOrchestrator(
                    llama_client=client, delegation_service=_Delegation(), **kwargs
                )

            registry = _Registry(db, tmp)
            service = NexusChatService(
                operations_registry=registry,
                experiment_service=experiments,
                orchestrator_factory=factory,
                github_work_tracking=_github(),
            )
            resumed = service.resume_finished_experiments()
            store = NexusTaskStore(db_path=db)
            try:
                after = store.get(session_id)
                events = store.recent_events(session_id, role="tool", kind="new_experiment")
            finally:
                store.close()

        self.assertEqual(resumed, ["exp-chat"])
        # The resumed chat turn is discussion-only, as on the event path.
        self.assertEqual([b["allow_mutation"] for b in built], [False])
        self.assertNotEqual(after.status, "waiting_for_experiment")
        self.assertTrue(events)
        self.assertIn('"status":"failed"', client.prompts[0])


if __name__ == "__main__":
    unittest.main()
