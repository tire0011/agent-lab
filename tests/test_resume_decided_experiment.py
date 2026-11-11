"""A finished experiment whose adoption is decided cannot be resumed (#642).

The controller resumed the review-blocked 08adc04a every tick for hours;
nothing ran, and each tick was a paid controller call outside the budget.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

import nexus_tick
from modules.task_orchestrator import ACTION_ANSWER, NexusTaskOrchestrator
from tests.test_task_orchestrator import _Client, _Registry

RESUME = ('{"type":"action","action":"resume_experiment","experiment_id":"08adc04a","command":null,'
          '"message":"","reason":"continue"}')
ANSWER = ('{"type":"action","action":"answer","experiment_id":null,"command":null,'
          '"message":"stopped","reason":"nothing to resume"}')


class ResumeDecidedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = _Registry(str(Path(tmp.name) / "state.db"), tmp.name)

    def _run(self, adoption_status):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE experiment_adoptions (experiment_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO experiment_adoptions VALUES ('08adc04a60b1', ?)", (adoption_status,))
        service = MagicMock()
        service._conn = conn
        service.resolve_experiment.return_value = SimpleNamespace(id="08adc04a60b1")
        client = _Client([RESUME, ANSWER])
        orchestrator = NexusTaskOrchestrator(
            operations_registry=self.registry, experiment_service=service, llama_client=client,
        )
        return orchestrator.run("Issue #642: retry", source_type="issue", source_id="642"), client

    def test_a_review_blocked_experiment_is_not_resumed(self):
        result, client = self._run("failed")
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertIn("cannot be resumed", client.prompts[-1])
        self.assertIn("new_experiment", client.prompts[-1])

    def test_an_adopted_experiment_is_not_resumed_either(self):
        result, client = self._run("adopted")
        self.assertIn("cannot be resumed", client.prompts[-1])


class CloseIssueTests(unittest.TestCase):
    def test_close_comments_first_and_uses_no_comment_flag(self):
        calls = []
        with mock.patch.object(nexus_tick, "_gh", lambda root, argv: calls.append(argv)):
            nexus_tick._close_issue("/agent", 349, "All parts are closed.")
        self.assertEqual(calls, [["issue", "comment", "349", "--body", "All parts are closed."],
                                 ["issue", "close", "349"]])


if __name__ == "__main__":
    unittest.main()
