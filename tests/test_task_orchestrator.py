from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.nexus_delegation import DelegationResult
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
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
    def __init__(self, db_path: str, repo_root: str):
        self.calls = []
        self.db_path = db_path
        self.repo_root = Path(repo_root)

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


class _Memory:
    def __init__(self, results=None):
        self.results = results or []
        self.queries = []

    def search(self, query, *, limit=8):
        self.queries.append((query, limit))
        return self.results


class _Delegation:
    def __init__(self):
        self.calls = []

    def catalog(self):
        return [
            {
                "provider": "codex_readonly",
                "description": "read-only specialist",
                "writes_repo": False,
                "cost": "external",
            }
        ]

    async def delegate(self, provider, task, *, context=""):
        self.calls.append((provider, task, context))
        return DelegationResult(
            provider=provider,
            success=True,
            content="The failure is caused by timeout handling in verification.",
            metadata={"task_id": "codex-1"},
        )


class TaskOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "state.db")
        self.registry = _Registry(self.db_path, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _orchestrator(self, client, *, service=None, memory=None, delegation=None):
        return NexusTaskOrchestrator(
            operations_registry=self.registry,
            experiment_service=service or MagicMock(),
            llama_client=client,
            memory=memory,
            delegation_service=delegation,
        )

    def test_llm_can_inspect_then_resume_existing_experiment(self):
        experiment = SimpleNamespace(id="bd9720b26ec74f15a2693e5641adf141")
        service = MagicMock()
        service.resolve_experiment.return_value = experiment
        client = _Client([
            '{"type":"tool","tool":"operation","operation":"experiment_show",'
            '"args":{"experiment_id":"bd9720b2"},"reason":"inspect current state"}',
            '{"type":"action","action":"resume_experiment","experiment_id":"bd9720b2",'
            '"command":null,"message":"","reason":"existing experiment is verifying"}',
        ])

        result = self._orchestrator(client, service=service).run(
            "Resume experiment bd9720b2 and finish verification",
            source_type="task",
            source_id="mailbox-17",
        )

        self.assertEqual(result.action, ACTION_RESUME_EXPERIMENT)
        self.assertEqual(result.experiment_id, experiment.id)
        self.assertEqual(self.registry.calls[0][0], "experiment_show")
        self.assertIn("status: verifying", client.prompts[1])
        self.assertIsNotNone(result.session_id)

    def test_registered_operation_name_is_accepted_as_direct_tool_alias(self):
        client = _Client([
            '{"type":"tool","tool":"experiment_show","args":{"experiment_id":"bd9720b2"},'
            '"reason":"inspect current state"}',
            '{"type":"action","action":"answer","experiment_id":null,"command":null,'
            '"message":"Experiment inspected.","reason":"tool result is sufficient"}',
        ])

        result = self._orchestrator(client).run("Inspect experiment bd9720b2")

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(self.registry.calls[0][0], "experiment_show")
        self.assertEqual(self.registry.calls[0][1], {"experiment_id": "bd9720b2"})
        self.assertIn('"tool":"operation"', client.prompts[1])
        self.assertIn('"operation":"experiment_show"', client.prompts[1])

    def test_memory_is_a_tool_and_result_returns_to_controller(self):
        memory = _Memory([
            {
                "source": "project_document",
                "document": "DECISIONS.md",
                "content": "Use local model first and delegate expensive work selectively.",
            }
        ])
        client = _Client([
            '{"type":"tool","tool":"memory_search","query":"model delegation decision",'
            '"limit":5,"reason":"look for previous architecture decision"}',
            '{"type":"action","action":"answer","experiment_id":null,"command":null,'
            '"message":"We already decided to use local-first delegation.",'
            '"reason":"memory answered the question"}',
        ])

        result = self._orchestrator(client, memory=memory).run(
            "What did we decide about model delegation?",
            source_type="idea",
            source_id="idea-44",
        )

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(memory.queries, [("model delegation decision", 5)])
        self.assertIn("local model first", client.prompts[1])
        self.assertIn('"type":"idea"', client.prompts[0])

    def test_delegate_is_tool_and_local_controller_decides_after_result(self):
        delegation = _Delegation()
        client = _Client([
            '{"type":"tool","tool":"delegate","provider":"codex_readonly",'
            '"task":"Inspect why verification is timing out","context":"experiment 123",'
            '"reason":"specialist repository analysis is useful"}',
            '{"type":"action","action":"answer","experiment_id":null,"command":null,'
            '"message":"The timeout handling is the likely cause.",'
            '"reason":"delegate evidence is sufficient"}',
        ])

        result = self._orchestrator(client, delegation=delegation).run(
            "Investigate the verification timeout",
            source_type="issue",
            source_id="46",
            title="Verification timeout",
        )

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(delegation.calls[0][0], "codex_readonly")
        self.assertIn("timeout handling", client.prompts[1])
        self.assertIn('"type":"issue"', client.prompts[0])

    def test_clarification_persists_and_same_session_can_continue(self):
        client = _Client([
            '{"type":"action","action":"clarification","experiment_id":null,'
            '"command":null,"message":"Should I optimize for cost or latency?",'
            '"reason":"the tradeoff materially changes the design"}',
            '{"type":"action","action":"answer","experiment_id":null,"command":null,'
            '"message":"I will optimize for cost.","reason":"the user answered the tradeoff"}',
        ])
        orchestrator = self._orchestrator(client)

        first = orchestrator.run(
            "Improve model routing",
            source_type="issue",
            source_id="52",
        )
        self.assertEqual(first.action, ACTION_CLARIFICATION)
        session = orchestrator.task_store.get(first.session_id)
        self.assertEqual(session.status, "waiting_for_user")

        second = orchestrator.continue_session(first.session_id, "Optimize for cost.")
        self.assertEqual(second.action, ACTION_ANSWER)
        self.assertEqual(second.session_id, first.session_id)
        self.assertIn("Optimize for cost", client.prompts[1])
        self.assertIn("Improve model routing", client.prompts[1])

    def test_missing_capability_is_valid_final_decision(self):
        client = _Client([
            '{"type":"action","action":"missing_capability","experiment_id":null,'
            '"command":null,"message":"I need a calendar capability.",'
            '"reason":"no available capability can access calendars"}'
        ])

        result = self._orchestrator(client).run("Move my meeting to tomorrow")

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertIn("calendar", result.message)
        self.assertEqual(self.registry.calls, [])
        self.assertTrue(list((Path(self.tmp.name) / "state" / "requirements").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
