from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models import Idea
from modules.github_work_tracking import GitHubWorkTrackingError, GitHubWorkTrackingService
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
        self.github_work_tracking = MagicMock()
        self.github_work_tracking.configured = True
        self.github_work_tracking.ensure_for_source.return_value = SimpleNamespace(
            issue_number=42,
            branch_name="nexus/42-controller-test",
        )
        self.github_work_tracking.bind_experiment.side_effect = (
            lambda ref, experiment_id: SimpleNamespace(
                issue_number=ref.issue_number,
                branch_name=ref.branch_name,
                experiment_id=experiment_id,
            )
        )
        self.github_work_tracking.ensure_for_experiment.return_value = SimpleNamespace(
            issue_number=42,
            branch_name="nexus/42-controller-test",
            experiment_id="exp-existing",
        )
        self.service = NexusControllerService(
            self.orchestrator,
            github_work_tracking=self.github_work_tracking,
        )

    def tearDown(self):
        close = getattr(self.service.github_work_tracking, "close", None)
        if close:
            close()
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

        self.github_work_tracking.ensure_for_source.assert_called_once()
        self.github_work_tracking.bind_experiment.assert_called_once()
        self.orchestrator.experiment_service.create_experiment.assert_called_once_with(
            "Implement the requested feature"
        )

    def test_missing_github_configuration_blocks_new_experiment_before_creation(self):
        actual_tracking = GitHubWorkTrackingService(
            db_path=str(Path(self.tmp.name) / "missing-github.db"),
            repo_root=self.tmp.name,
        )
        service = NexusControllerService(
            self.orchestrator,
            github_work_tracking=actual_tracking,
        )
        self.addCleanup(actual_tracking.close)
        self.orchestrator.run.return_value = TaskDecision(
            action=ACTION_NEW_EXPERIMENT,
            session_id="session-1",
        )

        with self.assertRaises(GitHubWorkTrackingError) as ctx:
            service.handle_task("Implement the requested feature", source_id="task-1")

        self.assertIn("GITHUB_REPOSITORY", str(ctx.exception))
        self.assertIn("GITHUB_TOKEN/GH_TOKEN", str(ctx.exception))
        self.orchestrator.experiment_service.create_experiment.assert_not_called()
        self.orchestrator.task_store.append_event.assert_any_call(
            "session-1",
            role="tool",
            kind="github_work_tracking_unavailable",
            content=str(ctx.exception),
        )

    def test_missing_github_configuration_blocks_linked_resume(self):
        actual_tracking = GitHubWorkTrackingService(
            db_path=str(Path(self.tmp.name) / "resume-github.db"),
            repo_root=self.tmp.name,
            repository="tire0011/agent-lab",
            transport=lambda method, path, payload: (
                {"items": []}
                if method == "GET" and path.startswith("/search/issues?")
                else {"number": 55, "title": "Resume lifecycle", "state": "open"}
                if method == "POST" and path == "/repos/tire0011/agent-lab/issues"
                else {"object": {"sha": "main-sha"}}
                if method == "GET" and "/git/ref/heads/" in path
                else {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
                if method == "POST" and path == "/repos/tire0011/agent-lab/git/refs"
                else (_ for _ in ()).throw(AssertionError(f"unexpected {method} {path}"))
            ),
        )
        ref = actual_tracking.ensure_for_source(
            SimpleNamespace(
                source_type="task",
                source_id="session-1",
                title="Resume lifecycle",
                content="Resume lifecycle",
            )
        )
        actual_tracking.bind_experiment(ref, "exp-linked")
        actual_tracking.close()

        unconfigured_tracking = GitHubWorkTrackingService(
            db_path=str(Path(self.tmp.name) / "resume-github.db"),
            repo_root=self.tmp.name,
            repository="tire0011/agent-lab",
        )
        service = NexusControllerService(
            self.orchestrator,
            github_work_tracking=unconfigured_tracking,
        )
        self.addCleanup(unconfigured_tracking.close)
        self.orchestrator.continue_session.return_value = TaskDecision(
            action=ACTION_RESUME_EXPERIMENT,
            session_id="session-1",
            experiment_id="exp-linked",
        )

        with self.assertRaises(GitHubWorkTrackingError):
            service.reply("session-1", "Continue the linked experiment")

        self.orchestrator.task_store.append_event.assert_any_call(
            "session-1",
            role="tool",
            kind="github_work_tracking_unavailable",
            content=unittest.mock.ANY,
        )

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
