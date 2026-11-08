"""Issue #120: missing executables become capability gaps with recovery choices."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules.capability_gaps import (
    ALT_EXISTING_EQUIVALENT,
    ALT_INSTALL,
    ALT_REUSABLE_OPERATION,
    AUTO_PROVISION_ENV,
    CHOICE_MISSING_CAPABILITY,
    CapabilityGapStore,
    plan_recovery,
    provision_tool,
)
from modules.operations_registry import OperationsRegistry
from modules.task_orchestrator import ACTION_ANSWER, NexusTaskOrchestrator

RG_COMMAND = 'rg -n -C 4 "(--version|VERSION|def health)" agent.py api.py tests'
RG_NOT_FOUND = ("exit_code: 127\nstderr:\nbash: line 1: rg: command not found", None)
GREP_OK = ("exit_code: 0\nstdout:\nagent.py:10:VERSION = '1'", None)
EXECUTION = "modules.task_orchestrator_parts.execution"


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        return self.responses.pop(0)


class _IssueService:
    """Fake GitHub tracking: exact-title dedup like the real service."""

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.ensure_calls: list[dict] = []
        self.resolved: list[tuple[int, str]] = []

    def __call__(self, **_kwargs):
        return self

    def ensure_capability_issue(self, **kwargs):
        self.ensure_calls.append(kwargs)
        title = f"Provide {kwargs['capability']} when {kwargs['display_name']} is unavailable"
        for number, issue in self.issues.items():
            if issue["title"] == title and issue["state"] == "open":
                return {"number": number, "url": f"https://example/{number}", "created": False}
        number = 1200 + len(self.issues)
        self.issues[number] = {"title": title, "state": "open", "details": kwargs}
        return {"number": number, "url": f"https://example/{number}", "created": True}

    def resolve_capability_issue(self, number, *, note):
        self.resolved.append((number, note))
        self.issues[number]["state"] = "closed"

    def close(self):
        pass


def _which(available: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in available else None


class PlanRecoveryTests(unittest.TestCase):
    def _plan(self, executable="rg", *, equivalent=None, occurrences=1, auto=False, ops=("search_text",)):
        return plan_recovery(
            executable=executable,
            command=f"{executable} x",
            equivalent=equivalent,
            occurrences=occurrences,
            auto_provision=auto,
            available_operations=set(ops),
        )

    def test_all_four_alternatives_are_offered(self):
        gap = self._plan(equivalent=("grep -R -E x", "rg -> grep"))
        kinds = [item["kind"] for item in gap["alternatives"]]
        self.assertEqual(kinds, ["existing_equivalent", "install_or_provision", "capability_issue", "reusable_operation"])
        self.assertEqual(gap["capability"], "repository text-search capability")
        self.assertEqual(gap["normalized_identity"], "rg")

    def test_policy_prefers_equivalent_then_install_then_operation_then_missing(self):
        self.assertEqual(self._plan(equivalent=("grep x", "m"), auto=True)["chosen"], ALT_EXISTING_EQUIVALENT)
        self.assertEqual(self._plan(auto=True)["chosen"], ALT_INSTALL)
        self.assertEqual(self._plan()["chosen"], ALT_REUSABLE_OPERATION)
        self.assertEqual(self._plan(ops=())["chosen"], CHOICE_MISSING_CAPABILITY)

    def test_unknown_tools_become_durable_only_when_recurring(self):
        self.assertFalse(self._plan("cargo", ops=())["durable"])
        recurring = self._plan("cargo", occurrences=2, ops=())
        self.assertTrue(recurring["durable"])
        self.assertTrue(recurring["recurring"])
        self.assertFalse(self._plan("python3", occurrences=5)["durable"])

    def test_recurring_gap_promotes_the_reusable_operation(self):
        self.assertFalse(self._plan()["alternatives"][3]["promoted"])
        self.assertTrue(self._plan(occurrences=2)["alternatives"][3]["promoted"])


class ProvisionToolTests(unittest.TestCase):
    def test_installs_ripgrep_with_apt_and_verifies_the_executable(self):
        installed = set()
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            installed.add("rg")
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with patch("modules.capability_gaps.os.geteuid", return_value=1000):
            outcome = provision_tool("rg", runner=runner, which=lambda n: "/usr/bin/rg" if n in installed else None)

        self.assertEqual(outcome["status"], "installed")
        self.assertEqual(calls, [["sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", "ripgrep"]])

    def test_already_available_tool_is_not_reinstalled(self):
        runner = MagicMock()
        outcome = provision_tool("rg", runner=runner, which=lambda _n: "/usr/bin/rg")
        self.assertEqual(outcome["status"], "already_available")
        runner.assert_not_called()

    def test_rejects_non_executable_names(self):
        with self.assertRaises(ValueError):
            provision_tool("rg; rm -rf /", runner=MagicMock(), which=lambda _n: None)


class CapabilityGapFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.db_path = str(root / "agent.db")
        self.issues = _IssueService()
        self.subprocess_calls = []
        self.registry = OperationsRegistry(
            experiment_service=MagicMock(),
            db_path=self.db_path,
            repo_root=str(self.repo),
            mailbox_lock_path=str(root / "mailbox.lock"),
            subprocess_runner=self._subprocess,
            github_work_tracking_factory=self.issues,
        )
        self.addCleanup(self.registry.close)
        self.available = {"grep"}

    def _subprocess(self, command, **kwargs):
        self.subprocess_calls.append(command)
        if "apt-get" in command:
            self.available.add("rg")
        return subprocess.CompletedProcess(command, 0, "installed", "")

    def _orchestrator(self, responses=()):
        orchestrator = NexusTaskOrchestrator(
            operations_registry=self.registry,
            experiment_service=MagicMock(),
            llama_client=_Client(responses),
        )
        self.addCleanup(orchestrator.close)
        return orchestrator

    def _patched(self, side_effect, *, env=None):
        stack = ExitStack()
        command = stack.enter_context(patch(f"{EXECUTION}.process_command", side_effect=side_effect))
        which = _which(self.available)
        stack.enter_context(patch("modules.runtime_execution.shutil.which", side_effect=lambda n: which(n)))
        stack.enter_context(patch("modules.capability_gaps.shutil.which", side_effect=lambda n: which(n)))
        stack.enter_context(patch(f"{EXECUTION}.shutil.which", side_effect=lambda n: which(n)))
        stack.enter_context(patch.dict("os.environ", env or {AUTO_PROVISION_ENV: ""}))
        return stack, command

    def _gap_row(self, identity="rg"):
        store = CapabilityGapStore(self.db_path)
        try:
            return store.get(identity)
        finally:
            store.close()

    def test_rg_regression_falls_back_tracks_one_issue_and_task_continues(self):
        orchestrator = self._orchestrator([
            json.dumps({"type": "tool", "tool": "command", "command": RG_COMMAND, "reason": "repository text search"}),
            json.dumps({"type": "action", "action": "answer", "message": "VERSION is in agent.py.",
                        "reason": "search results answer the question"}),
        ])
        stack, command = self._patched([RG_NOT_FOUND, GREP_OK])
        with stack:
            result = orchestrator.run("Where is the version defined?")

        self.assertEqual(result.action, ACTION_ANSWER)
        # The failing rg command was not repeated: the second call is grep.
        bodies = [call.args[0].body for call in command.call_args_list]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(bodies[0].startswith("rg "))
        self.assertTrue(bodies[1].startswith("grep -R -E"))
        observation = result.observations[0]
        gap = observation["recovery"]["capability_gap"]
        self.assertEqual(gap["missing_executable"], "rg")
        self.assertEqual(gap["chosen"], ALT_EXISTING_EQUIVALENT)
        self.assertEqual(observation["recovery"]["capability_issue"]["status"], "created")
        details = self.issues.ensure_calls[0]
        self.assertEqual(details["attempted_command"], RG_COMMAND)
        self.assertIn("grep -R -E", details["recovery"])
        self.assertFalse(details["installation_attempted"])
        self.assertTrue(details["origin"])
        self.assertIn("search_text", details["suggested_solution"])
        self.assertEqual(
            next(iter(self.issues.issues.values()))["title"],
            "Provide repository text-search capability when ripgrep is unavailable",
        )
        # Persisted in session history and the durable gap store.
        events = orchestrator.task_store.recent_events(result.session_id, role="tool", limit=50)
        persisted = json.loads(events[0]["content"])["recovery"]
        self.assertEqual(persisted["choice"], ALT_EXISTING_EQUIVALENT)
        self.assertEqual(persisted["capability_issue"]["url"], "https://example/1200")
        row = self._gap_row()
        self.assertEqual(row["issue_number"], 1200)
        self.assertIn(ALT_EXISTING_EQUIVALENT, row["last_recovery"])

    def test_duplicate_missing_rg_events_reuse_one_issue(self):
        orchestrator = self._orchestrator()
        stack, _command = self._patched([RG_NOT_FOUND, GREP_OK, RG_NOT_FOUND, GREP_OK])
        with stack:
            first = orchestrator._run_command_with_recovery(RG_COMMAND, tool="command", extra={"session_id": "s1"})
            second = orchestrator._run_command_with_recovery("rg TODO", tool="command", extra={"session_id": "s2"})

        self.assertEqual(first["recovery"]["capability_issue"]["status"], "created")
        self.assertEqual(second["recovery"]["capability_issue"]["status"], "reused")
        self.assertEqual(second["recovery"]["capability_issue"]["number"], 1200)
        self.assertEqual(len(self.issues.ensure_calls), 1)
        self.assertEqual(len(self.issues.issues), 1)
        self.assertEqual(self._gap_row()["occurrences"], 2)
        self.assertTrue(second["recovery"]["capability_gap"]["alternatives"][3]["promoted"])

    def test_without_equivalent_the_reusable_search_operation_is_recommended(self):
        self.available = set()
        orchestrator = self._orchestrator()
        stack, command = self._patched([RG_NOT_FOUND])
        with stack:
            observation = orchestrator._run_command_with_recovery(RG_COMMAND, tool="command")

        self.assertEqual(command.call_count, 1)
        self.assertEqual(observation["event"], "command_not_found")
        gap = observation["capability_gap"]
        self.assertEqual(gap["chosen"], ALT_REUSABLE_OPERATION)
        self.assertEqual(gap["recommended_operation"], "search_text")
        self.assertIn("search_text", observation["message"])
        self.assertIn("provision_tool", observation["message"])
        self.assertEqual(gap["issue"]["status"], "created")

    def test_auto_provisioning_installs_retries_and_closes_the_issue(self):
        self.available = set()
        orchestrator = self._orchestrator()
        stack, _ = self._patched([RG_NOT_FOUND])
        with stack:
            orchestrator._run_command_with_recovery(RG_COMMAND, tool="command")
        self.assertEqual(len(self.issues.issues), 1)

        stack, command = self._patched([RG_NOT_FOUND, GREP_OK], env={AUTO_PROVISION_ENV: "1"})
        with stack:
            observation = orchestrator._run_command_with_recovery(RG_COMMAND, tool="command")

        self.assertEqual(observation["recovery"]["choice"], ALT_INSTALL)
        self.assertEqual(command.call_args.args[0].body, RG_COMMAND)  # retried once, after install
        gap = observation["recovery"]["capability_gap"]
        self.assertTrue(gap["installation_attempted"])
        self.assertTrue(gap["installation"]["success"])
        self.assertEqual(gap["issue"]["status"], "closed")
        self.assertEqual(self.issues.resolved[0][0], 1200)
        self.assertTrue(any("apt-get" in call for call in self.subprocess_calls))
        # Provisioning is an explicit, audited operation.
        conn = sqlite3.connect(self.db_path)
        try:
            audited = conn.execute("SELECT parameters, success FROM operation_log WHERE operation = 'provision_tool'").fetchall()
        finally:
            conn.close()
        self.assertEqual(audited, [(json.dumps({"tool": "rg"}), 1)])
        self.assertEqual(self._gap_row()["status"], "resolved")

    def test_tool_becoming_available_later_closes_its_issue(self):
        self.available = set()
        orchestrator = self._orchestrator()
        stack, _ = self._patched([RG_NOT_FOUND])
        with stack:
            orchestrator._run_command_with_recovery(RG_COMMAND, tool="command")

        self.available = {"rg"}
        stack, _ = self._patched([GREP_OK])
        with stack:
            orchestrator._run_command_with_recovery("rg -n VERSION agent.py", tool="command")

        self.assertEqual([number for number, _ in self.issues.resolved], [1200])
        self.assertEqual(self.issues.issues[1200]["state"], "closed")

    def test_issue_tracking_failure_does_not_block_the_fallback(self):
        def broken(**_kwargs):
            raise RuntimeError("GitHub unavailable")

        self.registry.github_work_tracking_factory = broken
        orchestrator = self._orchestrator()
        stack, command = self._patched([RG_NOT_FOUND, GREP_OK])
        with stack:
            observation = orchestrator._run_command_with_recovery(RG_COMMAND, tool="command")

        self.assertEqual(command.call_count, 2)
        self.assertIn("VERSION", observation["result"])
        self.assertEqual(observation["recovery"]["capability_issue"]["status"], "failed")

    def test_first_unknown_tool_does_not_open_an_issue(self):
        orchestrator = self._orchestrator()
        stack, _ = self._patched([("exit_code: 127\nstderr:\ncargo: not found", None)])
        with stack:
            observation = orchestrator._run_command_with_recovery("cargo build", tool="command")

        self.assertEqual(observation["capability_gap"]["issue"]["status"], "not_required")
        self.assertEqual(self.issues.ensure_calls, [])


class RegistryProvisionOperationTests(unittest.TestCase):
    def test_router_resolves_repository_text_search_to_the_reusable_operation(self):
        from modules.nexus_tool_router import NexusToolRouter

        with tempfile.TemporaryDirectory() as tmp:
            registry = OperationsRegistry(experiment_service=MagicMock(), db_path=str(Path(tmp) / "a.db"), repo_root=tmp)
            try:
                router = NexusToolRouter(operations_registry=registry, schema_completion=MagicMock())
                names = [spec.name for spec in router.candidate_operations("repository text search with rg")]
                git_names = [spec.name for spec in router.candidate_operations("inspect repository changes")]
            finally:
                registry.close()
        self.assertEqual(names[0], "search_text")
        self.assertEqual(git_names[0], "git_status")

    def test_provision_tool_is_cataloged_as_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = OperationsRegistry(experiment_service=MagicMock(), db_path=str(Path(tmp) / "a.db"), repo_root=tmp)
            try:
                spec = {s.name: s for s in registry.catalog()}["provision_tool"]
            finally:
                registry.close()
        self.assertTrue(spec.mutates_state)
        # The controller may install a tool explicitly; installation is allowed on the Nexus VM.
        self.assertTrue(spec.controller_allowed)


if __name__ == "__main__":
    unittest.main()
