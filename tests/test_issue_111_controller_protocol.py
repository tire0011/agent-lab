from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.task_orchestrator import ACTION_ANSWER, NexusTaskOrchestrator, TaskOrchestratorError


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.response_formats = []

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        self.prompts.append(prompt)
        self.response_formats.append(response_format)
        return self.responses.pop(0)


class _Spec:
    name = "git_status"
    description = "Git status of the repository"
    tags = ["git", "status", "repository", "changes"]
    arguments = []
    # Declared, like the real OperationSpec: an undeclared spec is
    # treated as mutating and would not be discoverable at all.
    mutates_state = False

    def to_dict(self):
        return {
            "operation": self.name,
            "description": self.description,
            "arguments": [],
            "tags": self.tags,
            "mutates_state": self.mutates_state,
        }


class _Registry:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.calls = []

    def catalog(self):
        return [_Spec()]

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, args, caller))
        return SimpleNamespace(to_dict=lambda: {
            "operation": operation,
            "success": True,
            "output_lines": ["clean"],
            "duration_ms": 1,
            "exit_code": 0,
            "error_summary": None,
        })


class _Delegation:
    def catalog(self):
        return []


class Issue111ControllerProtocolTests(unittest.TestCase):
    def make_orchestrator(self, tmp, responses):
        registry = _Registry(str(Path(tmp) / "state.db"), tmp)
        orchestrator = NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=MagicMock(),
            llama_client=_Client(responses),
            delegation_service=_Delegation(),
        )
        return orchestrator, registry, orchestrator.llama

    def test_schema_and_prompt_use_action_only_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _registry, client = self.make_orchestrator(
                tmp, ['{"action":"answer","message":"done","reason":"done"}']
            )
            try:
                result = orchestrator.run("answer")
            finally:
                orchestrator.close()
        self.assertEqual(result.action, ACTION_ANSWER)
        # Nested where llama-server and Azure actually read it (#218); a
        # schema at the top level is silently ignored by both.
        schema = client.response_formats[0]["json_schema"]["schema"]
        self.assertEqual(schema["required"], ["action"])
        self.assertNotIn("type", schema["properties"])
        self.assertIn("call_tool", schema["properties"]["action"]["enum"])
        self.assertIn('{"action":"find_tool"', client.prompts[0])
        self.assertIn('{"action":"call_tool"', client.prompts[0])

    def test_missing_find_tool_goal_is_corrected_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _registry, client = self.make_orchestrator(tmp, [
                '{"action":"find_tool","reason":"need tool"}',
                '{"action":"answer","message":"corrected","reason":"fallback"}',
            ])
            try:
                result = orchestrator.run("inspect")
            finally:
                orchestrator.close()
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertIn("find_tool requires goal", client.prompts[1])

    def test_find_tool_then_call_tool_executes_resolved_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, registry, client = self.make_orchestrator(tmp, [
                '{"action":"find_tool","goal":"inspect repository git status","reason":"discover"}',
                '{"status":"found","operation":"git_status","reason":"direct match","related_tools":[]}',
                '{"action":"call_tool","tool":"git_status","arguments":{},"reason":"invoke resolved tool"}',
                '{"action":"answer","message":"clean","reason":"observed"}',
            ])
            try:
                result = orchestrator.run("inspect git status")
            finally:
                orchestrator.close()
        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(registry.calls[0][0], "git_status")
        self.assertIn('"resolved_tool":"git_status"', client.prompts[2])
        # The protocol still requires a grant before an operation runs - the
        # execution above is what proves it. The controller prompt shows the
        # result instead of the grant that authorised it (#223).
        self.assertIn('"status":"succeeded"', client.prompts[3])
        self.assertNotIn('"grant_event_id"', client.prompts[3])

    def test_call_tool_requires_arguments(self):
        with self.assertRaisesRegex(TaskOrchestratorError, "call_tool requires arguments"):
            NexusTaskOrchestrator._parse('{"action":"call_tool","tool":"git_status"}')

    def test_unresolved_call_tool_is_rejected_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, registry, client = self.make_orchestrator(tmp, [
                '{"action":"call_tool","tool":"git_status","arguments":{},"reason":"guess"}',
                '{"action":"missing_capability","message":"not granted","reason":"operation_not_granted"}',
            ])
            try:
                result = orchestrator.run("inspect git status")
            finally:
                orchestrator.close()
        self.assertEqual(registry.calls, [])
        self.assertIn('"error":"operation_not_granted"', client.prompts[1])


if __name__ == "__main__":
    unittest.main()
