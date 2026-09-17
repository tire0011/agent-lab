from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        self.prompts.append(prompt)
        return self.responses.pop(0)


class _Spec:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.arguments = []
        self.tags = ["git", "repository", "lifecycle"]
        # Declared, like the real OperationSpec: an undeclared spec is
        # treated as mutating and would not be discoverable at all.
        self.mutates_state = False

    def to_dict(self):
        return {
            "operation": self.name,
            "description": self.description,
            "arguments": [],
            "tags": list(self.tags),
            "mutates_state": self.mutates_state,
        }


class _ExperimentService:
    """Resolves only ids that exist, the way the real service does.

    A MagicMock resolves anything, which is exactly what hid the bug this file
    now covers: every hex-looking token looked like a real experiment.
    """

    def __init__(self, known: dict[str, object] | None = None) -> None:
        self.known = dict(known or {})
        self.calls: list[str] = []

    def resolve_experiment(self, experiment_id: str):
        self.calls.append(experiment_id)
        if experiment_id not in self.known:
            raise KeyError(f"unknown experiment {experiment_id!r}")
        return self.known[experiment_id]


class _Registry:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.calls = []
        self._specs = [
            _Spec("git_status", "Show repository status"),
            _Spec("git_log", "Show repository history"),
            _Spec("git_worktrees", "Show repository worktrees"),
        ]

    def catalog(self):
        return list(self._specs)

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, args, caller))
        return SimpleNamespace(to_dict=lambda: {"operation": operation, "success": True, "output_lines": []})


