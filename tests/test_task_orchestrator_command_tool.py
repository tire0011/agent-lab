from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from modules.task_orchestrator import ACTION_ANSWER, NexusTaskOrchestrator, TaskOrchestratorError


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def complete(
        self,
        *,
        prompt,
        temperature,
        max_tokens,
        response_format=None,
    ):
        del response_format
        self.prompts.append(prompt)
        return self.responses.pop(0)


class _Registry:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)

    def catalog(self):
        return []


class _Delegation:
    def catalog(self):
        return []


class TaskOrchestratorCommandToolTests(unittest.TestCase):
    def test_malformed_command_action_is_dispatched_as_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _Client([
                '{"type":"action","action":"command","command":"printf recovered",'
                '"reason":"inspect value"}',
                '{"type":"action","action":"answer","experiment_id":null,'
                '"message":"done","reason":"command succeeded"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=_Registry(str(Path(tmp) / "state.db"), tmp),
                experiment_service=MagicMock(),
                llama_client=client,
                delegation_service=_Delegation(),
            )
            try:
                result = orchestrator.run("Run the command and report the value")
            finally:
                orchestrator.close()

            self.assertEqual(result.action, ACTION_ANSWER)
            self.assertEqual(result.observations[0]["tool"], "command")
            self.assertIn("recovered", result.observations[0]["result"])

    def test_malformed_find_tool_action_is_dispatched_as_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _Client([
                '{"type":"action","action":"find_tool","goal":"inspect repository status",'
                '"reason":"need a capability"}',
                '{"status":"not_found","reason":"no matching operation","related_tools":[]}',
                '{"type":"action","action":"answer","experiment_id":null,'
                '"message":"No matching operation exists.","reason":"resolver finished"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=_Registry(str(Path(tmp) / "state.db"), tmp),
                experiment_service=MagicMock(),
                llama_client=client,
                delegation_service=_Delegation(),
            )
            try:
                result = orchestrator.run("Inspect repository status")
            finally:
                orchestrator.close()

            self.assertEqual(result.action, ACTION_ANSWER)
            self.assertEqual(result.observations[0]["tool"], "find_tool")
            self.assertEqual(result.observations[0]["status"], "not_found")

    def test_unknown_action_remains_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _Client([
                '{"type":"action","action":"not_a_tool","message":"bad",'
                '"reason":"unknown"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=_Registry(str(Path(tmp) / "state.db"), tmp),
                experiment_service=MagicMock(),
                llama_client=client,
                delegation_service=_Delegation(),
            )
            try:
                with self.assertRaisesRegex(TaskOrchestratorError, "unknown final action 'not_a_tool'"):
                    orchestrator.run("Reject the unknown action")
            finally:
                orchestrator.close()

    def test_command_result_returns_to_controller_and_allows_next_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _Client([
                '{"type":"tool","tool":"command","command":"printf first",'
                '"reason":"inspect first value"}',
                '{"type":"tool","tool":"command","command":"printf second",'
                '"reason":"inspect second value"}',
                '{"type":"action","action":"answer","experiment_id":null,'
                '"message":"Both commands succeeded.","reason":"enough evidence"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=_Registry(str(Path(tmp) / "state.db"), tmp),
                experiment_service=MagicMock(),
                llama_client=client,
                delegation_service=_Delegation(),
            )
            try:
                result = orchestrator.run("Inspect two values and report the result")
            finally:
                orchestrator.close()

            self.assertEqual(result.action, ACTION_ANSWER)
            self.assertEqual(len(result.observations), 2)
            self.assertEqual([item["tool"] for item in result.observations], ["command", "command"])
            self.assertIn("first", result.observations[0]["result"])
            self.assertIn("second", result.observations[1]["result"])
            self.assertIn('"tool":"command"', client.prompts[1])
            self.assertIn("first", client.prompts[1])
            self.assertIn("second", client.prompts[2])

    def test_command_is_advertised_as_tool_not_final_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _Client([
                '{"type":"action","action":"answer","experiment_id":null,'
                '"message":"done","reason":"done"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=_Registry(str(Path(tmp) / "state.db"), tmp),
                experiment_service=MagicMock(),
                llama_client=client,
                delegation_service=_Delegation(),
            )
            try:
                orchestrator.run("Answer directly")
            finally:
                orchestrator.close()

            prompt = client.prompts[0]
            self.assertIn('"name":"command"', prompt)
            self.assertNotIn('{"type":"tool"', prompt)


if __name__ == "__main__":
    unittest.main()
