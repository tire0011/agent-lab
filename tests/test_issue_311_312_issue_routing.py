"""#311: an issue to solve starts an experiment once its briefing is there.
#312: a clarification that only repeats a tool's guidance is rejected.
"""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modules.task_orchestrator import (
    ACTION_CLARIFICATION,
    ACTION_NEW_EXPERIMENT,
    NexusTaskOrchestrator,
)
from tests.test_issue_234_completion_evidence import _orchestrator

BRIEFING = {
    "tool": "knowledge_collector",
    "knowledge_briefing": "GitHub issue #196: Fix controller completion behavior.",
    "used_sources": ["github_issue"],
}
COLLECT = '{"action":"knowledge_collector","request":"issue 196","reason":"read the issue first"}'


def _run(task: str, responses: list[str]):
    with tempfile.TemporaryDirectory() as tmp:
        orchestrator, _registry, client = _orchestrator(tmp, responses)
        try:
            with patch.object(
                orchestrator, "_run_knowledge_collector", AsyncMock(return_value=dict(BRIEFING))
            ):
                result = orchestrator.run(task)
        finally:
            orchestrator.close()
    return result, client


class IssueBriefingStartsExperimentTests(unittest.TestCase):
    def test_solve_issue_goes_to_an_experiment_after_the_briefing(self):
        # Only the first turn asks the model; the step after the briefing is fixed.
        result, client = _run("solve github issue #196", [COLLECT])
        self.assertEqual(result.action, ACTION_NEW_EXPERIMENT)
        self.assertEqual(len(client.prompts), 1)
        self.assertIn("Fix controller completion behavior", result.experiment_handover)

    def test_other_tasks_still_ask_the_controller(self):
        answer = '{"action":"answer","message":"It is about completion.","reason":"briefing read"}'
        result, client = _run("what is github issue #196 about?", [COLLECT, answer])
        self.assertEqual(result.action, "answer")
        self.assertEqual(len(client.prompts), 2)


class ClarificationRepeatingToolGuidanceTests(unittest.TestCase):
    GUIDANCE = {
        "tool": "find_tool",
        "status": "invalid_goal",
        "guidance": "Please provide a concrete capability request with specific values for the required arguments.",
    }

    def _validate(self, message: str):
        orchestrator = object.__new__(NexusTaskOrchestrator)
        orchestrator.task_store = MagicMock()
        orchestrator.task_store.get.return_value = SimpleNamespace(requires_completion_evidence=False)
        orchestrator.tool_router = MagicMock()
        orchestrator.tool_router.candidate_operations.return_value = []
        return orchestrator._validate_evidence_sensitive_terminal(
            session_id="s1",
            action=ACTION_CLARIFICATION,
            decision={"action": ACTION_CLARIFICATION, "message": message},
            observations=[self.GUIDANCE],
        )

    def test_a_question_that_repeats_tool_guidance_is_rejected(self):
        # The question the controller asked in the live #301 run 3.
        rejection = self._validate(
            "Could you please provide a concrete capability request with specific "
            "values for the required arguments?"
        )
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection["event"], "terminal_decision_rejected")
        self.assertEqual(rejection["gap_kind"], "tool_guidance")

    def test_an_ordinary_question_is_unaffected(self):
        self.assertIsNone(self._validate("Which branch should the fix go to?"))


if __name__ == "__main__":
    unittest.main()
