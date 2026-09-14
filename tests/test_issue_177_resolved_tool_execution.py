"""A resolved read-only tool runs without a second LLM decision (#177).

Live validation of #172 showed FailureAnalysis entering the shared
``find_tool``/``call_tool`` path and then stalling on a formality: after the
resolver had already returned one concrete read-only operation, the loop
asked the local model what to do next. On the 7B model that decision cost
many minutes, and in analysis ``878e8008`` the model answered it by
repeating its ``find_tool``. The guard refused the duplicate, gathering
ended with a single observation, no diagnostic operation ever ran, and the
diagnosis fell back on the stale GitHub-token hypothesis.

These tests drive only FailureAnalysis - no adoption, no finalization - and
pin down that the resolved operation is executed, that everything guarding
that execution still applies, and that nothing mutating can reach it.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.failure_analysis_service import (
    CATEGORY_UNAVAILABLE,
    FailureAnalysisService,
)

from tests.test_issue_172_failure_analysis_tools import (
    _ScriptedClient,
    _diagnosis_json,
    _registry,
)


_GIT_STATUS_GOAL = "inspect git_status of the repository working tree"
_LEAKY_FILENAME = "token=ghp_abcdefghijklmnop1234567.txt"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class ResolvedToolExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # A real repository, so `git status` returns real output rather than
        # an error that would hide whether execution happened at all.
        _git(self.tmp, "init", "-b", "main")
        _git(self.tmp, "config", "user.email", "test@example.com")
        _git(self.tmp, "config", "user.name", "Test User")
        (self.tmp / "app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
        _git(self.tmp, "add", "app.py")
        _git(self.tmp, "commit", "-m", "init")
        # An untracked change git_status will report - named so that its own
        # output carries a credential shape through the redaction path.
        (self.tmp / _LEAKY_FILENAME).write_text("x\n", encoding="utf-8")

        self.registry = _registry(self.tmp)
        self.addCleanup(self.registry.close)

    def _service(self, client) -> FailureAnalysisService:
        service = FailureAnalysisService(
            db_path=str(self.tmp / "agent.db"),
            llama_client=client,
            operations_registry=self.registry,
            max_tool_steps=4,
        )
        self.addCleanup(service.close)
        return service

    def _lookup_only_client(self) -> _ScriptedClient:
        """One find_tool, and nothing that ever says ``call_tool``.

        Whatever executes the operation therefore cannot have been an LLM
        action decision - the script contains none.
        """
        return _ScriptedClient(
            tool_steps=[
                json.dumps({
                    "action": "find_tool",
                    "goal": _GIT_STATUS_GOAL,
                    "reason": "the failure is about the repository's current state",
                }),
            ],
            diagnosis=_diagnosis_json(summary="working tree inspected", category="lifecycle_logic"),
        )

    def _audit_rows(self) -> list[sqlite3.Row]:
        con = sqlite3.connect(str(self.tmp / "agent.db"))
        con.row_factory = sqlite3.Row
        try:
            return list(con.execute("SELECT * FROM operations_audit ORDER BY rowid"))
        finally:
            con.close()

    def test_resolved_tool_runs_without_a_second_action_decision(self):
        client = self._lookup_only_client()
        service = self._service(client)

        analysis = service.analyze(
            subject_type="experiment",
            subject_id="exp-177",
            failure="git status unknown",
        )

        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)
        prompt = client.diagnosis_prompts[0]
        tool_lines = [line for line in prompt.splitlines() if line.startswith("[T")]

        # Exactly one lookup, and the operation ran right behind it.
        find_lines = [line for line in tool_lines if "find_tool" in line]
        call_lines = [line for line in tool_lines if "call_tool" in line]
        self.assertEqual(len(find_lines), 1, tool_lines)
        self.assertEqual(len(call_lines), 1, tool_lines)
        self.assertIn("git_status", call_lines[0])
        self.assertIn("[T2]", call_lines[0])

        # No scripted step ever asked for call_tool, so no action decision
        # stood between the resolution and the execution.
        self.assertNotIn("call_tool", " ".join(client.tool_decisions))

    def test_the_real_result_becomes_current_evidence(self):
        client = self._lookup_only_client()
        service = self._service(client)

        analysis = service.analyze(
            subject_type="experiment",
            subject_id="exp-177",
            failure="git status unknown",
        )

        prompt = client.diagnosis_prompts[0]
        self.assertIn("Current evidence, gathered just now", prompt)
        # The untracked file git actually reported is in the evidence, which
        # only holds if the operation really ran against the real repository.
        self.assertIn("??", prompt)
        self.assertIn(".txt", prompt)
        self.assertIn("T2", analysis.evidence_refs)

    def test_execution_is_audited(self):
        service = self._service(self._lookup_only_client())

        service.analyze(
            subject_type="experiment",
            subject_id="exp-177",
            failure="git status unknown",
        )

        rows = [dict(row) for row in self._audit_rows()]
        executed = [row for row in rows if row["operation"] == "git_status"]
        self.assertEqual(len(executed), 1, rows)
        self.assertEqual(executed[0]["success"], 1)
        self.assertEqual(json.loads(executed[0]["args_json"]), {})
        # The caller is deliberately not persisted (it would carry an API-key
        # identifier); the audit records what ran and how it went.
        self.assertNotIn("caller", executed[0])

    def test_no_mutating_operation_is_ever_auto_executed(self):
        service = self._service(self._lookup_only_client())

        service.analyze(
            subject_type="experiment",
            subject_id="exp-177",
            failure="git status unknown",
        )

        audited = {row["operation"] for row in self._audit_rows()}
        mutating = {
            spec.name
            for spec in self.registry.catalog()
            if getattr(spec, "mutates_state", False)
        }
        self.assertTrue(mutating, "the fixture must offer at least one mutating operation")
        self.assertEqual(audited & mutating, set())

    def test_redaction_and_bounds_apply_to_the_executed_result(self):
        client = self._lookup_only_client()
        service = self._service(client)

        service.analyze(
            subject_type="experiment",
            subject_id="exp-177",
            failure="git status unknown",
        )

        prompt = client.diagnosis_prompts[0]
        # git reported a filename of credential shape; it must not survive.
        self.assertNotIn("ghp_abcdefghijklmnop1234567", prompt)
        self.assertIn("[REDACTED]", prompt)
        call_line = next(line for line in prompt.splitlines() if line.startswith("[T2]"))
        self.assertLessEqual(len(call_line), 4000)


class DirectExecutionEligibilityTests(unittest.TestCase):
    """What may run without a decision, decided on the spec - not on a name."""

    @staticmethod
    def _found(**overrides):
        result = {
            "status": "found",
            "resolved_tool": "git_status",
            "arguments_schema": [],
            "operation": {"operation": "git_status", "mutates_state": False, "arguments": []},
        }
        result.update(overrides)
        return result

    def setUp(self):
        self.eligible = FailureAnalysisService._directly_executable_operation

    def test_read_only_no_argument_operation_is_eligible(self):
        self.assertEqual(self.eligible(self._found()), "git_status")

    def test_mutating_operation_is_never_eligible(self):
        result = self._found(
            resolved_tool="inbox_refresh",
            operation={"operation": "inbox_refresh", "mutates_state": True, "arguments": []},
        )
        self.assertIsNone(self.eligible(result))

    def test_read_only_must_be_stated_not_assumed(self):
        # A spec that does not answer the question is not a "no" to it.
        self.assertIsNone(self.eligible(self._found(operation={"operation": "git_status"})))
        self.assertIsNone(self.eligible(self._found(operation=None)))

    def test_required_arguments_without_a_default_are_not_invented(self):
        result = self._found(
            resolved_tool="experiment_show",
            operation={"operation": "experiment_show", "mutates_state": False},
            arguments_schema=[{"name": "experiment_id", "required": True, "default": None}],
        )
        self.assertIsNone(self.eligible(result))

    def test_optional_and_defaulted_arguments_stay_eligible(self):
        result = self._found(
            resolved_tool="git_log",
            arguments_schema=[
                {"name": "limit", "required": False, "default": None},
                {"name": "service", "required": True, "default": "agent-api"},
            ],
        )
        self.assertEqual(self.eligible(result), "git_log")

    def test_an_unresolved_lookup_is_not_eligible(self):
        for result in (
            self._found(status="not_found", resolved_tool=""),
            self._found(status="unavailable"),
            self._found(resolved_tool=""),
        ):
            with self.subTest(result=result):
                self.assertIsNone(self.eligible(result))


if __name__ == "__main__":
    unittest.main()
