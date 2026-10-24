"""#330: the controller does not resume an experiment its task did not start.

On 2026-10-19 a refused ``machine_reboot`` named the unrelated experiment that
blocked it, the controller resumed that experiment, and Nexus merged a change
nobody had asked for (#324, reverted in #325).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.task_orchestrator import (
    ACTION_CLARIFICATION,
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
    NexusTaskOrchestrator,
)
from tests.test_issue_111_controller_protocol import _Client, _Delegation, _Registry

FOREIGN_ID = "c0a96e74d1b24f0e9a3c5b7d2e4f6a81"
OWN_ID = "9642bd56a1b24f0e9a3c5b7d2e4f6a82"


class _ExperimentService:
    """Resolves only ids that exist, the way the real service does."""

    def __init__(self, *ids: str) -> None:
        self.known = {experiment_id: SimpleNamespace(id=experiment_id) for experiment_id in ids}

    def resolve_experiment(self, experiment_id: str):
        for full_id, experiment in self.known.items():
            if full_id.startswith(experiment_id):
                return experiment
        raise KeyError(experiment_id)


class ForeignResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = _Registry(str(Path(self.tmp.name) / "state.db"), self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _orchestrator(self, client):
        return NexusTaskOrchestrator(
            operations_registry=self.registry,
            experiment_service=_ExperimentService(FOREIGN_ID, OWN_ID),
            llama_client=client,
            delegation_service=_Delegation(),
        )

    def test_resuming_an_experiment_the_session_did_not_start_is_rejected(self):
        client = _Client([
            '{"action":"resume_experiment","experiment_id":"' + FOREIGN_ID + '",'
            '"message":"","reason":"machine_reboot was refused because this experiment runs"}',
            '{"action":"clarification","message":"The reboot is blocked by a running '
            'experiment. Should I wait for it?",'
            '"gap_kind":"user_intent","why_human_required":"only the user may touch another experiment","reason":"blocked"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run(
                "Reboot the machine", source_type="task", source_id="330-foreign"
            )
            events = orchestrator.task_store.recent_events(
                result.session_id, kind="terminal_decision_rejected"
            )
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_CLARIFICATION)
        self.assertIsNone(result.experiment_id)
        rejection = result.observations[0]
        self.assertEqual(rejection["terminal_action"], ACTION_RESUME_EXPERIMENT)
        self.assertEqual(rejection["experiment_id"], FOREIGN_ID)
        self.assertFalse(rejection["retryable"])
        self.assertEqual(rejection["recommended_next_action"], ACTION_CLARIFICATION)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["metadata"]["experiment_id"], FOREIGN_ID)
        # The controller is told why, on the very next turn.
        self.assertIn("Do not resume it", client.prompts[1])

    def test_resuming_the_sessions_own_experiment_still_works(self):
        client = _Client([
            '{"action":"new_experiment","arguments":{"goal":"add a kind filter"},'
            '"reason":"implement"}',
            '{"action":"resume_experiment","experiment_id":"' + OWN_ID[:8] + '",'
            '"message":"","reason":"verification timed out; continue it"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            first = orchestrator.run(
                "solve github issue #238", source_type="task", source_id="330-own"
            )
            self.assertEqual(first.action, ACTION_NEW_EXPERIMENT)
            orchestrator.task_store.wait_for_experiment(first.session_id, OWN_ID)
            resumed = orchestrator.resume_after_experiment(
                OWN_ID,
                {"tool": "new_experiment", "experiment_id": OWN_ID,
                 "status": "failed", "pr_number": None, "reason": "verification timed out"},
            )
        finally:
            orchestrator.close()

        self.assertEqual([d.action for d in resumed], [ACTION_RESUME_EXPERIMENT])
        self.assertEqual(resumed[0].experiment_id, OWN_ID)

    def test_an_explicit_user_request_to_resume_is_still_honoured(self):
        client = _Client([
            '{"action":"resume_experiment","experiment_id":"' + FOREIGN_ID[:8] + '",'
            '"message":"","reason":"the user asked for it"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run(
                f"Resume experiment {FOREIGN_ID[:8]} and finish verification",
                source_type="task",
                source_id="330-explicit",
            )
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_RESUME_EXPERIMENT)
        self.assertEqual(result.experiment_id, FOREIGN_ID)

    def test_an_id_mentioned_without_resume_intent_does_not_authorise_it(self):
        client = _Client([
            '{"action":"resume_experiment","experiment_id":"' + FOREIGN_ID + '",'
            '"message":"","reason":"it blocks the reboot"}',
            '{"action":"clarification","message":"Wait for the blocking experiment?",'
            '"gap_kind":"user_intent","why_human_required":"only the user may touch another experiment","reason":"blocked"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run(
                f"Reboot the machine even though {FOREIGN_ID} is running",
                source_type="task",
                source_id="330-mention",
            )
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_CLARIFICATION)
        self.assertEqual(result.observations[0]["experiment_id"], FOREIGN_ID)

    def test_the_controller_prompt_says_to_report_the_block_instead(self):
        client = _Client([
            '{"action":"clarification","message":"Which machine?","reason":"ask"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            orchestrator.run("Reboot the machine", source_type="task", source_id="330-prompt")
        finally:
            orchestrator.close()

        rules = client.prompts[0].partition("RULES:")[2].partition("AVAILABLE CAPABILITIES")[0]
        self.assertIn("refused because of another experiment", rules)
        self.assertIn("Never resume that experiment", rules)


if __name__ == "__main__":
    unittest.main()
