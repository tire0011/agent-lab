from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from models import Idea
from modules.nexus_controller_service import NexusControllerService
from modules.task_orchestrator import TaskDecision


class NexusControllerServiceTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = MagicMock()
        self.orchestrator.run.return_value = TaskDecision(action="answer", session_id="session-1")
        self.service = NexusControllerService(self.orchestrator)

    def test_issue_is_passed_to_same_controller_with_source_identity(self):
        self.service.handle_issue(
            46,
            title="Workflow routing",
            body="Let the local LLM decide what to do.",
            state="OPEN",
        )
        args, kwargs = self.orchestrator.run.call_args
        self.assertIn("GitHub Issue #46", args[0])
        self.assertEqual(kwargs["source_type"], "issue")
        self.assertEqual(kwargs["source_id"], "46")

    @patch("modules.nexus_sources.list_ideas")
    def test_idea_is_passed_to_same_controller(self, list_ideas):
        list_ideas.return_value = [
            Idea(id="idea-3", section="Agents", text="Try a memory search tool", state="new")
        ]
        self.service.handle_idea("idea-3")
        args, kwargs = self.orchestrator.run.call_args
        self.assertEqual(args[0], "Try a memory search tool")
        self.assertEqual(kwargs["source_type"], "idea")
        self.assertEqual(kwargs["source_id"], "idea-3")

    def test_reply_resumes_same_persistent_session(self):
        self.orchestrator.continue_session.return_value = TaskDecision(
            action="answer", session_id="session-1"
        )
        result = self.service.reply("session-1", "Optimize for cost")
        self.assertEqual(result.session_id, "session-1")
        self.orchestrator.continue_session.assert_called_once_with(
            "session-1", "Optimize for cost"
        )


if __name__ == "__main__":
    unittest.main()
