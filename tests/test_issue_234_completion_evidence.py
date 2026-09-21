from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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


class _IssueRegistry:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.calls = []

    def catalog(self):
        return [_IssueSpec()]

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
        self.assertEqual(len(client.prompts), 4)


if __name__ == "__main__":
    unittest.main()
