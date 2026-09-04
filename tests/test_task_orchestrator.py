from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.nexus_delegation import DelegationResult
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


class _Spec:
    name = "experiment_show"
    description = "show experiment"
    arguments = []
    tags = ["experiment", "status"]

    def to_dict(self):
        return {
            "operation": "experiment_show",
            "description": "show experiment",
            "arguments": [{"name": "experiment_id", "type": "string", "required": True}],
            "tags": list(self.tags),
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
    def __init__(self, name, description, *, tags=None, arguments=None):
        self.name = name
        self.description = description
        self.tags = list(tags or [])
        self.arguments = list(arguments or [])

    def to_dict(self):
        return {
            "operation": self.name,
            "description": self.description,
            "arguments": [argument.to_dict() for argument in self.arguments],
            "tags": list(self.tags),
        }


class _MultiRegistry:
    def __init__(self, db_path: str, repo_root: str):
        self.calls = []
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self._specs = [
            _NamedSpec("experiment_show", "Sichere Kurzübersicht zu einem Experiment.", tags=["experiment", "status"]),
            _NamedSpec("git_status", "Git-Status des Haupt-Repositories.", tags=["git", "status", "repository", "changes", "local changes"]),
            _NamedSpec("inbox_refresh", "Genau einen kontrollierten Mailbox-Poll auslösen.", tags=["mailbox", "poll", "refresh"]),
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
        self.assertIn('"main_selected_schema_chars"', client.prompts[2])
        self.assertIn('"legacy_catalog_schema_chars"', client.prompts[2])
        self.assertIn('"selection_correctness":"unknown"', client.prompts[2])
        self.assertIn('"grant_event_id"', client.prompts[3])

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
        self.assertIn('"grant_event_id"', client2.prompts[1])

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
                result = orchestrator.run("Implement the change")

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

    def test_budget_exceeded_falls_back_to_external_provider(self):
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
        client = _Client([])  # local llama-server must never be invoked
        orchestrator = self._orchestrator(client, delegation=delegation)
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
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.message, "handled externally")
        self.assertEqual(delegation.calls[0][0], "azure_openai")
        self.assertEqual(client.prompts, [])

    def test_find_tool_resolver_budget_exceeded_uses_external_fallback_without_local_submit(self):
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
        client = _Client([])  # local llama-server must never be invoked
        orchestrator = self._orchestrator(client, delegation=delegation)
        try:
            from unittest.mock import patch

            with patch("modules.task_orchestrator.compute_budget") as compute_budget_mock:
                from modules.prompt_budget import compute_budget as real_compute_budget

                def _forces_fallback(**kwargs):
                    plan = real_compute_budget(**kwargs)
                    plan.safe_local_budget_chars = 1
                    return plan

                compute_budget_mock.side_effect = _forces_fallback
                orchestrator.llm_jobs.submit = MagicMock(side_effect=AssertionError("local submit forbidden"))
                result = orchestrator.run("Inspect repository changes without risking local OOM")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(client.prompts, [])
        self.assertEqual(orchestrator.llm_jobs.submit.call_count, 0)
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
                    if call_count == 2:
                        plan.safe_local_budget_chars = 1
                    return plan

                compute_budget_mock.side_effect = _safe_controller_then_unsafe_resolver
                result = orchestrator.run("Inspect repository changes")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(orchestrator.llm_jobs.submit.call_count, 2)
        self.assertEqual(len(delegation.calls), 1)
        self.assertIn("tool resolver", delegation.calls[0][1])
        self.assertIn('"resolver_provider":"azure_openai"', client.prompts[1])

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
                    if call_count == 2:
                        plan.safe_local_budget_chars = 1
                    return plan

                compute_budget_mock.side_effect = _safe_controller_then_unsafe_resolver
                result = orchestrator.run("Inspect repository changes")
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_CLARIFICATION)
        self.assertIn('"status":"unavailable"', client.prompts[1])
        self.assertNotIn('"status":"not_found"', client.prompts[1])
        self.assertIn('"resolver_llm_calls":0', client.prompts[1])

    def test_budget_exceeded_correction_retry_never_falls_back_to_local(self):
        """Regression for the JSON-correction retry bypassing budget safety.

        When the main completion is routed externally because local
        inference is unsafe, a malformed external response must have its
        correction retry routed externally too - never straight back to
        llama-server.
        """
        class _FlakyJsonDelegation:
            def __init__(self):
                self.calls = []

            def catalog(self):
                return [{"provider": "azure_openai", "description": "specialist", "writes_repo": False, "cost": "paid"}]

            async def delegate(self, provider, task, *, context=""):
                self.calls.append((provider, task, context))
                if len(self.calls) == 1:
                    return DelegationResult(
                        provider=provider,
                        success=True,
                        content="not valid json at all",
                        metadata={},
                    )
                return DelegationResult(
                    provider=provider,
                    success=True,
                    content='{"type":"action","action":"answer","message":"handled externally",'
                    '"reason":"corrected externally"}',
                    metadata={},
                )

        delegation = _FlakyJsonDelegation()
        client = _Client([])  # local llama-server must never be invoked, even on correction
        orchestrator = self._orchestrator(client, delegation=delegation)
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
        finally:
            orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.message, "handled externally")
        self.assertEqual(len(delegation.calls), 2)
        self.assertEqual(delegation.calls[0][0], "azure_openai")
        self.assertEqual(delegation.calls[1][0], "azure_openai")
        self.assertEqual(client.prompts, [])

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
