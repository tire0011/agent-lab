from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from modules.nexus_delegation import DelegationResult
from modules.nexus_task_store import NexusTaskStore
from modules.operations_registry import OperationArgSpec
from modules.runtime_execution import resolve_python_interpreter
from modules.central_logging import get_task_logger, read_log_entries
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_MISSING_CAPABILITY,
    ACTION_RESUME_EXPERIMENT,
    NexusTaskOrchestrator,
    TaskOrchestratorError,
)


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.response_formats = []

    async def complete(
        self,
        *,
        prompt,
        temperature,
        max_tokens,
        response_format=None,
    ):
        self.prompts.append(prompt)
        self.response_formats.append(response_format)
        return self.responses.pop(0)


class _FailingClient(_Client):
    async def complete(
        self,
        *,
        prompt,
        temperature,
        max_tokens,
        response_format=None,
    ):
        self.prompts.append(prompt)
        raise RuntimeError("llama-server unavailable")


class _Spec:
    name = "experiment_show"
    description = "show experiment"
    # The real OperationSpec derives to_dict() from this list, so the fake must
    # too: it used to declare no arguments while reporting a required one, and
    # a caller that reads the schema off the spec would have been told this
    # operation takes nothing.
    arguments = [OperationArgSpec("experiment_id", "string", "Experiment-ID.", required=True)]
    tags = ["experiment", "status"]
    # Declared, like the real OperationSpec: a spec that does not say whether
    # it mutates state is treated as mutating and is not discoverable at all.
    mutates_state = False

    def to_dict(self):
        return {
            "operation": "experiment_show",
            "description": "show experiment",
            "arguments": [argument.to_dict() for argument in self.arguments],
            "tags": list(self.tags),
            "mutates_state": self.mutates_state,
        }


class _Registry:
    def __init__(self, db_path: str, repo_root: str):
        self.calls = []
        self.db_path = db_path
        self.repo_root = Path(repo_root)

    def catalog(self):
        return [_Spec()]

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, args, caller))
        return SimpleNamespace(
            to_dict=lambda: {
                "operation": operation,
                "success": True,
                "output_lines": ["id: bd9720b26ec74f15a2693e5641adf141", "status: verifying"],
                "duration_ms": 1,
                "exit_code": 0,
                "error_summary": None,
            }
        )


class _NamedSpec:
    # mutates_state is keyword-only and required here for the same reason it is
    # on the real OperationSpec: a fake that could skip the declaration would
    # stop mirroring the contract the router actually filters on.
    def __init__(self, name, description, *, tags=None, arguments=None, mutates_state):
        self.name = name
        self.description = description
        self.tags = list(tags or [])
        self.arguments = list(arguments or [])
        self.mutates_state = mutates_state

    def to_dict(self):
        return {
            "operation": self.name,
            "description": self.description,
            "arguments": [argument.to_dict() for argument in self.arguments],
            "tags": list(self.tags),
            "mutates_state": self.mutates_state,
        }


class _MultiRegistry:
    def __init__(self, db_path: str, repo_root: str):
        self.calls = []
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self._specs = [
            _NamedSpec("experiment_show", "Sichere Kurzübersicht zu einem Experiment.", tags=["experiment", "status"], mutates_state=False),
            _NamedSpec("git_status", "Git-Status des Haupt-Repositories.", tags=["git", "status", "repository", "changes", "local changes"], mutates_state=False),
            _NamedSpec("inbox_refresh", "Genau einen kontrollierten Mailbox-Poll auslösen.", tags=["mailbox", "poll", "refresh"], mutates_state=True),
        ]

    def catalog(self):
        return list(self._specs)

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, args, caller))
        return SimpleNamespace(
            to_dict=lambda: {
                "operation": operation,
                "success": True,
                "output_lines": ["M README.md"],
                "duration_ms": 1,
                "exit_code": 0,
                "error_summary": None,
            }
        )


class _Memory:
    def __init__(self, results=None):
        self.results = results or []
        self.queries = []

    def search(self, query, *, limit=8):
        self.queries.append((query, limit))
        return self.results


class _Delegation:
    def __init__(self):
        self.calls = []

    def catalog(self):
        return [
            {
                "provider": "codex_readonly",
                "description": "read-only specialist",
                "writes_repo": False,
                "cost": "external",
            }
        ]

    async def delegate(self, provider, task, *, context=""):
        self.calls.append((provider, task, context))
        return DelegationResult(
            provider=provider,
            success=True,
            content="The failure is caused by timeout handling in verification.",
            metadata={"task_id": "codex-1"},
        )


class TaskOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "state.db")
        self.registry = _Registry(self.db_path, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _interpreter(self) -> str:
        return resolve_python_interpreter(self.registry.repo_root)

    def _orchestrator(self, client, *, service=None, memory=None, delegation=None):
        return NexusTaskOrchestrator(
            operations_registry=self.registry,
            experiment_service=service or MagicMock(),
            llama_client=client,
            memory=memory,
            delegation_service=delegation,
        )

    def test_llm_can_inspect_then_resume_existing_experiment(self):
        experiment = SimpleNamespace(id="bd9720b26ec74f15a2693e5641adf141")
        service = MagicMock()
        service.resolve_experiment.return_value = experiment
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"inspect experiment status",'
            '"reason":"need experiment status"}',
            '{"status":"found","operation":"experiment_show","reason":"experiment_show inspects experiments",'
            '"related_tools":[]}',
            '{"type":"tool","tool":"operation","operation":"experiment_show",'
            '"args":{"experiment_id":"bd9720b2"},"reason":"inspect current state"}',
            '{"type":"action","action":"resume_experiment","experiment_id":"bd9720b2",'
            '"command":null,"message":"","reason":"existing experiment is verifying"}',
        ])

        result = self._orchestrator(client, service=service).run(
            "Resume experiment bd9720b2 and finish verification",
            source_type="task",
            source_id="mailbox-17",
        )

        self.assertEqual(result.action, ACTION_RESUME_EXPERIMENT)
        self.assertEqual(result.experiment_id, experiment.id)
        self.assertEqual(self.registry.calls[0][0], "experiment_show")
        self.assertIn("status: verifying", client.prompts[-1])
        self.assertIsNotNone(result.session_id)

    def test_controller_inference_runs_through_persistent_job_service(self):
        client = _Client([
            '{"type":"action","action":"answer","experiment_id":null,"command":null,'
            '"message":"done","reason":"finished"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Answer locally", source_id="job-test")
            self.assertEqual(result.action, ACTION_ANSWER)
            row = orchestrator.llm_jobs._conn.execute(
                "SELECT status, session_id FROM local_llm_jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(row["session_id"], result.session_id)
        finally:
            orchestrator.close()

    def test_a_request_for_an_already_held_result_is_named_as_such(self):
        """The loop guard refuses every repeat; this names the one #223 is about.

        A retry of a call that *failed* is legitimate. Asking for a result
        already in hand is never progress, and the two were indistinguishable
        in the journal - both arrived as `duplicate_tool_call`.
        """
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"action":"find_tool","goal":"inspect whether the repository has local changes",'
            '"reason":"need git status"}',
            '{"status":"found","operation":"git_status","reason":"reports changes","related_tools":[]}',
            '{"action":"call_tool","tool":"git_status","arguments":{},'
            '"reason":"the result is already there but I will fetch it again"}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done","reason":"finished"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Inspect local repository changes")
            events = orchestrator.task_store.recent_events(
                result.session_id, kind="redundant_tool_request"
            )
        finally:
            orchestrator.close()

        self.assertEqual(len(events), 1, events)
        metadata = events[0]["metadata"]
        self.assertEqual(metadata["operation"], "git_status")
        self.assertIsNotNone(metadata["already_succeeded_in_event"])
        # The model's own words for it, which is what makes the record useful.
        self.assertIn("fetch it again", metadata["controller_reason"])

    def test_the_budget_is_logged_once_not_every_turn(self):
        """A dozen numbers that do not move bury the two that do.

        The budget plan carries host memory, the context window and the
        section split. None of it changes within a run, and repeating it on
        every turn pushed prompt_chars and omitted_observations - the values
        worth watching - into the noise.
        """
        from unittest.mock import patch

        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"action":"memory_search","query":"one","limit":3,"reason":"look"}',
            '{"action":"memory_search","query":"two","limit":3,"reason":"look again"}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done","reason":"enough"}',
        ])
        orchestrator = self._orchestrator(client)
        with patch("modules.task_orchestrator.logger") as log:
            try:
                orchestrator.run("Search memory twice")
            finally:
                orchestrator.close()

        lines = [str(c.args[0]) for c in log.info.call_args_list if c.args]
        budgets = [l for l in lines if "controller_prompt_budget" in l]
        prompts = [l for l in lines if l.startswith("controller_prompt session_id")]
        self.assertEqual(len(budgets), 1, "the unchanged budget goes out once")
        self.assertEqual(len(prompts), 3, "what changes is still logged per turn")

    def test_a_jittering_memory_reading_does_not_relog_the_budget(self):
        """MemAvailable moves on every read; the budget it produces does not.

        Comparing the whole plan would log every turn again while looking like
        it does not.
        """
        from unittest.mock import patch

        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"action":"memory_search","query":"one","limit":3,"reason":"look"}',
            '{"action":"memory_search","query":"two","limit":3,"reason":"look again"}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done","reason":"enough"}',
        ])
        orchestrator = self._orchestrator(client)
        readings = iter([8_000_000, 7_999_100, 8_000_300, 7_998_800])

        with patch("modules.task_orchestrator.read_mem_available_kb",
                   side_effect=lambda *a, **k: next(readings, 8_000_000)), \
             patch("modules.task_orchestrator.logger") as log:
            try:
                orchestrator.run("Search memory twice")
            finally:
                orchestrator.close()

        budgets = [
            str(c.args[0]) for c in log.info.call_args_list
            if c.args and "controller_prompt_budget" in str(c.args[0])
        ]
        self.assertEqual(len(budgets), 1, "a moving reading is not a moving budget")

    def test_a_finished_run_states_its_reuse_verdict(self):
        """Green must be observable, not only red.

        Without a line per run, a correct run is silent and indistinguishable
        from one nobody looked at - so the only way to know was to read the
        journal back with a script afterwards.
        """
        from unittest.mock import patch

        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"action":"find_tool","goal":"inspect whether the repository has local changes",'
            '"reason":"need git status"}',
            '{"status":"found","operation":"git_status","reason":"reports changes","related_tools":[]}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done","reason":"the result is already here"}',
        ])
        orchestrator = self._orchestrator(client)
        with patch("modules.task_orchestrator.logger") as log:
            try:
                orchestrator.run("Inspect local repository changes")
            finally:
                orchestrator.close()

        verdicts = [
            call for call in log.info.call_args_list
            if call.args and "controller_result_reuse" in str(call.args[0])
        ]
        self.assertEqual(len(verdicts), 1, "exactly one verdict per run")
        rendered = str(verdicts[0].args)
        self.assertIn("clean", rendered)
        self.assertIn("0", rendered)  # no redundant requests

    def test_a_retry_of_a_failed_call_is_not_called_redundant(self):
        """Nothing is in hand there, so asking again is a legitimate move."""
        registry = _MultiRegistry(self.db_path, self.tmp.name)
        self.registry = registry
        client = _Client([
            '{"action":"find_tool","goal":"inspect whether the repository has local changes",'
            '"reason":"need git status"}',
            '{"status":"found","operation":"git_status","reason":"reports changes","related_tools":[]}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done","reason":"finished"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Inspect local repository changes")
            events = orchestrator.task_store.recent_events(
                result.session_id, kind="redundant_tool_request"
            )
        finally:
            orchestrator.close()
        self.assertEqual(events, [])

    def test_a_lookup_that_already_ran_says_so_on_itself(self):
        """Otherwise it reads as an invitation to make the call it just made.

        Session e33be32780f747de8c6a85f0ba6af00d: find_tool resolved and
        executed github_issue, the observation said only `resolved_tool`, and
        the controller followed the documented "find_tool then call_tool" and
        called it. The guard refused a call that was never needed.
        """
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"inspect whether the repository has local changes",'
            '"reason":"need a git inspection capability"}',
            '{"status":"found","operation":"git_status","reason":"git_status reports repository changes",'
            '"related_tools":[]}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"Repository inspected.","reason":"the result is already here"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Inspect local repository changes")
        finally:
            orchestrator.close()

        lookups = [o for o in result.observations if o.get("tool") == "find_tool"]
        self.assertTrue(lookups, result.observations)
        lookup = lookups[0]
        self.assertTrue(lookup.get("already_executed"))
        self.assertIn("already run", lookup.get("next_step", ""))
        # And it names where the result is, so "read it below" is checkable.
        executed = [
            o for o in result.observations
            if o.get("tool") == "operation" and o.get("operation") == "git_status"
        ]
        self.assertEqual(lookup.get("result_in_event"), executed[0]["event_id"])

    def test_the_annotation_reaches_the_controller_prompt(self):
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"inspect whether the repository has local changes",'
            '"reason":"need a git inspection capability"}',
            '{"status":"found","operation":"git_status","reason":"git_status reports repository changes",'
            '"related_tools":[]}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done","reason":"the result is already here"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            orchestrator.run("Inspect local repository changes")
        finally:
            orchestrator.close()

        # The turn after the lookup is the one that used to call it again.
        section = client.prompts[2].partition("OBSERVATIONS FROM TOOLS USED IN THIS RUN:")[2]
        self.assertIn("already_executed", section)
        self.assertIn("already run", section)

    def test_every_controller_decision_is_journalled_with_its_reason(self):
        """Reading a session back has to answer "why", not only "what".

        `nexus_controller_turns` keeps a turn's outcome; the model's own
        stated reason was kept nowhere, so a finished session showed which
        tools ran and nothing about what the controller thought it was doing.
        """
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"action":"memory_search","query":"prior work","limit":3,'
            '"reason":"check what is already known"}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done","reason":"memory was enough"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Inspect prior work")
            events = orchestrator.task_store.recent_events(
                result.session_id, kind="controller_decision"
            )
        finally:
            orchestrator.close()

        recorded = sorted(
            ((e["metadata"] or {}).get("action"), e["content"]) for e in events
        )
        self.assertEqual(
            recorded,
            [("answer", "memory was enough"), ("memory_search", "check what is already known")],
        )
        query = next(
            (e["metadata"] or {}).get("query")
            for e in events
            if (e["metadata"] or {}).get("action") == "memory_search"
        )
        self.assertEqual(query, "prior work")

    def test_journalling_a_decision_never_breaks_the_turn(self):
        """Instrumentation must not be the reason a turn fails."""
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done","reason":"nothing to do"}',
        ])
        orchestrator = self._orchestrator(client)
        original = orchestrator.task_store.append_event

        def explode_on_decisions(session_id, *, role, kind, content, metadata=None):
            if kind == "controller_decision":
                raise RuntimeError("journal is on fire")
            return original(session_id, role=role, kind=kind, content=content, metadata=metadata)

        orchestrator.task_store.append_event = explode_on_decisions  # type: ignore[method-assign]
        try:
            result = orchestrator.run("Answer despite a broken journal")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.message, "done")

    def test_low_memory_budget_is_advisory_when_local_succeeds(self):
        client = _Client([
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done locally","reason":"local inference succeeded"}',
        ])
        delegation = MagicMock()
        delegation.catalog.return_value = [
            {"provider": "azure_openai", "description": "external", "writes_repo": False, "cost": "paid"}
        ]
        orchestrator = self._orchestrator(client, delegation=delegation)
        try:
            from unittest.mock import patch

            with patch("modules.task_orchestrator.compute_budget") as compute_budget_mock:
                from modules.prompt_budget import compute_budget as real_compute_budget

                def _low_memory(**kwargs):
                    plan = real_compute_budget(**kwargs)
                    plan.low_memory = True
                    plan.safe_local_budget_chars = 0
                    return plan

                compute_budget_mock.side_effect = _low_memory
                result = orchestrator.run("Answer despite advisory memory pressure")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.message, "done locally")
        self.assertEqual(len(client.prompts), 1)
        delegation.delegate.assert_not_called()

    def test_local_inference_failure_uses_external_fallback(self):
        client = _FailingClient([])
        delegation = MagicMock()
        delegation.catalog.return_value = [
            {"provider": "azure_openai", "description": "external", "writes_repo": False, "cost": "paid"}
        ]
        delegation.delegate = AsyncMock(return_value=DelegationResult(
            provider="azure_openai",
            success=True,
            content='{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done externally","reason":"local inference failed"}',
            metadata={},
        ))
        orchestrator = self._orchestrator(client, delegation=delegation)
        try:
            result = orchestrator.run("Answer after local failure")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.message, "done externally")
        delegation.delegate.assert_called_once()
        self.assertEqual(delegation.delegate.call_args.args[0], "azure_openai")

    def test_controller_prompt_exposes_find_tool_instead_of_full_operation_catalog(self):
        client = _Client([
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done","reason":"finished"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Answer locally")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertIn('"name":"find_tool"', client.prompts[0])
        self.assertIn('"action":"find_tool"', client.prompts[0])
        self.assertNotIn('"type":"tool"', client.prompts[0])
        self.assertNotIn('"operations":[', client.prompts[0])
        self.assertNotIn('"operation":"experiment_show"', client.prompts[0])

    def test_find_tool_resolves_single_schema_then_controller_executes_operation(self):
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"inspect whether the repository has local changes",'
            '"reason":"need a git inspection capability"}',
            '{"status":"found","operation":"git_status","reason":"git_status reports repository changes",'
            '"related_tools":[]}',
            '{"action":"call_tool","tool":"git_status","arguments":{},'
            '"reason":"use the operation returned by find_tool"}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"Repository has local changes.","reason":"git status result is sufficient"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Inspect local repository changes")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(self.registry.calls[0][0], "git_status")
        self.assertEqual(len(client.prompts), 4)
        self.assertIn("TOOL RESOLVER MODE", client.prompts[1])
        self.assertIn('"operation":"git_status"', client.prompts[1])
        self.assertLessEqual(client.prompts[1].count('"operation":'), 5)
        self.assertIn('"tool":"find_tool"', client.prompts[2])
        self.assertIn('"status":"found"', client.prompts[2])
        self.assertIn('"operation":"git_status"', client.prompts[2])
        # The controller is shown what the lookup answered, not what the
        # resolver measured doing it, and not the grant that authorised the
        # call (#223). All of it is still recorded - see the journal
        # assertions below.
        # Scoped to the observation section: `legacy_catalog_schema_chars`
        # also describes the find_tool capability itself in the catalog above,
        # which is a different field with the same name.
        for prompt in (client.prompts[2], client.prompts[3]):
            section = prompt.partition("OBSERVATIONS FROM TOOLS USED IN THIS RUN:\n")[2]
            for internal in (
                '"main_selected_schema_chars"',
                '"legacy_catalog_schema_chars"',
                '"selection_correctness"',
                '"grant_event_id"',
                '"operation_schema"',
            ):
                self.assertNotIn(internal, section)
        self.assertIn('"resolved_tool":"git_status"', client.prompts[2])
        self.assertIn('"status":"succeeded"', client.prompts[3])

    def test_guessed_operation_without_find_tool_grant_is_rejected(self):
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"type":"tool","tool":"git_status","args":{},'
            '"reason":"guess repository operation"}',
            '{"type":"action","action":"missing_capability","experiment_id":null,'
            '"message":"Operation was not granted.","reason":"operation_not_granted"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Inspect local repository changes")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertEqual(self.registry.calls, [])
        self.assertIn('"error":"operation_not_granted"', client.prompts[1])

    def test_find_tool_grant_survives_restart_for_same_session(self):
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client1 = _Client([
            '{"type":"tool","tool":"find_tool","goal":"inspect repository changes",'
            '"reason":"need git status"}',
            '{"status":"found","operation":"git_status","reason":"git_status reports changes",'
            '"related_tools":[]}',
            '{"type":"action","action":"clarification","experiment_id":null,'
            '"message":"Ready to inspect after restart.","reason":"pause after grant"}',
        ])
        orchestrator1 = self._orchestrator(client1)
        try:
            first = orchestrator1.run("Prepare to inspect repository changes", source_id="grant-restart")
        finally:
            orchestrator1.close()

        client2 = _Client([
            '{"type":"tool","tool":"operation","operation":"git_status","args":{},'
            '"reason":"use persisted grant"}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"Repository inspected.","reason":"git status result is sufficient"}',
        ])
        orchestrator2 = self._orchestrator(client2)
        try:
            second = orchestrator2.run(
                "Now inspect repository changes",
                source_id="grant-restart",
                session_id=first.session_id,
            )
        finally:
            orchestrator2.close()

        self.assertEqual(second.action, ACTION_ANSWER)
        self.assertEqual(self.registry.calls[0][0], "git_status")
        # The grant survived, which is what this test is about: the operation
        # ran in a second process without a fresh find_tool. It is proven by
        # the journal, where the grant id is recorded, rather than by the
        # controller prompt, which no longer carries execution metadata (#223).
        store = NexusTaskStore(db_path=self.db_path)
        try:
            events = store.recent_events(first.session_id, kind="operation")
        finally:
            store.close()
        persisted = [json.loads(event["content"]) for event in events]
        self.assertTrue(
            any(entry.get("grant_event_id") for entry in persisted),
            "the persisted operation event must still carry its grant",
        )
        self.assertNotIn('"grant_event_id"', client2.prompts[1])

    def test_find_tool_rejects_placeholder_goals_and_distinguishes_not_found(self):
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        orchestrator = self._orchestrator(_Client([]))
        try:
            invalid = asyncio.run(
                orchestrator._resolve_find_tool("plain-language capability needed", session_id="goal-check")
            )
            empty = asyncio.run(
                orchestrator._resolve_find_tool("   ", session_id="goal-check-empty")
            )
            not_found = asyncio.run(
                orchestrator._resolve_find_tool("inspect GPU utilization", session_id="goal-check-missing")
            )
        finally:
            orchestrator.close()

        self.assertEqual(invalid["status"], "invalid_goal")
        self.assertIn("placeholder", invalid["reason"])
        self.assertIn("Describe the operation you need", invalid["guidance"])
        self.assertEqual(empty["status"], "invalid_goal")
        self.assertEqual(not_found["status"], "not_found")
        self.assertEqual(not_found["related_tools"], [])

    def test_find_tool_reformulates_after_invalid_goal_then_succeeds(self):
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"plain-language capability needed",'
            '"reason":"I need a generic placeholder goal"}',
            '{"status":"invalid_goal","error":"The goal is a placeholder, not a concrete capability request.",'
            '"guidance":"Describe the operation you need, e.g. \'run a shell command in the repository\'.",'
            '"reason":"goal is placeholder"}',
            '{"type":"tool","tool":"find_tool","goal":"inspect whether the repository has local changes",'
            '"reason":"need a git inspection capability"}',
            '{"status":"found","operation":"git_status","reason":"git_status reports repository changes",'
            '"related_tools":[]}',
            '{"action":"call_tool","tool":"git_status","arguments":{},'
            '"reason":"use the operation returned by find_tool"}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"Repository inspected.","reason":"git status result is sufficient"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Inspect repository changes")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(self.registry.calls[0][0], "git_status")
        self.assertTrue(any("plain-language capability needed" in prompt for prompt in client.prompts))
        self.assertTrue(any("inspect whether the repository has local changes" in prompt for prompt in client.prompts))

    def test_repeated_invalid_goal_is_stopped_by_loop_protection(self):
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"plain-language capability needed",'
            '"reason":"generic placeholder"}',
            '{"status":"invalid_goal","error":"The goal is a placeholder, not a concrete capability request.",'
            '"guidance":"Describe the operation you need, e.g. \'run a shell command in the repository\'.",'
            '"reason":"goal is placeholder"}',
            '{"type":"tool","tool":"find_tool","goal":"plain-language capability needed",'
            '"reason":"repeat placeholder"}',
            '{"type":"action","action":"clarification","experiment_id":null,'
            '"message":"Need a concrete capability before retrying.",'
            '"reason":"non_progressing_cycle"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Inspect repository state")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_CLARIFICATION)
        self.assertIn(result.observations[-1]["event"], {"duplicate_tool_call", "non_progressing_cycle"})
        self.assertEqual(self.registry.calls, [])

    def test_find_tool_returns_not_found_without_implementing_missing_capability(self):
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"inspect GPU utilization",'
            '"reason":"need GPU metrics"}',
            '{"type":"action","action":"missing_capability","experiment_id":null,'
            '"message":"No available tool exposes GPU metrics.","reason":"find_tool returned not_found"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Check GPU utilization")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertEqual(self.registry.calls, [])
        self.assertEqual(len(client.prompts), 2)
        self.assertIn('"status":"not_found"', client.prompts[1])
        self.assertIn("No read-only OperationsRegistry candidate matched", client.prompts[1])

    def test_find_tool_does_not_expose_non_read_only_operations(self):
        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"trigger a mailbox refresh poll",'
            '"reason":"need mailbox refresh"}',
            '{"type":"action","action":"missing_capability","experiment_id":null,'
            '"message":"Mailbox refresh is not available as a read-only operation.",'
            '"reason":"find_tool returned not_found"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Refresh the mailbox")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertEqual(self.registry.calls, [])
        self.assertEqual(len(client.prompts), 2)
        self.assertIn('"status":"not_found"', client.prompts[1])
        self.assertNotIn("inbox_refresh", client.prompts[1])

    def test_final_route_reasoning_is_written_to_task_log(self):
        client = _Client([
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"done","reason":"existing state already answers the request"}',
        ])
        task_log = get_task_logger("mailbox-17", log_dir=self.tmp.name)
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run(
                "Answer locally",
                source_id="mailbox-17",
                task_logger=task_log,
            )
        finally:
            orchestrator.close()

        entries = read_log_entries(task_log.path)
        decision = next(entry for entry in entries if entry["event"] == "route_selected")
        self.assertEqual(decision["mailbox_task_id"], "mailbox-17")
        self.assertEqual(decision["action"], result.action)
        self.assertEqual(decision["reason"], "existing state already answers the request")

    def test_missing_final_action_is_corrected_once(self):
        client = _Client(['{\"type\":\"action\"}', '{\"type\":\"action\",\"action\":\"answer\",\"message\":\"done\",\"reason\":\"corrected\"}'])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run('Answer locally')
            self.assertEqual(result.action, ACTION_ANSWER)
            self.assertEqual(len(client.prompts), 2)
            self.assertIn('action response requires action', client.prompts[1])
        finally:
            orchestrator.close()

    def test_registered_operation_name_alias_without_grant_is_rejected(self):
        client = _Client([
            '{"type":"tool","tool":"experiment_show","args":{"experiment_id":"bd9720b2"},'
            '"reason":"inspect current state"}',
            '{"type":"action","action":"missing_capability","experiment_id":null,"command":null,'
            '"message":"Operation was not granted.","reason":"operation_not_granted"}',
        ])

        result = self._orchestrator(client).run("Inspect experiment bd9720b2")

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertEqual(self.registry.calls, [])
        self.assertIn('"tool":"operation"', client.prompts[1])
        self.assertIn('"operation":"experiment_show"', client.prompts[1])
        self.assertIn('"error":"operation_not_granted"', client.prompts[1])

    def test_command_action_envelope_is_normalized_to_tool(self):
        client = _Client([
            '{"type":"action","action":"command","command":"git status --short",'
            '"message":"","reason":"inspect repository"}',
            '{"type":"action","action":"answer","experiment_id":null,"command":null,'
            '"message":"Repository inspected.","reason":"command result is sufficient"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            from unittest.mock import patch
            with patch(
                "modules.task_orchestrator.process_command",
                return_value=("exit_code: 0\nstdout:\nclean\n", None),
            ) as command_mock:
                result = orchestrator.run("Inspect the repository")

            self.assertEqual(result.action, ACTION_ANSWER)
            command_mock.assert_called_once()
            self.assertIn('"tool":"command"', client.prompts[1])
            self.assertIn("clean", client.prompts[1])
        finally:
            orchestrator.close()

    def test_memory_is_a_tool_and_result_returns_to_controller(self):
        memory = _Memory([
            {
                "source": "project_document",
                "document": "DECISIONS.md",
                "content": "Use local model first and delegate expensive work selectively.",
            }
        ])
        client = _Client([
            '{"type":"tool","tool":"memory_search","query":"model delegation decision",'
            '"limit":5,"reason":"look for previous architecture decision"}',
            '{"type":"action","action":"answer","experiment_id":null,"command":null,'
            '"message":"We already decided to use local-first delegation.",'
            '"reason":"memory answered the question"}',
        ])

        result = self._orchestrator(client, memory=memory).run(
            "What did we decide about model delegation?",
            source_type="idea",
            source_id="idea-44",
        )

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(memory.queries, [("model delegation decision", 5)])
        self.assertIn("local model first", client.prompts[1])
        self.assertIn('"type":"idea"', client.prompts[0])

    def test_identical_memory_search_executes_only_once(self):
        memory = _Memory([{"content": "original result"}])
        client = _Client([
            '{"type":"tool","tool":"memory_search","query":"existing version",'
            '"limit":6,"reason":"find prior work"}',
            '{"type":"tool","tool":"memory_search","limit":6,'
            '"query":"existing version","reason":"find prior work again"}',
            '{"type":"action","action":"answer","message":"Stopped duplicate.",'
            '"reason":"duplicate observation requires another path"}',
        ])
        orchestrator = self._orchestrator(client, memory=memory)
        try:
            result = orchestrator.run("Find the existing version implementation")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(memory.queries, [("existing version", 6)])
        duplicate = result.observations[1]
        self.assertEqual(duplicate["event"], "duplicate_tool_call")
        self.assertNotIn("results", duplicate)

    def test_a_b_a_tool_cycle_is_detected(self):
        memory = _Memory([])
        client = _Client([
            '{"type":"tool","tool":"memory_search","query":"a","reason":"first"}',
            '{"type":"tool","tool":"memory_search","query":"b","reason":"second"}',
            '{"type":"tool","tool":"memory_search","query":"a","reason":"repeat first"}',
            '{"type":"action","action":"clarification","message":"Need changed state.",'
            '"reason":"cycle detected"}',
        ])
        orchestrator = self._orchestrator(client, memory=memory)
        try:
            result = orchestrator.run("Inspect memory")
        finally:
            orchestrator.close()

        self.assertEqual(memory.queries, [("a", 6), ("b", 6)])
        self.assertEqual(result.observations[2]["event"], "non_progressing_cycle")

    def test_failed_command_repeat_is_blocked_but_different_recovery_runs(self):
        client = _Client([
            '{"type":"tool","tool":"command","command":"cargo build",'
            '"reason":"build the project"}',
            '{"type":"tool","tool":"command","command":"cargo build",'
            '"reason":"retry failed command"}',
            '{"type":"tool","tool":"command","command":"make build",'
            '"reason":"use available build tool"}',
            '{"type":"action","action":"answer","message":"Recovered with make.",'
            '"reason":"different command succeeded"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            from unittest.mock import patch
            with patch(
                "modules.task_orchestrator.process_command",
                side_effect=[
                    ("exit_code: 127\nstderr:\ncargo: not found", None),
                    ("exit_code: 0\nstdout:\nbuilt", None),
                ],
            ) as command_mock:
                result = orchestrator.run("Build the project")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(command_mock.call_count, 2)
        self.assertEqual(result.observations[0]["event"], "command_not_found")
        self.assertEqual(result.observations[0]["missing_executable"], "cargo")
        self.assertEqual(result.observations[1]["event"], "duplicate_tool_call")
        self.assertIn("make", command_mock.call_args.args[0].body)

    def test_command_not_found_performs_one_deterministic_retry(self):
        client = _Client([
            '{"type":"tool","tool":"command","command":"python agent.py --version",'
            '"reason":"check version"}',
            '{"type":"action","action":"answer","message":"Version is 0.2.0.",'
            '"reason":"corrected command succeeded"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            from unittest.mock import patch
            with patch(
                "modules.task_orchestrator.process_command",
                side_effect=[
                    ("exit_code: 127\nstderr:\npython: not found", None),
                    ("exit_code: 0\nstdout:\n0.2.0", None),
                ],
            ) as command_mock:
                result = orchestrator.run("Print the Nexus version")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        # Exactly one corrected retry, and no further LLM inference for it.
        self.assertEqual(command_mock.call_count, 2)
        self.assertEqual(len(client.prompts), 2)
        observation = result.observations[0]
        self.assertEqual(observation["recovery"]["event"], "command_resolution_retry")
        self.assertEqual(observation["recovery"]["missing_executable"], "python")
        self.assertEqual(observation["recovery"]["original_command"], "python agent.py --version")
        self.assertEqual(observation["interpreter"], self._interpreter())
        self.assertEqual(observation["cwd"], str(self.registry.repo_root))
        retried = command_mock.call_args.args[0].body
        self.assertTrue(retried.startswith(self._interpreter()))
        self.assertIn("agent.py --version", retried)

    def test_python_run_uses_the_resolved_interpreter_and_preserves_arguments(self):
        client = _Client([
            '{"type":"tool","tool":"python_run","script":"agent.py","argv":["--version"],'
            '"reason":"check version deterministically"}',
            '{"type":"action","action":"answer","message":"Version is 0.2.0.",'
            '"reason":"version observed"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            from unittest.mock import patch
            with patch(
                "modules.task_orchestrator.process_command",
                return_value=("exit_code: 0\nstdout:\n0.2.0", None),
            ) as command_mock:
                result = orchestrator.run("Print the Nexus version")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(command_mock.call_count, 1)
        self.assertEqual(
            command_mock.call_args.args[0].body,
            f"{self._interpreter()} agent.py --version",
        )
        self.assertEqual(command_mock.call_args.args[1], self.registry.repo_root)
        observation = result.observations[0]
        self.assertEqual(observation["tool"], "python_run")
        self.assertEqual(observation["interpreter"], self._interpreter())
        self.assertEqual(observation["cwd"], str(self.registry.repo_root))
        self.assertNotIn(self._interpreter(), client.prompts[0].split("OBSERVATIONS")[0])

    def test_python_run_honours_a_repository_relative_working_directory(self):
        (self.registry.repo_root / "scripts").mkdir(parents=True, exist_ok=True)
        client = _Client([
            '{"type":"tool","tool":"python_run","module":"unittest","argv":["discover"],'
            '"cwd":"scripts","reason":"run tests from scripts"}',
            '{"type":"action","action":"answer","message":"Tests ran.","reason":"done"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            from unittest.mock import patch
            with patch(
                "modules.task_orchestrator.process_command",
                return_value=("exit_code: 0\nstdout:\nOK", None),
            ) as command_mock:
                result = orchestrator.run("Run the tests")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(
            command_mock.call_args.args[0].body,
            f"{self._interpreter()} -m unittest discover",
        )
        self.assertEqual(
            command_mock.call_args.args[1],
            (self.registry.repo_root / "scripts").resolve(),
        )

    def test_python_run_rejects_a_working_directory_outside_the_repository(self):
        client = _Client([
            '{"type":"tool","tool":"python_run","script":"agent.py","cwd":"../elsewhere",'
            '"reason":"escape"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            with self.assertRaises(TaskOrchestratorError):
                orchestrator.run("Run outside the repository")
        finally:
            orchestrator.close()

    def test_python_run_signature_distinguishes_working_directories(self):
        for name in ("scripts", "tools"):
            (self.registry.repo_root / name).mkdir(parents=True, exist_ok=True)
        client = _Client([
            '{"type":"tool","tool":"python_run","module":"unittest","argv":["discover"],'
            '"cwd":"scripts","reason":"run scripts tests"}',
            '{"type":"tool","tool":"python_run","module":"unittest","argv":["discover"],'
            '"cwd":"tools","reason":"run tools tests"}',
            '{"type":"action","action":"answer","message":"Both suites ran.","reason":"done"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            from unittest.mock import patch
            with patch(
                "modules.task_orchestrator.process_command",
                return_value=("exit_code: 0\nstdout:\nOK", None),
            ) as command_mock:
                result = orchestrator.run("Run both test suites")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        # A different working directory is a materially different call and must
        # not be rejected as a duplicate tool call.
        self.assertEqual(command_mock.call_count, 2)
        self.assertNotIn("event", result.observations[1])
        self.assertEqual(
            [str(call.args[1]) for call in command_mock.call_args_list],
            [
                str((self.registry.repo_root / "scripts").resolve()),
                str((self.registry.repo_root / "tools").resolve()),
            ],
        )

    def test_python_run_signature_still_blocks_an_identical_repeat(self):
        (self.registry.repo_root / "scripts").mkdir(parents=True, exist_ok=True)
        client = _Client([
            '{"type":"tool","tool":"python_run","module":"unittest","argv":["discover"],'
            '"cwd":"scripts","reason":"run tests"}',
            '{"type":"tool","tool":"python_run","module":"unittest","argv":["discover"],'
            '"cwd":"scripts","reason":"run the same tests again"}',
            '{"type":"action","action":"answer","message":"Tests ran.","reason":"done"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            from unittest.mock import patch
            with patch(
                "modules.task_orchestrator.process_command",
                return_value=("exit_code: 0\nstdout:\nOK", None),
            ) as command_mock:
                result = orchestrator.run("Run the tests")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(command_mock.call_count, 1)
        self.assertEqual(result.observations[1]["event"], "duplicate_tool_call")

    def test_delegate_is_tool_and_local_controller_decides_after_result(self):
        delegation = _Delegation()
        client = _Client([
            '{"type":"tool","tool":"delegate","provider":"codex_readonly",'
            '"task":"Inspect why verification is timing out","context":"experiment 123",'
            '"reason":"specialist repository analysis is useful"}',
            '{"type":"action","action":"answer","experiment_id":null,"command":null,'
            '"message":"The timeout handling is the likely cause.",'
            '"reason":"delegate evidence is sufficient"}',
        ])

        result = self._orchestrator(client, delegation=delegation).run(
            "Investigate the verification timeout",
            source_type="issue",
            source_id="46",
            title="Verification timeout",
        )

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(delegation.calls[0][0], "codex_readonly")
        self.assertIn("timeout handling", client.prompts[1])
        self.assertIn('"type":"issue"', client.prompts[0])

    def test_clarification_persists_and_same_session_can_continue(self):
        client = _Client([
            '{"type":"action","action":"clarification","experiment_id":null,'
            '"command":null,"message":"Should I optimize for cost or latency?",'
            '"reason":"the tradeoff materially changes the design"}',
            '{"type":"action","action":"answer","experiment_id":null,"command":null,'
            '"message":"I will optimize for cost.","reason":"the user answered the tradeoff"}',
        ])
        orchestrator = self._orchestrator(client)

        first = orchestrator.run(
            "Improve model routing",
            source_type="issue",
            source_id="52",
        )
        self.assertEqual(first.action, ACTION_CLARIFICATION)
        session = orchestrator.task_store.get(first.session_id)
        self.assertEqual(session.status, "waiting_for_user")

        second = orchestrator.continue_session(first.session_id, "Optimize for cost.")
        self.assertEqual(second.action, ACTION_ANSWER)
        self.assertEqual(second.session_id, first.session_id)
        self.assertIn("Optimize for cost", client.prompts[1])
        self.assertIn("Improve model routing", client.prompts[1])

    def test_knowledge_collector_is_not_automatic_preflight(self):
        from unittest.mock import patch

        client = _Client([
            '{"action":"answer","message":"No research needed.","reason":"task is already clear"}',
        ])
        orchestrator = self._orchestrator(client)
        collector = AsyncMock(return_value=[])
        try:
            with patch.dict("os.environ", {"NEXUS_KNOWLEDGE_COLLECTOR_ENABLED": "1"}), patch.object(
                orchestrator, "_run_knowledge_collector", collector
            ):
                result = orchestrator.run("Explain the current status")

            self.assertEqual(result.action, ACTION_ANSWER)
            collector.assert_not_awaited()
        finally:
            orchestrator.close()

    def test_controller_can_explicitly_invoke_knowledge_collector(self):
        from unittest.mock import patch

        client = _Client([
            '{"action":"knowledge_collector",'
            '"request":"Collect relevant facts about GitHub issue #238.",'
            '"reason":"issue context is needed before deciding what to do"}',
            '{"action":"answer","message":"I used the gathered facts.",'
            '"reason":"the briefing now answers the informational request"}',
        ])
        orchestrator = self._orchestrator(client)
        briefing = {
            "tool": "knowledge_collector",
            "knowledge_briefing": "Issue #238 concerns recursive memory_search indexing.",
            "source_count": 2,
        }
        collector = AsyncMock(return_value=briefing)
        try:
            with patch.object(orchestrator, "_run_knowledge_collector", collector):
                result = orchestrator.run("Tell me what is known about GitHub issue #238")

            self.assertEqual(result.action, ACTION_ANSWER)
            collector.assert_awaited_once()
            self.assertEqual(
                collector.await_args.args[1],
                "Collect relevant facts about GitHub issue #238.",
            )
            self.assertIn("Issue #238 concerns recursive memory_search indexing", client.prompts[-1])
            self.assertEqual(result.session_id, collector.await_args.args[0].id)
        finally:
            orchestrator.close()

    def test_main_prompt_is_general_agent_and_exposes_direct_local_work(self):
        client = _Client([
            '{"action":"answer","message":"done","reason":"no action required"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            orchestrator.run("Summarize this task")
            prompt = client.prompts[0]
            self.assertIn("You are Nexus, an autonomous agent.", prompt)
            self.assertNotIn("autonomous software developer", prompt)
            self.assertIn("knowledge_collector", prompt)
            self.assertIn("command", prompt)
            self.assertIn("python_run", prompt)
            self.assertIn("must not be discovered through find_tool", prompt)
        finally:
            orchestrator.close()

    def test_invalid_json_escape_gets_one_schema_constrained_correction(self):
        client = _Client([
            r'''{"type":"tool","tool":"command","command":"echo Don\'t stop","reason":"implement"}''',
            '{"type":"tool","tool":"command","command":"printf repaired",'
            '"reason":"corrected syntax"}',
            '{"type":"action","action":"answer","experiment_id":null,'
            '"message":"Implemented.","reason":"command succeeded"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            from unittest.mock import patch
            with patch(
                "modules.task_orchestrator.process_command",
                return_value=("exit_code: 0\nstdout:\nrepaired\n", None),
            ) as command_mock:
                # Read-only wording on purpose: this test is about repairing
                # one invalid JSON reply, and an implementation-intent task
                # would additionally have to satisfy the #234 completion gate,
                # which a mocked command that changes nothing cannot.
                result = orchestrator.run("Show the repaired command output")

            self.assertEqual(result.action, ACTION_ANSWER)
            command_mock.assert_called_once()
            self.assertIn("printf repaired", command_mock.call_args.args[0].body)
            self.assertIn("not valid JSON", client.prompts[1])
            self.assertEqual(len(client.prompts), 3)
            self.assertTrue(
                all(item and item["type"] == "json_schema" for item in client.response_formats)
            )
        finally:
            orchestrator.close()

    def test_invalid_json_after_single_correction_raises_clear_error(self):
        client = _Client([
            r'''{"type":"tool","tool":"command","command":"echo Don\'t stop"}''',
            "{still invalid}",
        ])
        orchestrator = self._orchestrator(client)
        try:
            with self.assertRaisesRegex(
                TaskOrchestratorError,
                "single correction retry also failed",
            ):
                orchestrator.run("Implement the change")
            self.assertEqual(len(client.prompts), 2)
        finally:
            orchestrator.close()

    def test_missing_capability_is_valid_final_decision(self):
        client = _Client([
            '{"type":"action","action":"missing_capability","experiment_id":null,'
            '"command":null,"message":"I need a calendar capability.",'
            '"reason":"no available capability can access calendars"}'
        ])

        result = self._orchestrator(client).run("Move my meeting to tomorrow")

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertIn("calendar", result.message)
        self.assertEqual(self.registry.calls, [])
        self.assertTrue(list((Path(self.tmp.name) / "state" / "requirements").glob("*.json")))

    def test_persisted_history_does_not_duplicate_this_runs_own_observations(self):
        """Regression test for unbounded prompt growth (agent-lab#91-style bug).

        Before the fix, every tool observation of the current run was embedded
        both via the fresh ``observations`` list AND via persisted history
        (since history() re-read events this same run had just appended),
        making the prompt grow roughly quadratically with step count.
        """
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"inspect experiment status",'
            '"reason":"need experiment inspection"}',
            '{"status":"found","operation":"experiment_show","reason":"experiment_show inspects experiments",'
            '"related_tools":[]}',
            '{"type":"tool","tool":"operation","operation":"experiment_show",'
            '"args":{"experiment_id":"marker-alpha-unique-token"},"reason":"first inspection"}',
            '{"type":"tool","tool":"operation","operation":"experiment_show",'
            '"args":{"experiment_id":"marker-beta-unique-token"},"reason":"second inspection"}',
            '{"type":"action","action":"answer","message":"done","reason":"done"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            orchestrator.run("Inspect twice then answer")
        finally:
            orchestrator.close()

        final_prompt = client.prompts[-1]
        self.assertEqual(final_prompt.count("marker-alpha-unique-token"), 1)
        self.assertEqual(final_prompt.count("marker-beta-unique-token"), 1)

    def test_nested_memory_search_results_are_deduped_against_prior_history(self):
        memory = _Memory([
            {
                "source": "task_history",
                "kind": "operation",
                "content": "Improve model routing",
            },
            {
                "source": "project_document",
                "document": "DECISIONS.md",
                "content": "Fresh unseen decision content.",
            },
        ])
        client = _Client([
            '{"type":"tool","tool":"memory_search","query":"model routing",'
            '"limit":6,"reason":"look for prior discussion"}',
            '{"type":"action","action":"answer","message":"done","reason":"done"}',
        ])
        orchestrator = self._orchestrator(client, memory=memory)
        try:
            orchestrator.run("Improve model routing")
        finally:
            orchestrator.close()

        final_prompt = client.prompts[-1]
        self.assertIn("Fresh unseen decision content.", final_prompt)
        # Without dedup this token would appear a third time (again inside the
        # memory_search observation), duplicating what CURRENT INPUT and the
        # persisted history section already show verbatim.
        self.assertEqual(final_prompt.count("Improve model routing"), 2)

    def test_oversized_observation_is_truncated_and_referenced_by_event_id(self):
        client = _Client([
            '{"type":"tool","tool":"command","command":"cat huge_file.txt",'
            '"reason":"inspect large output"}',
            '{"type":"action","action":"answer","message":"done","reason":"done"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            from unittest.mock import patch

            huge_output = "y" * 50000
            with patch(
                "modules.task_orchestrator.process_command",
                return_value=(f"exit_code: 0\nstdout:\n{huge_output}", None),
            ), patch(
                "modules.task_orchestrator.compute_budget"
            ) as compute_budget_mock:
                from modules.prompt_budget import compute_budget as real_compute_budget

                def _tiny_budget(**kwargs):
                    plan = real_compute_budget(**kwargs)
                    plan.observation_chars = 200
                    return plan

                compute_budget_mock.side_effect = _tiny_budget
                result = orchestrator.run("Read the huge file")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        final_prompt = client.prompts[-1]
        self.assertNotIn(huge_output, final_prompt)
        self.assertIn("event_id", final_prompt)

    def test_full_raw_observation_is_persisted_even_when_prompt_is_compacted(self):
        """A >10k tool result must remain fully retrievable by event_id (#91):

        only the prompt-facing view may be compacted/truncated, the durable
        copy stored via ``append_event``/``get_event`` must stay complete.
        """
        huge_line = "z" * 500
        registry = self.registry

        def _execute(operation, args, *, caller):
            registry.calls.append((operation, args, caller))
            return SimpleNamespace(
                to_dict=lambda: {
                    "operation": operation,
                    "success": True,
                    # 30 * 500 = 15000 chars, well over the old 10000-char cap.
                    "output_lines": [huge_line] * 30,
                    "duration_ms": 1,
                    "exit_code": 0,
                    "error_summary": None,
                }
            )

        registry.execute = _execute
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"inspect experiment output",'
            '"reason":"need experiment inspection"}',
            '{"status":"found","operation":"experiment_show","reason":"experiment_show inspects experiments",'
            '"related_tools":[]}',
            '{"type":"tool","tool":"operation","operation":"experiment_show",'
            '"args":{"experiment_id":"bd9720b26ec74f15a2693e5641adf141"},"reason":"inspect"}',
            '{"type":"action","action":"answer","message":"done","reason":"done"}',
        ])
        orchestrator = self._orchestrator(client)
        try:
            result = orchestrator.run("Inspect experiment output")
            self.assertEqual(result.action, ACTION_ANSWER)
            row = orchestrator.task_store._conn.execute(
                "SELECT id, content FROM nexus_task_events WHERE session_id=? AND kind='operation'"
                " ORDER BY id DESC LIMIT 1",
                (result.session_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            # The row already stored in the events table must be complete...
            self.assertGreater(len(row["content"]), 15000)
            self.assertIn(huge_line, row["content"])
            # ...and get_event() must return that same complete raw content.
            full = orchestrator.task_store.get_event(row["id"])
            self.assertEqual(full["content"], row["content"])
            self.assertIn(huge_line, full["content"])
        finally:
            orchestrator.close()

    def test_local_inference_failure_falls_back_to_external_provider(self):
        class _JsonDelegation:
            def __init__(self):
                self.calls = []

            def catalog(self):
                return [{"provider": "azure_openai", "description": "specialist", "writes_repo": False, "cost": "paid"}]

            async def delegate(self, provider, task, *, context=""):
                self.calls.append((provider, task, context))
                return DelegationResult(
                    provider=provider,
                    success=True,
                    content='{"type":"action","action":"answer","message":"handled externally",'
                    '"reason":"compact package delegated"}',
                    metadata={},
                )

        delegation = _JsonDelegation()
        client = _FailingClient([])
        orchestrator = self._orchestrator(client, delegation=delegation)
        try:
            from unittest.mock import patch

            with patch("modules.task_orchestrator.compute_budget") as compute_budget_mock:
                from modules.prompt_budget import compute_budget as real_compute_budget

                compute_budget_mock.side_effect = real_compute_budget
                result = orchestrator.run("Answer without risking a local OOM")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.message, "handled externally")
        self.assertEqual(delegation.calls[0][0], "azure_openai")
        self.assertEqual(len(client.prompts), 1)

    def test_find_tool_resolver_local_failure_uses_external_fallback(self):
        class _ResolverDelegation:
            def __init__(self):
                self.calls = []
                self.responses = [
                    '{"type":"tool","tool":"find_tool","goal":"inspect repository changes",'
                    '"reason":"need git status"}',
                    '{"status":"found","operation":"git_status","reason":"git_status reports changes",'
                    '"related_tools":[]}',
                    '{"type":"action","action":"answer","message":"resolved externally",'
                    '"reason":"resolver observation is sufficient"}',
                ]

            def catalog(self):
                return [{"provider": "azure_openai", "description": "specialist", "writes_repo": False, "cost": "paid"}]

            async def delegate(self, provider, task, *, context=""):
                self.calls.append((provider, task, context))
                return DelegationResult(
                    provider=provider,
                    success=True,
                    content=self.responses.pop(0),
                    metadata={},
                )

        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        delegation = _ResolverDelegation()
        client = _FailingClient([])
        orchestrator = self._orchestrator(client, delegation=delegation)
        try:
            from unittest.mock import patch

            with patch("modules.task_orchestrator.compute_budget") as compute_budget_mock:
                from modules.prompt_budget import compute_budget as real_compute_budget

                compute_budget_mock.side_effect = real_compute_budget
                result = orchestrator.run("Inspect repository changes without risking local OOM")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(len(client.prompts), 3)
        self.assertEqual(len(delegation.calls), 3)
        self.assertIn("persistent controller", delegation.calls[0][1])
        self.assertIn("tool resolver", delegation.calls[1][1])

    def test_find_tool_recomputes_budget_after_safe_controller_before_resolver(self):
        class _ResolverDelegation:
            def __init__(self):
                self.calls = []

            def catalog(self):
                return [{"provider": "azure_openai", "description": "specialist", "writes_repo": False, "cost": "paid"}]

            async def delegate(self, provider, task, *, context=""):
                self.calls.append((provider, task, context))
                return DelegationResult(
                    provider=provider,
                    success=True,
                    content='{"status":"found","operation":"git_status","reason":"git_status reports changes"}',
                    metadata={},
                )

        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        delegation = _ResolverDelegation()
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"inspect repository changes",'
            '"reason":"need git status"}',
            '{"type":"action","action":"answer","message":"resolver used external fallback",'
            '"reason":"resolver observation is sufficient"}',
            '{"type":"action","action":"answer","message":"resolver result observed",'
            '"reason":"resolver result was returned"}',
        ])
        orchestrator = self._orchestrator(client, delegation=delegation)
        original_submit = orchestrator.llm_jobs.submit
        orchestrator.llm_jobs.submit = MagicMock(side_effect=original_submit)
        try:
            from unittest.mock import patch

            with patch("modules.task_orchestrator.compute_budget") as compute_budget_mock:
                from modules.prompt_budget import compute_budget as real_compute_budget

                call_count = 0

                def _safe_controller_then_unsafe_resolver(**kwargs):
                    nonlocal call_count
                    call_count += 1
                    plan = real_compute_budget(**kwargs)
                    return plan

                compute_budget_mock.side_effect = _safe_controller_then_unsafe_resolver
                result = orchestrator.run("Inspect repository changes")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(orchestrator.llm_jobs.submit.call_count, 3)
        self.assertEqual(len(delegation.calls), 0)
        self.assertEqual(len(client.prompts), 3)

    def test_resolver_capacity_failure_returns_unavailable_not_not_found(self):
        class _EmptyDelegation:
            def catalog(self):
                return []

            async def delegate(self, provider, task, *, context=""):
                raise AssertionError("delegate should not be called with no providers")

        self.registry = _MultiRegistry(self.db_path, self.tmp.name)
        client = _Client([
            '{"type":"tool","tool":"find_tool","goal":"inspect repository changes",'
            '"reason":"need git status"}',
            '{"type":"action","action":"clarification","message":"Resolver capacity is unavailable.",'
            '"reason":"resolver unavailable"}',
        ])
        orchestrator = self._orchestrator(client, delegation=_EmptyDelegation())
        try:
            from unittest.mock import patch

            with patch("modules.task_orchestrator.compute_budget") as compute_budget_mock:
                from modules.prompt_budget import compute_budget as real_compute_budget

                call_count = 0

                def _safe_controller_then_unsafe_resolver(**kwargs):
                    nonlocal call_count
                    call_count += 1
                    plan = real_compute_budget(**kwargs)
                    return plan

                compute_budget_mock.side_effect = _safe_controller_then_unsafe_resolver
                result = orchestrator.run("Inspect repository changes")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)

    def test_local_correction_retry_stays_local(self):
        client = _Client([
            "not valid json at all",
            '{"type":"action","action":"answer","message":"corrected locally",'
            '"reason":"corrected locally"}',
        ])
        delegation = MagicMock()
        delegation.catalog.return_value = []
        orchestrator = self._orchestrator(client, delegation=delegation)
        try:
            from unittest.mock import patch

            with patch("modules.task_orchestrator.compute_budget") as compute_budget_mock:
                from modules.prompt_budget import compute_budget as real_compute_budget

                compute_budget_mock.side_effect = real_compute_budget
                result = orchestrator.run("Answer with a local correction retry")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.message, "corrected locally")
        self.assertEqual(len(client.prompts), 2)

    def test_budget_exceeded_with_no_external_provider_is_a_controlled_missing_capability(self):
        class _EmptyDelegation:
            def catalog(self):
                return []

            async def delegate(self, provider, task, *, context=""):
                raise AssertionError("delegate should not be called when no provider is available")

        client = _Client([])
        orchestrator = self._orchestrator(client, delegation=_EmptyDelegation())
        try:
            from unittest.mock import patch

            with patch("modules.task_orchestrator.compute_budget") as compute_budget_mock:
                from modules.prompt_budget import compute_budget as real_compute_budget

                def _forces_fallback(**kwargs):
                    plan = real_compute_budget(**kwargs)
                    plan.safe_local_budget_chars = 1
                    return plan

                compute_budget_mock.side_effect = _forces_fallback
                result = orchestrator.run("Answer without risking a local OOM")

            self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
            session = orchestrator.task_store.get(result.session_id)
            self.assertEqual(session.status, "blocked_missing_capability")
        finally:
            orchestrator.close()


if __name__ == "__main__":
    unittest.main()
