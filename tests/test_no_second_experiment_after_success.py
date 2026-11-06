"""An issue session does not start a second experiment once one succeeded (#530)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.task_orchestrator import ACTION_ANSWER, ACTION_NEW_EXPERIMENT, NexusTaskOrchestrator
from tests.test_task_orchestrator import _Client, _Registry

NEW_EXPERIMENT = (
    '{"type":"action","action":"new_experiment","experiment_id":null,"command":null,'
    '"goal":"Implement it","message":"","reason":"needs a code change"}'
)


class NoSecondExperimentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = _Registry(str(Path(tmp.name) / "state.db"), tmp.name)

    def _first_experiment(self, source_type: str, status: str):
        service = MagicMock()
        service.get_experiment.return_value = SimpleNamespace(status=status)
        orchestrator = NexusTaskOrchestrator(
            operations_registry=self.registry,
            experiment_service=service,
            llama_client=_Client([NEW_EXPERIMENT, NEW_EXPERIMENT]),
        )
        first = orchestrator.run("Issue #348: token budget", source_type=source_type, source_id="348")
        self.assertEqual(first.action, ACTION_NEW_EXPERIMENT)
        # What the controller service journals once the experiment ended.
        orchestrator.task_store.append_event(
            first.session_id, role="tool", kind=ACTION_NEW_EXPERIMENT,
            content="{}", metadata={"experiment_id": "2aeeca59"},
        )
        return orchestrator.run(
            "Experiment 2aeeca59 finished", source_type=source_type, source_id="348",
            session_id=first.session_id,
        )

    def test_issue_session_refuses_a_second_experiment_after_success(self):
        second = self._first_experiment("issue", "succeeded")
        self.assertEqual(second.action, ACTION_ANSWER)
        self.assertIn("2aeeca59", second.message)

    def test_a_failed_experiment_may_be_retried(self):
        self.assertEqual(self._first_experiment("issue", "failed").action, ACTION_NEW_EXPERIMENT)

    def test_chat_sessions_may_ask_for_follow_up_work(self):
        self.assertEqual(self._first_experiment("chat", "succeeded").action, ACTION_NEW_EXPERIMENT)


    def test_a_review_block_allows_the_retry_in_the_same_session(self):
        # #546: the guard refused the review retry, so #276 was dispatched
        # every tick without a new experiment.
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE experiment_adoptions (experiment_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO experiment_adoptions VALUES ('2aeeca59', 'failed')")
        service = MagicMock()
        service.get_experiment.return_value = SimpleNamespace(status="succeeded")
        service._conn = conn
        orchestrator = NexusTaskOrchestrator(
            operations_registry=self.registry,
            experiment_service=service,
            llama_client=_Client([NEW_EXPERIMENT, NEW_EXPERIMENT]),
        )
        first = orchestrator.run("Issue #348", source_type="issue", source_id="348")
        orchestrator.task_store.append_event(
            first.session_id, role="tool", kind=ACTION_NEW_EXPERIMENT,
            content="{}", metadata={"experiment_id": "2aeeca59"},
        )
        second = orchestrator.run("Review blocked; retry", source_type="issue", source_id="348",
                                  session_id=first.session_id)
        self.assertEqual(second.action, ACTION_NEW_EXPERIMENT)

if __name__ == "__main__":
    unittest.main()
