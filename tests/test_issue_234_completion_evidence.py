from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.operations_registry import OperationArgSpec
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_MISSING_CAPABILITY,
    NexusTaskOrchestrator,
)


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        self.prompts.append(prompt)
        return self.responses.pop(0)


class _IssueSpec:
    name = "github_issue"
    description = "Read one GitHub issue."
    arguments = [
        OperationArgSpec("issue_number", "integer", "GitHub issue number.", required=True)
    ]
    tags = ["github", "issue", "read"]
    mutates_state = False

    def to_dict(self):
        return {
            "operation": self.name,
            "description": self.description,
            "arguments": [argument.to_dict() for argument in self.arguments],
            "tags": list(self.tags),
            "mutates_state": self.mutates_state,
        }


class _MutationSpec:
    name = "apply_fix"
    description = "Apply the requested repository fix."
    arguments = []
    tags = ["repository", "change"]
    mutates_state = True

    def to_dict(self):
        return {
            "operation": self.name,
            "description": self.description,
            "arguments": [],
            "tags": list(self.tags),
            "mutates_state": self.mutates_state,
        }


class _IssueRegistry:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.calls = []

    def catalog(self):
        return [_IssueSpec(), _MutationSpec()]

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, args, caller))
        return SimpleNamespace(
            to_dict=lambda: {
                "operation": operation,
                "success": True,
                "output_lines": [
                    "Issue #196",
                    "state: open",
                    "title: Fix controller completion behavior",
                ],
                "duration_ms": 1,
                "exit_code": 0,
                "error_summary": None,
            }
        )


class Issue234CompletionEvidenceTests(unittest.TestCase):
    def test_solve_issue_cannot_finish_after_read_only_issue_fetch(self):
        """Regression for #234 / recorded #196 failure.

        Reading an open work item is context, not completion evidence for a
        solve/fix/implement task. A premature terminal answer must therefore be
        rejected so the controller gets another turn.
        """
        with tempfile.TemporaryDirectory() as tmp:
            registry = _IssueRegistry(str(Path(tmp) / "state.db"), tmp)
            client = _Client(
                [
                    '{"type":"tool","tool":"find_tool","goal":"read github issue 196",'
                    '"reason":"need the issue details"}',
                    '{"status":"found","operation":"github_issue",'
                    '"reason":"github_issue reads an issue","related_tools":[]}',
                    '{"action":"call_tool","tool":"github_issue",'
                    '"arguments":{"issue_number":196},"reason":"read the issue"}',
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"The issue is already resolved. No further action is needed.",'
                    '"reason":"issue was inspected"}',
                    '{"type":"action","action":"missing_capability","experiment_id":null,'
                    '"message":"Implementation still requires a state-changing capability.",'
                    '"reason":"no completion evidence exists"}',
                ]
            )
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=MagicMock(),
                llama_client=client,
            )
            try:
                result = orchestrator.run("solve github issue #196")
            finally:
                orchestrator.close()

        self.assertEqual(len(registry.calls), 1)
        self.assertEqual(registry.calls[0][0], "github_issue")
        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertNotEqual(result.action, ACTION_ANSWER)
        self.assertEqual(len(client.prompts), 5)


    def test_read_only_summary_can_finish_after_issue_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = _IssueRegistry(str(Path(tmp) / "state.db"), tmp)
            client = _Client(
                [
                    '{"type":"tool","tool":"find_tool","goal":"read github issue 196",'
                    '"reason":"need the issue details"}',
                    '{"status":"found","operation":"github_issue",'
                    '"reason":"github_issue reads an issue","related_tools":[]}',
                    '{"action":"call_tool","tool":"github_issue",'
                    '"arguments":{"issue_number":196},"reason":"read the issue"}',
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Issue #196 is open.","reason":"summary complete"}',
                ]
            )
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=MagicMock(),
                llama_client=client,
            )
            try:
                result = orchestrator.run("summarize github issue #196")
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(len(registry.calls), 1)

    def test_implementation_can_finish_after_repository_state_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = _IssueRegistry(str(Path(tmp) / "state.db"), tmp)
            client = _Client(
                [
                    '{"action":"command","command":"echo changed > result.txt",'
                    '"reason":"apply the requested change"}',
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Implemented.","reason":"change completed"}',
                ]
            )
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=MagicMock(),
                llama_client=client,
            )
            try:
                with patch.object(
                    orchestrator,
                    "_repository_state_fingerprint",
                    side_effect=["before", "after"],
                ), patch(
                    "modules.task_orchestrator.process_command",
                    return_value=("exit_code: 0\nstdout:\n", None),
                ):
                    result = orchestrator.run("fix the repository behavior")
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        changed = [item for item in result.observations if item.get("state_changed")]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].get("tool"), "command")


if __name__ == "__main__":
    unittest.main()
