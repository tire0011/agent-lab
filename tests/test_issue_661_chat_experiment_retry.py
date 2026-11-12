"""A resumed chat session may retry an experiment, but only within its authority."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.nexus_chat_service import NexusChatService
from modules.nexus_task_store import NexusTaskStore
from modules.task_orchestrator import ACTION_NEW_EXPERIMENT, TaskDecision


class ChatExperimentRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "state.db")
        self.registry = SimpleNamespace(db_path=self.db_path, repo_root=self.tmp.name)
        self.experiments = MagicMock()
        self.github = MagicMock()
        self.github.ensure_for_source.return_value = SimpleNamespace(issue_number=661)
        self.github.bind_experiment.side_effect = lambda work, _experiment_id: work
        self.service = NexusChatService(
            operations_registry=self.registry,
            experiment_service=self.experiments,
            github_work_tracking=self.github,
            background_starter=lambda _name, _target: None,
        )

    def _parked_session(self, *, allow_mutation, attempts):
        store = NexusTaskStore(db_path=self.db_path)
        session = store.get_or_create(
            source_type="chat", source_id=f"chat-{allow_mutation}-{attempts}", title="Nexus chat"
        )
        store.append_event(session.id, role="user", kind="input", content="Implement the fix")
        for number in range(attempts):
            store.append_event(
                session.id,
                role="tool",
                kind=ACTION_NEW_EXPERIMENT,
                content=json.dumps({"experiment_id": f"exp-{number + 1}", "status": "failed"}),
            )
        store.wait_for_experiment(
            session.id, f"exp-{attempts}", allow_mutation=allow_mutation
        )
        store.close()
        return session.id

    def test_failed_experiment_can_start_second_with_same_session_and_handover(self):
        session_id = self._parked_session(allow_mutation=True, attempts=1)
        self.experiments.create_experiment.return_value = SimpleNamespace(id="exp-2")
        decision = TaskDecision(
            action=ACTION_NEW_EXPERIMENT,
            session_id=session_id,
            experiment_handover="What Nexus already found (handover):\n\ntests failed: assertion red",
        )

        self.service._launch_resumed_experiment(decision)

        goal = self.experiments.create_experiment.call_args.args[0]
        self.assertIn("tests failed: assertion red", goal)
        store = NexusTaskStore(db_path=self.db_path)
        try:
            session = store.get(session_id)
            self.assertEqual(session.waiting_experiment_id, "exp-2")
            self.assertTrue(session.allow_mutation)
        finally:
            store.close()

    def test_session_without_mutation_rights_cannot_start_on_resume(self):
        session_id = self._parked_session(allow_mutation=False, attempts=1)
        self.service._launch_resumed_experiment(
            TaskDecision(action=ACTION_NEW_EXPERIMENT, session_id=session_id)
        )
        self.experiments.create_experiment.assert_not_called()

    def test_default_limit_stops_a_fourth_experiment(self):
        session_id = self._parked_session(allow_mutation=True, attempts=3)
        with patch.dict("os.environ", {}, clear=False):
            self.service._launch_resumed_experiment(
                TaskDecision(action=ACTION_NEW_EXPERIMENT, session_id=session_id)
            )

        self.experiments.create_experiment.assert_not_called()
        store = NexusTaskStore(db_path=self.db_path)
        try:
            session = store.get(session_id)
            self.assertEqual(session.status, "needs_review")
            event = store.recent_events(session_id, role="system", kind="needs_review", limit=1)[0]
            self.assertIn("limit of 3 experiments per session", event["content"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
