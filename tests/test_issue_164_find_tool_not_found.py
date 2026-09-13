from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.task_orchestrator import ACTION_RESUME_EXPERIMENT, NexusTaskOrchestrator


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        self.prompts.append(prompt)
        return self.responses.pop(0)


class _Spec:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.arguments = []
        self.tags = ["git", "repository", "lifecycle"]

    def to_dict(self):
        return {
            "operation": self.name,
            "description": self.description,
            "arguments": [],
            "tags": list(self.tags),
        }


class _Registry:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.calls = []
        self._specs = [
            _Spec("git_status", "Show repository status"),
            _Spec("git_log", "Show repository history"),
            _Spec("git_worktrees", "Show repository worktrees"),
        ]

    def catalog(self):
        return list(self._specs)

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, args, caller))
        return SimpleNamespace(to_dict=lambda: {"operation": operation, "success": True, "output_lines": []})


class Issue164FindToolNotFoundTests(unittest.TestCase):
    def test_identical_not_found_lookup_redirects_to_known_resume_capability(self):
        experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        with tempfile.TemporaryDirectory() as tmp:
            registry = _Registry(str(Path(tmp) / "state.db"), tmp)
            service = MagicMock()
            service.resolve_experiment.return_value = SimpleNamespace(id=experiment_id)
            goal = "inspect repository lifecycle state"
            client = _Client([
                '{"action":"find_tool","goal":"' + goal + '","reason":"inspect lifecycle"}',
                '{"status":"not_found","operation":"","reason":"no exact lifecycle tool",'
                '"related_tools":["git_status","git_log","git_worktrees"]}',
                '{"action":"find_tool","goal":"' + goal + '","reason":"try the same lookup again"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=service,
                llama_client=client,
            )
            try:
                result = orchestrator.run(
                    f"Resume/continue existing experiment {experiment_id}",
                    source_type="task",
                    source_id="issue-164-regression",
                )
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_RESUME_EXPERIMENT)
        self.assertEqual(result.experiment_id, experiment_id)
        self.assertEqual(len(result.observations), 1)
        observation = result.observations[0]
        self.assertEqual(observation["status"], "not_found")
        self.assertFalse(observation["retryable"])
        self.assertTrue(observation["state_token"])
        self.assertEqual(
            observation["related_operations"],
            ["git_status", "git_log", "git_worktrees"],
        )
        self.assertIn(ACTION_RESUME_EXPERIMENT, observation["direct_capabilities"])
        self.assertEqual(observation["recommended_next_action"], ACTION_RESUME_EXPERIMENT)
        self.assertNotIn("tool_loop_detected", str(result.observations))
        service.resolve_experiment.assert_called_once_with(experiment_id)
        self.assertEqual(registry.calls, [])

    def test_resume_capability_requires_unambiguous_resume_intent(self):
        experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        with tempfile.TemporaryDirectory() as tmp:
            registry = _Registry(str(Path(tmp) / "state.db"), tmp)
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=MagicMock(),
                llama_client=_Client([]),
            )
            try:
                session = orchestrator.task_store.get_or_create(
                    source_type="task",
                    source_id="issue-164-safety",
                )
                orchestrator.task_store.append_event(
                    session.id,
                    role="user",
                    kind="input",
                    content=f"Inspect experiment {experiment_id}",
                )
                metadata = orchestrator._find_tool_not_found_metadata(
                    session.id,
                    "inspect repository lifecycle state",
                    ["git_status"],
                )
            finally:
                orchestrator.close()

        self.assertEqual(metadata["direct_capabilities"], [])
        self.assertEqual(metadata["recommended_next_action"], "resolve_related_operation")


if __name__ == "__main__":
    unittest.main()
