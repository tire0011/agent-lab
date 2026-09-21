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



def _command_result(exit_code: int, output: str) -> str:
    """Render a command result the way ``process_command`` really does.

    Fixtures used to invent ``exit_code: 0``, a format the runner never emits
    (`modules/mailbox/development.py` writes ``**Exit code:** 0``). The whole
    suite stayed green while the evidence parser could not match a single real
    result, so the shape under test has to come from the real renderer - see
    ``test_exit_code_parser_matches_the_real_command_runner``.
    """
    return "\n".join(
        [
            f"**Exit code:** {exit_code}",
            "**Duration:** 0.001s",
            "",
            "### stdout",
            "",
            "```text",
            output,
            "```",
        ]
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
                    return_value=(_command_result(0, "changed"), None),
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
                    return_value=(_command_result(1, "failed"), None),
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


    def test_verified_noop_can_finish_after_explicit_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _, _ = _orchestrator(
                tmp,
                [
                    '{"action":"command","command":"test -f existing.txt","effect":"verify",'
                    '"reason":"verify requested state already exists"}',
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Already implemented.","reason":"verified existing state",'
                    '"verified_noop":true}',
                ],
            )
            try:
                with patch(
                    "modules.task_orchestrator.process_command",
                    return_value=(_command_result(0, ""), None),
                ):
                    result = orchestrator.run("fix the repository behavior")
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertTrue(
            any(item.get("verification_evidence") for item in result.observations)
        )

    def test_unverified_already_done_claim_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _, _ = _orchestrator(
                tmp,
                [
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Already implemented.","reason":"I think it is done",'
                    '"verified_noop":true}',
                    '{"type":"action","action":"missing_capability","experiment_id":null,'
                    '"message":"No verification was performed.",'
                    '"reason":"verification evidence is missing"}',
                ],
            )
            try:
                result = orchestrator.run("fix the repository behavior")
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)



class Issue234EvidenceIntegrityTests(unittest.TestCase):
    """Fixes from the second review round: the gate has to work against the
    real command runner, and evidence must belong to the request in force."""

    def test_exit_code_parser_matches_the_real_command_runner(self):
        """The parser and the renderer must agree, or no evidence is ever seen.

        Runs the actual `process_command` rather than a fixture: the earlier
        parser matched an invented `exit_code: 0` shape, so every
        implementation task was silently rejected while the suite stayed green.
        """
        from modules.mailbox.development import process_command

        with tempfile.TemporaryDirectory() as tmp:
            rendered, error = process_command(
                SimpleNamespace(body="printf changed", meta={}), tmp, None, 0
            )

        self.assertIsNone(error)
        self.assertEqual(NexusTaskOrchestrator._command_exit_code(rendered), 0)

        with tempfile.TemporaryDirectory() as tmp:
            failed, _ = process_command(SimpleNamespace(body="exit 3", meta={}), tmp, None, 0)

        self.assertEqual(NexusTaskOrchestrator._command_exit_code(failed), 3)

    def test_new_request_in_the_same_session_does_not_inherit_evidence(self):
        """Evidence proves one request was done, not the next one."""
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _, _ = _orchestrator(
                tmp,
                [
                    '{"action":"command","command":"printf changed","effect":"change",'
                    '"reason":"apply the requested change"}',
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Implemented.","reason":"change completed"}',
                    '{"type":"action","action":"answer","experiment_id":null,'
                    '"message":"Already done.","reason":"reusing earlier work"}',
                    '{"type":"action","action":"missing_capability","experiment_id":null,'
                    '"message":"No state-changing action completed for this request.",'
                    '"reason":"completion evidence is still missing"}',
                ],
            )
            try:
                with patch(
                    "modules.task_orchestrator.process_command",
                    return_value=(_command_result(0, "changed"), None),
                ):
                    first = orchestrator.run("fix issue 196", source_id="stale-evidence")
                self.assertEqual(first.action, ACTION_ANSWER)
                second = orchestrator.continue_session(first.session_id, "fix issue 9")
                session = orchestrator.task_store.get(first.session_id)
            finally:
                orchestrator.close()

        self.assertEqual(second.action, ACTION_MISSING_CAPABILITY)
        self.assertTrue(session.requires_completion_evidence)

    def test_status_request_does_not_arm_the_gate(self):
        """"Update me on ..." asks for news, not for a change."""
        for task in (
            "please update me on issue 42",
            "status update on the mailbox work",
            "keep me posted on the fix",
        ):
            with self.subTest(task=task):
                self.assertFalse(NexusTaskOrchestrator._is_implementation_intent(task))

        for task in ("fix issue 42", "# Fix issue #196", "please update the retry limit"):
            with self.subTest(task=task):
                self.assertTrue(NexusTaskOrchestrator._is_implementation_intent(task))

    def test_effect_is_not_part_of_the_call_signature(self):
        """Relabelling a command must not buy it a second run, or evidence."""
        orchestrator = NexusTaskOrchestrator.__new__(NexusTaskOrchestrator)
        read_tool, read_args = NexusTaskOrchestrator._tool_identity(
            orchestrator, {"tool": "command", "command": "ls -la", "effect": "read"}
        )
        change_tool, change_args = NexusTaskOrchestrator._tool_identity(
            orchestrator, {"tool": "command", "command": "ls -la", "effect": "change"}
        )

        self.assertEqual(read_tool, change_tool)
        self.assertEqual(read_args, change_args)
        self.assertNotIn("effect", read_args)

    def test_evidence_survives_beyond_the_recent_event_window(self):
        """A long session must not lose its evidence and lock itself out."""
        from modules.nexus_task_store import NexusTaskStore

        with tempfile.TemporaryDirectory() as tmp:
            store = NexusTaskStore(db_path=str(Path(tmp) / "state.db"))
            session = store.get_or_create(
                source_type="task", source_id="long-session", title="long"
            )
            store.set_requires_completion_evidence(session.id, True, intent_key="key-1")
            store.record_completion_evidence(session.id, "key-1", "change")
            for index in range(120):
                store.append_event(
                    session.id,
                    role="tool",
                    kind="observation",
                    content=f'{{"step": {index}}}',
                )

            refreshed = store.get(session.id)

        self.assertTrue(
            NexusTaskOrchestrator._evidence_satisfies(refreshed, kind="change")
        )
        self.assertFalse(
            NexusTaskOrchestrator._evidence_satisfies(refreshed, kind="verify")
        )

    def test_requirement_is_written_only_after_the_claim_succeeds(self):
        """A lost claim must not mutate the session another worker owns."""
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, _, _ = _orchestrator(tmp, [])
            try:
                session = orchestrator.task_store.get_or_create(
                    source_type="task", source_id="claim-order", title="claim"
                )
                with patch.object(
                    orchestrator.task_store, "claim_session", return_value=False
                ):
                    with self.assertRaises(Exception):
                        orchestrator.run(
                            "fix the claim ordering", session_id=session.id
                        )
                refreshed = orchestrator.task_store.get(session.id)
            finally:
                orchestrator.close()

        self.assertFalse(refreshed.requires_completion_evidence)
        self.assertIsNone(refreshed.completion_intent_key)


if __name__ == "__main__":
    unittest.main()
