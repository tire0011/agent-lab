from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models import Idea
from modules.github_work_tracking import GitHubWorkTrackingError
from modules.nexus_controller_service import NexusControllerService
from modules.task_orchestrator import ACTION_NEW_EXPERIMENT, ACTION_RESUME_EXPERIMENT, TaskDecision


class NexusControllerServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orchestrator = MagicMock()
        self.orchestrator.run.return_value = TaskDecision(action="answer", session_id="session-1")
        self.orchestrator.experiment_service = MagicMock()
        self.orchestrator.experiment_service.db_path = str(Path(self.tmp.name) / "agent.db")
        self.orchestrator.operations_registry = SimpleNamespace(repo_root=Path(self.tmp.name))
        self.orchestrator.task_store = MagicMock()
        self.orchestrator.task_store.history.return_value = []
        self.github = MagicMock()
        self.github.ensure_for_source.return_value = SimpleNamespace(
            issue_number=42, branch_name="nexus/42-controller"
        )
        self.github.bind_experiment.side_effect = lambda ref, experiment_id: ref
        self.github.recover_experiment.return_value = SimpleNamespace(
            issue_number=42, branch_name="nexus/42-controller"
        )
        self.service = NexusControllerService(
            self.orchestrator, github_work_tracking=self.github
        )

    def tearDown(self):
        self.tmp.cleanup()

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

    def test_selected_new_experiment_is_actually_dispatched(self):
        experiment = SimpleNamespace(id="exp-123")
        self.orchestrator.run.return_value = TaskDecision(
            action=ACTION_NEW_EXPERIMENT,
            session_id="session-1",
        )
        self.orchestrator.experiment_service.create_experiment.return_value = experiment

        self.service.handle_task("Implement the requested feature", source_id="task-1")

        self.orchestrator.experiment_service.create_experiment.assert_called_once_with(
            "Implement the requested feature"
        )
        self.github.ensure_for_source.assert_called_once()
        self.github.bind_experiment.assert_called_once()

    def test_new_experiment_goal_is_the_task_followed_by_the_handover(self):
        # #263: what Nexus already found travels with the task.
        self.orchestrator.run.return_value = TaskDecision(
            action=ACTION_NEW_EXPERIMENT,
            session_id="session-1",
            experiment_handover="What Nexus already found (handover):\n\nNexus goal:\nadd a kind filter",
        )
        self.orchestrator.experiment_service.create_experiment.return_value = SimpleNamespace(
            id="exp-263"
        )

        self.service.handle_task("Implement the requested feature", source_id="task-1")

        goal = self.orchestrator.experiment_service.create_experiment.call_args[0][0]
        self.assertTrue(goal.startswith("Implement the requested feature\n\n"))
        self.assertIn("Nexus goal:\nadd a kind filter", goal)

    def test_missing_github_configuration_blocks_before_experiment_creation(self):
        self.orchestrator.run.return_value = TaskDecision(
            action=ACTION_NEW_EXPERIMENT,
            session_id="session-1",
        )
        self.github.ensure_for_source.side_effect = GitHubWorkTrackingError(
            "Configure GITHUB_TOKEN/GH_TOKEN"
        )

        with self.assertRaises(GitHubWorkTrackingError):
            self.service.handle_task("Implement the requested feature", source_id="task-1")

        self.orchestrator.experiment_service.create_experiment.assert_not_called()

    def test_resume_recovers_same_experiment_before_starting_thread(self):
        experiment = SimpleNamespace(id="exp-existing", goal="Existing goal")
        self.orchestrator.continue_session.return_value = TaskDecision(
            action=ACTION_RESUME_EXPERIMENT,
            session_id="session-1",
            experiment_id=experiment.id,
        )
        self.orchestrator.experiment_service.resolve_experiment.return_value = experiment

        self.service.reply("session-1", "Continue")

        self.github.recover_experiment.assert_called_once_with(experiment)

    def test_reply_new_experiment_uses_original_input_and_followup(self):
        experiment = SimpleNamespace(id="exp-after-reply")
        self.orchestrator.continue_session.return_value = TaskDecision(
            action=ACTION_NEW_EXPERIMENT,
            session_id="session-1",
        )
        self.orchestrator.experiment_service.create_experiment.return_value = experiment
        self.orchestrator.task_store.history.return_value = [
            {"role": "user", "kind": "input", "content": "Improve model routing"},
            {"role": "assistant", "kind": "clarification", "content": "Cost or latency?"},
            {"role": "user", "kind": "input", "content": "Optimize for cost"},
        ]

        self.service.reply("session-1", "Optimize for cost")

        goal = self.orchestrator.experiment_service.create_experiment.call_args[0][0]
        self.assertIn("Improve model routing", goal)
        self.assertIn("Optimize for cost", goal)

    def test_reply_resumes_same_persistent_session(self):
        self.orchestrator.continue_session.return_value = TaskDecision(
            action="answer",
            session_id="session-1",
        )

        self.service.reply("session-1", "Continue")

        self.orchestrator.continue_session.assert_called_once_with("session-1", "Continue")


if __name__ == "__main__":
    unittest.main()
