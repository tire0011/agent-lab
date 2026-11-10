"""#263 step 2: new_experiment carries what Nexus already found.

The solve-issue-238 runs on Nexus gathered the right root cause in a
knowledge briefing and then either changed code itself or stopped; an
experiment would have started from the bare task text.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules.experiment_handover import experiment_handover, with_handover
from modules.task_orchestrator import (
    ACTION_NEW_EXPERIMENT,
    NexusTaskOrchestrator,
)
from tests.test_issue_111_controller_protocol import _Client, _Delegation, _Registry

BRIEFING = (
    "GitHub issue #238: NexusMemory._search_task_events selects every row of "
    "nexus_task_events with no filter on kind."
)


class HandoverTextTests(unittest.TestCase):
    def test_goal_briefing_findings_and_attempts_are_handed_over(self):
        handover = experiment_handover(
            "Filter controller bookkeeping out of _search_task_events",
            [
                {"tool": "knowledge_collector", "knowledge_briefing": BRIEFING},
                {"tool": "delegate", "result": {"content": "Add a kind filter."}},
                {"tool": "command", "command": "grep -n kind modules/nexus_memory.py",
                 "result": "**Exit code:** 0"},
            ],
        )
        self.assertIn("Nexus goal (a summary; it never narrows the task above):\nFilter controller bookkeeping", handover)
        self.assertIn(BRIEFING, handover)
        # A delegate's analysis is a model's opinion, not evidence.
        self.assertIn("Delegate findings (unverified):\nAdd a kind filter.", handover)
        self.assertIn("- command grep -n kind modules/nexus_memory.py", handover)

    def test_a_secret_crossing_a_clip_boundary_is_not_kept_in_part(self):
        # Review on #265: clipping first left a prefix that no longer matched
        # the configured secret value, so part of it was persisted.
        secret = "s3cr3t-value-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        with patch.dict(os.environ, {"AGENT_API_KEY": secret}):
            handover = experiment_handover(
                None,
                [{"tool": "command", "command": "x" * 280 + secret, "result": "ok"}],
            )
        self.assertNotIn(secret[:12], handover)
        self.assertIn("[REDACTED]", handover)

    def test_nothing_found_hands_over_nothing(self):
        self.assertEqual(experiment_handover(None, []), "")
        self.assertEqual(with_handover("Fix it", ""), "Fix it")

    def test_the_handover_follows_the_task(self):
        self.assertEqual(with_handover("Fix it", "found X"), "Fix it\n\nfound X")


class OrchestratorHandoverTests(unittest.TestCase):
    def test_new_experiment_decision_carries_the_handover(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = _Registry(str(Path(tmp) / "state.db"), tmp)
            client = _Client([
                '{"action":"command","command":"echo checked","reason":"look"}',
                # Nested, as the capability list shows it (#257).
                '{"action":"new_experiment","arguments":{"goal":"add a kind filter"},'
                '"reason":"implement"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=MagicMock(),
                llama_client=client,
                delegation_service=_Delegation(),
            )
            try:
                result = orchestrator.run(
                    "solve github issue #238", source_type="task", source_id="issue-263"
                )
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_NEW_EXPERIMENT)
        self.assertIn("Nexus goal (a summary; it never narrows the task above):\nadd a kind filter", result.experiment_handover)
        self.assertIn("- command echo checked", result.experiment_handover)
        self.assertIn('"name":"new_experiment"', client.prompts[0])
        self.assertIn("the concrete change to make and why", client.prompts[0])


if __name__ == "__main__":
    unittest.main()
