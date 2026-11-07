from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models import Idea
from modules.nexus_controller_service import NexusControllerService
from modules.nexus_experiment_tool import (
    LAUNCH_ALREADY_ACTIVE,
    LAUNCH_BLOCKED,
    LAUNCH_SUBMITTED,
    ExperimentLaunch,
    NexusExperimentTool,
)
from modules.nexus_task_store import NexusTaskStore
from modules.task_orchestrator import ACTION_NEW_EXPERIMENT, ACTION_RESUME_EXPERIMENT, TaskDecision
from modules.nexus_sources import task_source


class NexusControllerServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orchestrator = MagicMock()
        self.orchestrator.run.return_value = TaskDecision(action="answer", session_id="session-1")
        self.orchestrator.experiment_service = MagicMock()
        self.orchestrator.experiment_service.db_path = str(Path(self.tmp.name) / "agent.db")
        self.orchestrator.operations_registry = SimpleNamespace(repo_root=Path(self.tmp.name))
        self.orchestrator.task_store = MagicMock()
        self.orchestrator.task_store.user_inputs.return_value = []
        self.orchestrator.experiment_tool = None
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

    def test_nexus_gets_its_experiment_tool_wired_to_this_service(self):
        # #263 step 4: the service configures the tool, Nexus executes it.
        tool = self.orchestrator.experiment_tool
        self.assertIsInstance(tool, NexusExperimentTool)
        self.assertIs(tool.github_work_tracking, self.github)
        self.assertEqual(tool.background, self.service._start_experiment_thread)
        self.assertEqual(tool.on_finished, self.service._resume_after_experiment)

    def test_a_new_experiment_decision_is_not_executed_by_the_service(self):
        # Without a launch from Nexus the service does not start one itself.
        self.orchestrator.run.return_value = TaskDecision(
            action=ACTION_NEW_EXPERIMENT, session_id="session-1"
        )

        self.service.handle_source(task_source("Implement the requested feature", source_id="task-1"))

        self.orchestrator.experiment_service.create_experiment.assert_not_called()
        self.github.ensure_for_source.assert_not_called()
        self.orchestrator.task_store.wait_for_experiment.assert_not_called()

    def test_a_started_launch_is_recorded_and_returns_the_decision(self):
        decision = TaskDecision(
            action=ACTION_NEW_EXPERIMENT,
            session_id="session-1",
            experiment_id="exp-263",
            experiment_launch=ExperimentLaunch(status=LAUNCH_SUBMITTED, experiment_id="exp-263"),
        )
        self.orchestrator.run.return_value = decision

        result = self.service.handle_source(task_source("Implement it", source_id="task-1"))

        self.assertIs(result, decision)
        kinds = [c.kwargs["kind"] for c in self.orchestrator.task_store.append_event.call_args_list]
        self.assertIn("experiment_started", kinds)

    def test_a_launch_that_did_not_start_is_returned_to_the_caller(self):
        for launch in (
            ExperimentLaunch(status=LAUNCH_ALREADY_ACTIVE, experiment_id="exp-1",
                             experiment_status="running", reason="already active"),
            ExperimentLaunch(status=LAUNCH_BLOCKED, reason="Configure GITHUB_TOKEN/GH_TOKEN"),
        ):
            with self.subTest(status=launch.status):
                self.orchestrator.run.return_value = TaskDecision(
                    action=ACTION_NEW_EXPERIMENT, session_id="session-1", experiment_launch=launch
                )
                result = self.service.handle_source(task_source("Implement it", source_id="task-1"))
                self.assertEqual(result["status"], launch.status)
                self.assertEqual(result.get("reason"), launch.reason)

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

    def test_execution_goal_uses_full_original_input_and_followup(self):
        store = NexusTaskStore(str(Path(self.tmp.name) / "goals.db"))
        self.addCleanup(store.close)
        session = store.get_or_create(source_type="issue", source_id="406")
        original = "x" * 5000 + "\nUNIQUE-END-MARKER"
        followup = "y" * 3000 + "\nFOLLOWUP-END-MARKER"
        store.append_event(session.id, role="user", kind="input", content=original)
        store.append_event(session.id, role="user", kind="input", content=followup)
        self.orchestrator.task_store = store

        goal = self.service._execution_goal(session.id, "fallback")

        self.assertEqual(goal, f"{original}\n\nUser follow-up:\n{followup}")

    def test_execution_goal_cap_is_recorded(self):
        store = NexusTaskStore(str(Path(self.tmp.name) / "truncated.db"))
        self.addCleanup(store.close)
        session = store.get_or_create(source_type="issue", source_id="406-large")
        store.append_event(session.id, role="user", kind="input", content="z" * 25000)
        self.orchestrator.task_store = store

        goal = self.service._execution_goal(session.id, "fallback")

        self.assertEqual(len(goal), 24000)
        records = store.recent_events(session.id, role="tool", kind="goal_truncated")
        self.assertEqual(len(records), 1)
        self.assertIn("25000", records[0]["content"])

    def test_reply_resumes_same_persistent_session(self):
        self.orchestrator.continue_session.return_value = TaskDecision(
            action="answer",
            session_id="session-1",
        )

        self.service.reply("session-1", "Continue")

        self.orchestrator.continue_session.assert_called_once_with("session-1", "Continue")


if __name__ == "__main__":
    unittest.main()
