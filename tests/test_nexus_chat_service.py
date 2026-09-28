import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.github_work_tracking import GitHubWorkTrackingError
from modules.nexus_chat_service import NexusChatService, is_execution_request
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
    TaskDecision,
)


class FakeTaskStore:
    def __init__(self, history):
        self._history = history
        self.waiting = []

    def history(self, session_id, limit=18, max_chars=9000):
        return list(self._history)

    def wait_for_experiment(self, session_id, experiment_id):
        self.waiting.append((session_id, experiment_id))
        return True


class FakeOrchestrator:
    def __init__(self, *, decision, history, calls, allow_mutation, **kwargs):
        self.decision = decision
        self.task_store = FakeTaskStore(history)
        self.calls = calls
        self.calls.append(("constructed", allow_mutation))

    def run(self, text, **kwargs):
        self.calls.append(("run", text, kwargs))
        return self.decision

    def continue_session(self, session_id, text):
        self.calls.append(("continue", session_id, text))
        return self.decision

    def close(self):
        self.calls.append(("close",))


class NexusChatServiceTests(unittest.TestCase):
    def _service(self, decision, *, history=None, github_work_tracking=None):
        calls = []
        history = history or []

        def factory(**kwargs):
            return FakeOrchestrator(
                decision=decision,
                history=history,
                calls=calls,
                **kwargs,
            )

        experiments = MagicMock()
        github = github_work_tracking or MagicMock()
        github.ensure_for_source.return_value = SimpleNamespace(
            issue_number=42, branch_name="nexus/42-chat"
        )
        github.bind_experiment.side_effect = lambda ref, experiment_id: ref
        github.recover_experiment.return_value = SimpleNamespace(
            issue_number=42, branch_name="nexus/42-chat"
        )
        service = NexusChatService(
            operations_registry=MagicMock(),
            experiment_service=experiments,
            orchestrator_factory=factory,
            background_starter=lambda name, target: calls.append(("background", name)),
            resume_runner=MagicMock(),
            github_work_tracking=github,
        )
        return service, experiments, github, calls

    def test_discussion_turn_is_non_mutating(self):
        decision = TaskDecision(action=ACTION_ANSWER, message="Wir können A oder B machen.", session_id="chat-1")
        service, experiments, _, calls = self._service(decision)

        result = service.handle("Welche Variante würdest du nehmen?")

        self.assertEqual(result.session_id, "chat-1")
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertIn(("constructed", False), calls)
        experiments.create_experiment.assert_not_called()

    def test_follow_up_reuses_session(self):
        decision = TaskDecision(action=ACTION_ANSWER, message="Weiter.", session_id="chat-1")
        service, _, _, calls = self._service(decision)

        service.handle("Und warum?", session_id="chat-1")

        self.assertIn(("continue", "chat-1", "Und warum?"), calls)

    def test_explicit_execution_starts_new_experiment_with_conversation_goal(self):
        decision = TaskDecision(action=ACTION_NEW_EXPERIMENT, message="Ich setze es um.", session_id="chat-1")
        history = [
            {"role": "user", "content": "Wir wollen im Chat die bestehende Statusanzeige erweitern."},
            {"role": "assistant", "content": "Dann würde ich die bestehende UI wiederverwenden."},
            {"role": "user", "content": "Mach das."},
        ]
        service, experiments, github, calls = self._service(decision, history=history)
        experiments.create_experiment.return_value = SimpleNamespace(id="exp-123")

        result = service.handle("Mach das.", session_id="chat-1")

        goal = experiments.create_experiment.call_args.args[0]
        self.assertIn("bestehende Statusanzeige", goal)
        self.assertNotEqual(goal.strip(), "Mach das.")
        self.assertEqual(result.experiment_id, "exp-123")
        self.assertIn(("constructed", True), calls)
        self.assertIn(("background", "exp-123"), calls)
        github.ensure_for_source.assert_called_once()
        github.bind_experiment.assert_called_once()

    def test_missing_github_configuration_blocks_before_experiment_creation(self):
        decision = TaskDecision(
            action=ACTION_NEW_EXPERIMENT,
            message="Ich setze es um.",
            session_id="chat-1",
        )
        github = MagicMock()
        github.ensure_for_source.side_effect = GitHubWorkTrackingError(
            "Configure GITHUB_TOKEN/GH_TOKEN"
        )
        service, experiments, _, _ = self._service(
            decision, github_work_tracking=github
        )

        with self.assertRaises(GitHubWorkTrackingError):
            service.handle("Mach das.", session_id="chat-1")

        experiments.create_experiment.assert_not_called()

    def test_explicit_execution_resumes_selected_experiment(self):
        decision = TaskDecision(
            action=ACTION_RESUME_EXPERIMENT,
            message="Ich mache dort weiter.",
            experiment_id="exp-123",
            session_id="chat-1",
        )
        service, experiments, github, calls = self._service(decision)
        experiment = SimpleNamespace(id="exp-123", goal="Existing goal")
        experiments.resolve_experiment.return_value = experiment

        result = service.handle("Mach bei dem Experiment weiter.", session_id="chat-1")

        experiments.resolve_experiment.assert_called_once_with("exp-123")
        github.recover_experiment.assert_called_once_with(experiment)
        self.assertEqual(result.experiment_id, "exp-123")
        self.assertIn(("background", "exp-123"), calls)

    def test_execution_intent_is_conservative(self):
        self.assertTrue(is_execution_request("Mach das."))
        self.assertTrue(is_execution_request("Bitte implementiere die besprochene Änderung."))
        self.assertTrue(is_execution_request("Kannst du das bitte umsetzen?"))
        self.assertFalse(is_execution_request("Wie würdest du das implementieren?"))
        self.assertFalse(is_execution_request("Welche Lösung ist besser?"))


if __name__ == "__main__":
    unittest.main()