class Issue164FindToolNotFoundTests(unittest.TestCase):
    def test_identical_not_found_lookup_redirects_to_known_resume_capability(self):
        experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        with tempfile.TemporaryDirectory() as tmp:
            registry = _Registry(str(Path(tmp) / "state.db"), tmp)
            service = _ExperimentService(
                {experiment_id: SimpleNamespace(id=experiment_id)}
            )
            goal = "inspect repository lifecycle state"
            client = _Client([
                '{"action":"find_tool","goal":"' + goal + '","reason":"inspect lifecycle"}',
                '{"status":"not_found","operation":"","reason":"no exact lifecycle tool",'
                '"related_tools":["git_status","git_log","git_worktrees"]}',
                '{"action":"find_tool","goal":"' + goal + '","reason":"try the same lookup again"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=service,
                llama_client=client,
            )
            try:
                result = orchestrator.run(
                    f"Resume/continue existing experiment {experiment_id}",
                    source_type="task",
                    source_id="issue-164-regression",
                )
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_RESUME_EXPERIMENT)
        self.assertEqual(result.experiment_id, experiment_id)
        self.assertEqual(len(result.observations), 1)
        observation = result.observations[0]
        self.assertEqual(observation["status"], "not_found")
        self.assertFalse(observation["retryable"])
        self.assertTrue(observation["state_token"])
        self.assertEqual(
            [item["operation"] for item in observation["related_operations"]],
            ["git_status", "git_log", "git_worktrees"],
        )
        self.assertIn(ACTION_RESUME_EXPERIMENT, observation["direct_capabilities"])
        self.assertEqual(observation["recommended_next_action"], ACTION_RESUME_EXPERIMENT)
        self.assertNotIn("tool_loop_detected", str(result.observations))
        # Resolved twice on purpose: once to establish that the id is a real
        # experiment before offering the capability, once on the final action.
        self.assertEqual(service.calls, [experiment_id, experiment_id])
        self.assertEqual(observation["resume_experiment_id"], experiment_id)
        self.assertEqual(registry.calls, [])

    def _orchestrator(self, tmp: str, *, service, responses=()):
        return NexusTaskOrchestrator(
            operations_registry=_Registry(str(Path(tmp) / "state.db"), tmp),
            experiment_service=service,
            llama_client=_Client(list(responses)),
        )

    def test_resume_capability_requires_unambiguous_resume_intent(self):
        experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        service = _ExperimentService({experiment_id: SimpleNamespace(id=experiment_id)})
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._orchestrator(tmp, service=service)
            try:
                metadata = orchestrator._find_tool_not_found_metadata(
                    f"Inspect experiment {experiment_id}",
                    "inspect repository lifecycle state",
                    ["git_status"],
                )
            finally:
                orchestrator.close()

        self.assertEqual(metadata["direct_capabilities"], [])
        self.assertNotIn("resume_experiment_id", metadata)
        self.assertEqual(metadata["recommended_next_action"], "resolve_related_operation")

    def test_a_commit_sha_is_not_an_experiment_id(self):
        # "Looks like hex" matched commit shas and dates too, so an everyday
        # task raised `unknown experiment` instead of finishing.
        service = _ExperimentService()
        goal = "inspect repository lifecycle state"
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._orchestrator(
                tmp,
                service=service,
                responses=[
                    '{"action":"find_tool","goal":"' + goal + '","reason":"inspect"}',
                    '{"status":"not_found","operation":"","reason":"no lifecycle tool",'
                    '"related_tools":["git_status"]}',
                    '{"action":"find_tool","goal":"' + goal + '","reason":"try again"}',
                    # The repeat is redirected to the related operation, so the
                    # resolver is asked about git_status by name (#218).
                    '{"status":"found","operation":"git_status","reason":"named directly",'
                    '"related_tools":[]}',
                    '{"action":"answer","message":"reviewed","reason":"done"}',
                ],
            )
            try:
                result = orchestrator.run(
                    "Continue reviewing the regression introduced in commit 4f1b3f2a",
                    source_type="task",
                    source_id="issue-164-commit-sha",
                )
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(service.calls, ["4f1b3f2a"])
        not_found = result.observations[0]
        self.assertEqual(not_found["direct_capabilities"], [])
        self.assertNotIn("resume_experiment_id", not_found)

    def test_a_date_is_not_an_experiment_id(self):
        service = _ExperimentService()

        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._orchestrator(tmp, service=service)
            try:
                metadata = orchestrator._find_tool_not_found_metadata(
                    "Continue the release notes for 20260913",
                    "inspect repository lifecycle state",
                    [],
                )
            finally:
                orchestrator.close()

        self.assertEqual(metadata["direct_capabilities"], [])
        self.assertEqual(metadata["recommended_next_action"], ACTION_MISSING_CAPABILITY)

    def test_intent_comes_from_this_task_not_the_session_history(self):
        # An experiment id plus "resume" from an earlier, unrelated request must
        # not offer a direct capability for a task that is about something else.
        experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        service = _ExperimentService({experiment_id: SimpleNamespace(id=experiment_id)})
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._orchestrator(tmp, service=service)
            try:
                session = orchestrator.task_store.get_or_create(
                    source_type="task", source_id="issue-164-history"
                )
                orchestrator.task_store.append_event(
                    session.id,
                    role="user",
                    kind="input",
                    content=f"Resume experiment {experiment_id}",
                )
                metadata = orchestrator._find_tool_not_found_metadata(
                    "Summarise yesterday's mailbox traffic",
                    "inspect mailbox state",
                    [],
                )
            finally:
                orchestrator.close()

        self.assertEqual(metadata["direct_capabilities"], [])
        self.assertEqual(service.calls, [])

    def test_a_repeated_terminal_lookup_still_reaches_the_loop_guard(self):
        # The pre-emption used to `continue` before loop_guard.check, so repeats
        # were never counted and the guard could no longer stop the loop. The
        # #218 redirect must not reintroduce that: it narrows the first repeat
        # to the related operation, and once the narrowed lookup is itself
        # terminal there is nothing left to offer and the guard takes over.
        service = _ExperimentService()
        goal = "inspect repository lifecycle state"
        narrowed = "git_status: Show repository status"
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state.db")
            orchestrator = self._orchestrator(
                tmp,
                service=service,
                responses=[
                    '{"action":"find_tool","goal":"' + goal + '","reason":"inspect"}',
                    '{"status":"not_found","operation":"","reason":"no lifecycle tool",'
                    '"related_tools":["git_status"]}',
                    '{"action":"find_tool","goal":"' + goal + '","reason":"try again"}',
                    '{"status":"not_found","operation":"","reason":"not this one either",'
                    '"related_tools":["git_status"]}',
                    '{"action":"find_tool","goal":"' + narrowed + '","reason":"and again"}',
                    '{"action":"find_tool","goal":"' + narrowed + '","reason":"once more"}',
                ],
            )
            try:
                result = orchestrator.run(
                    "Inspect the repository lifecycle",
                    source_type="task",
                    source_id="issue-164-loop-guard",
                )
            finally:
                orchestrator.close()

            conn = sqlite3.connect(db_path)
            duplicates = conn.execute(
                "SELECT SUM(duplicate_calls) FROM nexus_orchestration_budgets"
            ).fetchone()[0]
            recorded = conn.execute(
                "SELECT COUNT(*) FROM nexus_tool_calls WHERE tool=?", ("find_tool",)
            ).fetchone()[0]
            conn.close()

        self.assertEqual(result.action, ACTION_CLARIFICATION)
        self.assertIn("tool loop limit reached", result.reason)
        self.assertGreaterEqual(int(duplicates or 0), 1)
        self.assertGreaterEqual(int(recorded or 0), 3)
        # The refused repeat still tells the controller what it can do instead.
        blocked = result.observations[-1]
        self.assertFalse(blocked["retryable"])
        self.assertEqual(
            [item["operation"] for item in blocked["related_operations"]],
            ["git_status"],
        )
        self.assertEqual(blocked["recommended_next_action"], "resolve_related_operation")

    def test_a_redirected_turn_is_recorded_as_the_controller_decided_it(self):
        experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        service = _ExperimentService({experiment_id: SimpleNamespace(id=experiment_id)})
        goal = "inspect repository lifecycle state"
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._orchestrator(
                tmp,
                service=service,
                responses=[
                    '{"action":"find_tool","goal":"' + goal + '","reason":"inspect"}',
                    '{"status":"not_found","operation":"","reason":"no lifecycle tool",'
                    '"related_tools":["git_status"]}',
                    '{"action":"find_tool","goal":"' + goal + '","reason":"try again"}',
                ],
            )
            try:
                result = orchestrator.run(
                    f"Resume/continue existing experiment {experiment_id}",
                    source_type="task",
                    source_id="issue-164-telemetry",
                )
                stats = orchestrator.task_store.controller_turn_stats()
                events = orchestrator.task_store.recent_events(
                    result.session_id, kind="find_tool_redirected"
                )
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_RESUME_EXPERIMENT)
        # The controller chose find_tool twice; the turn record must say so
        # rather than claiming a decision the controller never made.
        self.assertEqual(stats["by_outcome"], {"find_tool": 2})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["metadata"]["controller_action"], "find_tool")
        self.assertEqual(events[0]["metadata"]["experiment_id"], experiment_id)


if __name__ == "__main__":
    unittest.main()
