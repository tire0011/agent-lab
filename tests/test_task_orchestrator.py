from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.task_orchestrator import (
    ACTION_MISSING_CAPABILITY,
    ACTION_RESUME_EXPERIMENT,
    NexusTaskOrchestrator,
)


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def complete(self, *, prompt, temperature, max_tokens):
        self.prompts.append(prompt)
        return self.responses.pop(0)


class _Spec:
    name = "experiment_show"

    def to_dict(self):
        return {
            "operation": "experiment_show",
            "description": "show experiment",
            "arguments": [{"name": "experiment_id", "type": "string", "required": True}],
        }


class _Registry:
    def __init__(self):
        self.calls = []

    def catalog(self):
        return [_Spec()]

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, args, caller))
        return SimpleNamespace(
            to_dict=lambda: {
                "operation": operation,
                "success": True,
                "output_lines": ["id: bd9720b26ec74f15a2693e5641adf141", "status: verifying"],
                "duration_ms": 1,
                "exit_code": 0,
                "error_summary": None,
            }
        )


class TaskOrchestratorTests(unittest.TestCase):
    def test_llm_can_inspect_then_resume_existing_experiment(self):
        experiment = SimpleNamespace(id="bd9720b26ec74f15a2693e5641adf141")
        service = MagicMock()
        service.resolve_experiment.return_value = experiment
        registry = _Registry()
        client = _Client([
            '{"type":"tool","tool":"operation","operation":"experiment_show",'
            '"args":{"experiment_id":"bd9720b2"},"reason":"inspect current state"}',
            '{"type":"action","action":"resume_experiment","experiment_id":"bd9720b2",'
            '"command":null,"message":"","reason":"existing experiment is verifying"}',
        ])

        result = NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=service,
            llama_client=client,
        ).run("Resume experiment bd9720b2 and finish verification")

        self.assertEqual(result.action, ACTION_RESUME_EXPERIMENT)
        self.assertEqual(result.experiment_id, experiment.id)
        self.assertEqual(registry.calls[0][0], "experiment_show")
        self.assertIn("status: verifying", client.prompts[1])

    def test_missing_capability_is_valid_final_decision(self):
        service = MagicMock()
        registry = _Registry()
        client = _Client([
            '{"type":"action","action":"missing_capability","experiment_id":null,'
            '"command":null,"message":"I need a calendar capability.",'
            '"reason":"no available capability can access calendars"}'
        ])

        result = NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=service,
            llama_client=client,
        ).run("Move my meeting to tomorrow")

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertIn("calendar", result.message)
        self.assertEqual(registry.calls, [])


if __name__ == "__main__":
    unittest.main()
