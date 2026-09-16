from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_MISSING_CAPABILITY,
    NexusTaskOrchestrator,
)
from tests.test_task_orchestrator import _Client, _NamedSpec


class _ExternalRegistry:
    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self.db_path = str(root / "state.db")
        self.repo_root = root
        self.fail = fail
        self.calls = []
        self._specs = [
            _NamedSpec(
                "external_record",
                "Read current details for an external system record.",
                tags=["external", "system", "record", "details", "read"],
                mutates_state=False,
            ),
            _NamedSpec(
                "calendar_availability",
                "Read calendar availability and schedules.",
                tags=["calendar", "availability", "schedule"],
                mutates_state=False,
            ),
        ]

    def catalog(self):
        return list(self._specs)

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, args, caller))
        return SimpleNamespace(to_dict=lambda: {
            "operation": operation,
            "success": not self.fail,
            "output_lines": [] if self.fail else ["state: current"],
            "duration_ms": 1,
            "exit_code": None,
            "error_summary": "External API returned HTTP 503" if self.fail else None,
        })


class TerminalEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _orchestrator(self, responses, *, fail=False, max_steps=6):
        client = _Client(responses)
        orchestrator = NexusTaskOrchestrator(
            operations_registry=_ExternalRegistry(self.root, fail=fail),
            experiment_service=MagicMock(),
            llama_client=client,
            max_steps=max_steps,
        )
        self.addCleanup(orchestrator.close)
        return orchestrator, client

    def test_untried_external_fact_clarification_is_rejected(self):
        orchestrator, client = self._orchestrator([
            '{"action":"clarification","message":"Paste external record 184.",'
            '"reason":"I cannot read it","information_gap":"external system record 184 details",'
            '"gap_kind":"external_information","evidence_event_ids":[],'
            '"why_human_required":"the API is unavailable"}',
            '{"action":"answer","message":"I will inspect the discoverable capability first.",'
            '"reason":"the terminal decision was not supported"}',
        ])

        result = orchestrator.run("Analyze external system record 184")

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.observations[0]["event"], "terminal_decision_rejected")
        self.assertEqual(result.observations[0]["candidate_operations"], ["external_record"])
        self.assertIn("terminal_decision_rejected", client.prompts[1])

    def test_user_only_choice_allows_immediate_clarification(self):
        orchestrator, client = self._orchestrator([
            '{"action":"clarification","message":"Should I optimize for cost or latency?",'
            '"reason":"the preference changes the design","information_gap":"cost or latency preference",'
            '"gap_kind":"user_preference","evidence_event_ids":[],'
            '"why_human_required":"Only the user can choose the desired tradeoff."}',
        ])

        result = orchestrator.run("Improve this workflow")

        self.assertEqual(result.action, ACTION_CLARIFICATION)
        self.assertEqual(result.observations, [])
        self.assertEqual(len(client.prompts), 1)

    def test_terminal_find_tool_not_found_supports_missing_capability(self):
        orchestrator, _client = self._orchestrator([
            '{"action":"find_tool","goal":"read GPU temperature",'
            '"reason":"look for a hardware metrics capability"}',
            '{"status":"not_found","operation":"","reason":"no candidate",'
            '"related_tools":[]}',
            '{"action":"missing_capability","message":"GPU temperature is unavailable.",'
            '"reason":"find_tool returned terminal not_found",'
            '"information_gap":"read GPU temperature","gap_kind":"missing_capability",'
            '"evidence_event_ids":[],"why_human_required":""}',
        ])

        result = orchestrator.run("Read GPU temperature")

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertEqual(result.observations[0]["status"], "not_found")

    def test_failed_api_attempt_is_evidence_for_terminal_reasoning(self):
        orchestrator, _client = self._orchestrator([
            '{"action":"find_tool","goal":"read external system record",'
            '"reason":"inspect the external fact"}',
            '{"status":"found","operation":"external_record",'
            '"arguments":{},"reason":"the operation reads the record",'
            '"related_tools":[]}',
            '{"action":"clarification","message":"The external API failed; try later?",'
            '"reason":"the attempted API returned HTTP 503",'
            '"information_gap":"external system record details",'
            '"gap_kind":"external_information","evidence_event_ids":[],'
            '"why_human_required":"The observed source is currently unavailable."}',
        ], fail=True)

        result = orchestrator.run("Read the external system record")

        self.assertEqual(result.action, ACTION_CLARIFICATION)
        self.assertFalse(result.observations[-1]["result"]["success"])

    def test_unrelated_failure_does_not_support_a_terminal_claim(self):
        orchestrator, client = self._orchestrator([
            '{"action":"find_tool","goal":"read external system record",'
            '"reason":"inspect the external fact"}',
            '{"status":"found","operation":"external_record",'
            '"arguments":{},"reason":"the operation reads the record",'
            '"related_tools":[]}',
            '{"action":"missing_capability","message":"Calendar access is unavailable.",'
            '"reason":"a tool failed","information_gap":"calendar availability",'
            '"gap_kind":"missing_capability","evidence_event_ids":[],'
            '"why_human_required":""}',
            '{"action":"answer","message":"I must investigate calendar access separately.",'
            '"reason":"the prior failure concerned a different source"}',
        ], fail=True)

        result = orchestrator.run("Check the record, then determine calendar availability")

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.observations[-1]["event"], "terminal_decision_rejected")
        self.assertIn("terminal_decision_rejected", client.prompts[-1])

    def test_repeated_unsupported_terminal_decisions_hit_existing_bound(self):
        decision = (
            '{"action":"clarification","message":"Paste the external record.",'
            '"reason":"I did not inspect it","information_gap":"external system record details",'
            '"gap_kind":"external_information","evidence_event_ids":[],'
            '"why_human_required":"assumed unavailable"}'
        )
        orchestrator, _client = self._orchestrator([decision, decision], max_steps=2)

        result = orchestrator.run("Read the external system record")

        self.assertEqual(result.action, ACTION_CLARIFICATION)
        self.assertIn("bounded decision-step limit", result.reason)
        self.assertEqual(
            [item["event"] for item in result.observations],
            ["terminal_decision_rejected", "terminal_decision_rejected"],
        )


if __name__ == "__main__":
    unittest.main()
