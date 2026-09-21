from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.operations_registry import OperationArgSpec
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
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


def _orchestrator(tmp: str, responses):
    registry = _IssueRegistry(str(Path(tmp) / "state.db"), tmp)
    client = _Client(responses)
    return (
        NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=MagicMock(),
            llama_client=client,
        ),
        registry,
        client,
    )


class Issue234CompletionEvidenceTests(unittest.TestCase):
    def test_solve_issue_cannot_finish_after_read_only_issue_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, registry, client = _orchestrator(
                tmp,
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
                ],
            )
            try:
                result = orchestrator.run("solve github issue #196")
            finally:
                orchestrator.close()

        self.assertEqual(len(registry.calls), 1)
        self.assertEqual(registry.calls[0][0], "github_issue")
        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertEqual(len(client.prompts), 5)

    def test_read_only_summary_can_finish_after_issue_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, registry, _ = _orchestrator(
                tmp,
                [
                    '{"type":"tool","tool":"find_tool","goal":"read github issue 196",'
                    '"reason":"need the issue details"}',
                    '{"status":"found","operation":"github_issue",'
                    '"reason":"github_issue reads an issue","related_tools":[]}',
                    '{"action":"call_tool","tool":"github_issue",'
                    '"arguments":{"issue_number":196},"reason":"read the issue"}',
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Issue #196 is open.","reason":"summary complete"}',
                ],
            )
            try:
                result = orchestrator.run("summarize github issue #196")
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(len(registry.calls), 1)

    def test_successful_change_command_is_completion_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _, _ = _orchestrator(
                tmp,
                [
                    '{"action":"command","command":"printf changed","effect":"change",'
                    '"reason":"apply the requested change"}',
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Implemented.","reason":"change completed"}',
                ],
            )
            try:
                with patch(
                    "modules.task_orchestrator.process_command",
                    return_value=("exit_code: 0\nstdout:\nchanged\n", None),
                ):
                    result = orchestrator.run("fix the repository behavior")
                events = orchestrator.task_store.recent_events(
                    result.session_id, role="tool", limit=10
                )
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertTrue(any("completion_evidence" in event["content"] for event in events))

    def test_failed_change_command_is_not_completion_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _, _ = _orchestrator(
                tmp,
                [
                    '{"action":"command","command":"false","effect":"change",'
                    '"reason":"apply the requested change"}',
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Implemented.","reason":"command ran"}',
                    '{"type":"action","action":"missing_capability","experiment_id":null,'
                    '"message":"The requested change did not complete.",'
                    '"reason":"change command failed"}',
                ],
            )
            try:
                with patch(
                    "modules.task_orchestrator.process_command",
                    return_value=("exit_code: 1\nstderr:\nfailed\n", None),
                ):
                    result = orchestrator.run("implement the requested change")
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)

    def test_follow_up_turn_keeps_original_implementation_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _, _ = _orchestrator(
                tmp,
                [
                    '{"type":"action","action":"clarification","experiment_id":null,'
                    '"message":"Should I proceed?","reason":"need authorization",'
                    '"gap_kind":"user_authorization","information_gap":"permission to proceed",'
                    '"why_human_required":"Only the user can authorize this change."}',
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Already resolved.","reason":"nothing else needed"}',
                    '{"type":"action","action":"missing_capability","experiment_id":null,'
                    '"message":"No state-changing action completed.",'
                    '"reason":"completion evidence is still missing"}',
                ],
            )
            try:
                first = orchestrator.run("solve github issue #196", source_id="follow-up-234")
                self.assertEqual(first.action, ACTION_CLARIFICATION)
                second = orchestrator.continue_session(first.session_id, "yes, go ahead")
                session = orchestrator.task_store.get(first.session_id)
            finally:
                orchestrator.close()

        self.assertTrue(session.requires_completion_evidence)
        self.assertEqual(second.action, ACTION_MISSING_CAPABILITY)

    def test_realistic_mailbox_heading_sets_implementation_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _, _ = _orchestrator(
                tmp,
                [
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Done.","reason":"claimed complete"}',
                    '{"type":"action","action":"missing_capability","experiment_id":null,'
                    '"message":"No change was performed.","reason":"missing change evidence"}',
                ],
            )
            try:
                result = orchestrator.run(
                    "# Fix issue #196\n\nPlease implement the requested behavior."
                )
                session = orchestrator.task_store.get(result.session_id)
            finally:
                orchestrator.close()

        self.assertTrue(session.requires_completion_evidence)
        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)


if __name__ == "__main__":
    unittest.main()
