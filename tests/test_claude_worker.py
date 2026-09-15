"""Unit tests for modules.claude_worker.

All tests use a fake subprocess runner — no real Claude calls, no quota used.
"""

import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from modules.claude_worker import (
    PROVIDER,
    ClaudeWorker,
    _default_claude_bin,
    _model_from_result,
    _parse_result,
    _usage_from_result,
)
from modules.coding_worker import SANDBOX_MODES, WorkerConfigError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_worker(**kwargs) -> ClaudeWorker:
    defaults = dict(
        claude_bin="/fake/claude",
        max_output_bytes=64 * 1024,
        db_path=None,
        model=None,
    )
    defaults.update(kwargs)
    return ClaudeWorker(**defaults)


def _completed_proc(stdout="", stderr="", returncode=0):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def _result_json(**overrides) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "sid-abc",
        "result": "Hello world",
        "num_turns": 1,
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 50,
            "server_tool_use": {"web_search_requests": 0},
            "service_tier": "standard",
        },
        "modelUsage": {"claude-sonnet-5": {"inputTokens": 100}},
    }
    payload.update(overrides)
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

class ParseResultTests(unittest.TestCase):
    def test_parses_single_json_result_object(self):
        parsed = _parse_result(_result_json())
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["session_id"], "sid-abc")
        self.assertEqual(parsed["result"], "Hello world")

    def test_parses_last_result_event_of_a_stream(self):
        stream = "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "sid-abc"}),
            json.dumps({"type": "assistant", "message": {"content": []}}),
            _result_json(result="final answer"),
        ])
        parsed = _parse_result(stream)
        self.assertEqual(parsed["result"], "final answer")

    def test_ignores_unparseable_lines(self):
        stream = "not json\n" + _result_json() + "\nalso not json"
        parsed = _parse_result(stream)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["session_id"], "sid-abc")

    def test_returns_none_without_a_result_event(self):
        self.assertIsNone(_parse_result(""))
        self.assertIsNone(_parse_result("   "))
        self.assertIsNone(_parse_result(json.dumps({"type": "system"})))

    def test_usage_keeps_only_scalar_counters(self):
        usage = _usage_from_result(json.loads(_result_json()))
        self.assertEqual(
            usage,
            {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50},
        )

    def test_usage_is_none_when_absent(self):
        self.assertIsNone(_usage_from_result({}))
        self.assertIsNone(_usage_from_result({"usage": {"service_tier": "standard"}}))

    def test_model_name_falls_back_when_unreported(self):
        self.assertEqual(_model_from_result(json.loads(_result_json())), "claude-sonnet-5")
        self.assertEqual(_model_from_result({}), "claude")


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

class DefaultBinaryTests(unittest.TestCase):
    def test_env_override_wins(self):
        with patch.dict(os.environ, {"AGENT_CLAUDE_BIN": "/opt/claude"}):
            self.assertEqual(_default_claude_bin(), "/opt/claude")

    def test_falls_back_to_path_lookup(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("modules.claude_worker.shutil.which", return_value="/usr/bin/claude"):
            self.assertEqual(_default_claude_bin(), "/usr/bin/claude")

    def test_falls_back_to_agent_local_bin(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("modules.claude_worker.shutil.which", return_value=None):
            self.assertEqual(_default_claude_bin(), "/home/agent/.local/bin/claude")


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------

class BuildCommandTests(unittest.TestCase):
    def setUp(self):
        self.worker = _make_worker()

    def test_always_non_interactive_and_ephemeral(self):
        for sandbox in SANDBOX_MODES:
            cmd = self.worker._build_command(sandbox)
            self.assertEqual(cmd[0], "/fake/claude")
            self.assertIn("--print", cmd)
            self.assertIn("--no-session-persistence", cmd)
            self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
            self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "none")

    def test_read_only_exposes_only_read_tools(self):
        cmd = self.worker._build_command("read-only")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Glob,Grep")
        self.assertNotIn("--allowedTools", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("--permission-mode", cmd)

    def test_workspace_write_preapproves_edit_and_shell_tools(self):
        cmd = self.worker._build_command("workspace-write")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        tools = cmd[cmd.index("--tools") + 1]
        self.assertIn("Edit", tools)
        self.assertIn("Write", tools)
        self.assertIn("Bash", tools)
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], tools)
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_full_access_is_the_only_mode_that_bypasses_permissions(self):
        cmd = self.worker._build_command("danger-full-access")
        self.assertIn("--dangerously-skip-permissions", cmd)

    def test_output_schema_is_passed_inline(self):
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        cmd = self.worker._build_command("read-only", output_schema=schema)
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), schema)

    def test_no_schema_flag_without_a_schema(self):
        self.assertNotIn("--json-schema", self.worker._build_command("read-only"))

    def test_model_override_is_passed(self):
        cmd = _make_worker(model="opus")._build_command("read-only")
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")

    def test_model_comes_from_the_environment_when_unset(self):
        with patch.dict(os.environ, {"AGENT_CLAUDE_MODEL": "sonnet"}):
            cmd = _make_worker()._build_command("read-only")
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worker = _make_worker()

    @patch("subprocess.run")
    def test_empty_task_is_rejected_without_starting_anything(self, mock_run):
        result = self.worker.run("   ", self.tmp.name)
        self.assertFalse(result.success)
        self.assertIn("empty", result.error)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_relative_working_dir_is_rejected(self, mock_run):
        result = self.worker.run("task", "relative/dir")
        self.assertIn("absolute", result.error)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_missing_working_dir_is_rejected(self, mock_run):
        result = self.worker.run("task", "/does/not/exist/at/all")
        self.assertIn("not exist", result.error)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_unknown_sandbox_is_rejected(self, mock_run):
        result = self.worker.run("task", self.tmp.name, sandbox="anything-goes")
        self.assertIn("invalid sandbox", result.error)
        mock_run.assert_not_called()

    def test_config_error_result_names_the_claude_provider(self):
        result = self.worker.run("", self.tmp.name)
        self.assertEqual(result.provider, PROVIDER)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worker = _make_worker()

    @patch("subprocess.run")
    def test_successful_run_is_parsed(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=_result_json())
        result = self.worker.run("do a thing", self.tmp.name)

        self.assertTrue(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.thread_id, "sid-abc")
        self.assertEqual(result.final_message, "Hello world")
        self.assertEqual(result.usage["input_tokens"], 100)
        self.assertEqual(result.provider, PROVIDER)
        self.assertIsNone(result.error)
        self.assertFalse(result.timed_out)

    @patch("subprocess.run")
    def test_task_goes_to_stdin_not_argv(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=_result_json())
        self.worker.run("SECRET_MARKER task text", self.tmp.name)

        args, kwargs = mock_run.call_args
        self.assertEqual(kwargs["input"], "SECRET_MARKER task text")
        self.assertNotIn("SECRET_MARKER task text", args[0])

    @patch("subprocess.run")
    def test_runs_inside_the_working_directory(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=_result_json())
        self.worker.run("task", self.tmp.name)
        self.assertEqual(mock_run.call_args.kwargs["cwd"], self.tmp.name)

    @patch("subprocess.run")
    def test_never_uses_a_shell(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=_result_json())
        self.worker.run("task", self.tmp.name)
        self.assertIsInstance(mock_run.call_args.args[0], list)
        self.assertNotIn("shell", mock_run.call_args.kwargs)

    @patch("subprocess.run")
    def test_application_secrets_never_reach_the_subprocess(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=_result_json())
        with patch.dict(
            os.environ,
            {
                "AGENT_API_KEY": "super-secret",
                "ANTHROPIC_API_KEY": "anthropic-key",
                "PATH": "/usr/bin",
            },
        ):
            self.worker.run("task", self.tmp.name)

        env = mock_run.call_args.kwargs["env"]
        self.assertNotIn("AGENT_API_KEY", env)
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "anthropic-key")
        self.assertEqual(env.get("PATH"), "/usr/bin")

    @patch("subprocess.run")
    def test_reported_error_result_is_a_failure(self, mock_run):
        mock_run.return_value = _completed_proc(
            stdout=_result_json(is_error=True, subtype="error_max_turns", result="ran out of turns")
        )
        result = self.worker.run("task", self.tmp.name)
        self.assertFalse(result.success)
        self.assertIn("ran out of turns", result.error)

    @patch("subprocess.run")
    def test_unparseable_output_is_a_failure_even_on_exit_zero(self, mock_run):
        mock_run.return_value = _completed_proc(stdout="I am not JSON", returncode=0)
        result = self.worker.run("task", self.tmp.name)
        self.assertFalse(result.success)
        self.assertIn("no parseable result", result.error)

    @patch("subprocess.run")
    def test_non_zero_exit_is_a_failure(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=_result_json(), returncode=2)
        result = self.worker.run("task", self.tmp.name)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 2)

    @patch("subprocess.run")
    def test_timeout_is_reported_as_such(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["claude"], timeout=5, output="partial", stderr=b"boom"
        )
        result = self.worker.run("task", self.tmp.name, timeout_seconds=5)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.success)
        self.assertIn("timed out", result.error)
        self.assertEqual(result.stdout, "partial")
        self.assertEqual(result.stderr, "boom")

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_missing_binary_is_reported(self, _mock_run):
        result = self.worker.run("task", self.tmp.name)
        self.assertFalse(result.success)
        self.assertIn("Claude binary not found", result.error)

    @patch("subprocess.run", side_effect=PermissionError)
    def test_non_executable_binary_is_reported(self, _mock_run):
        result = self.worker.run("task", self.tmp.name)
        self.assertIn("not executable", result.error)

    @patch("subprocess.run")
    def test_long_output_is_truncated_and_flagged(self, mock_run):
        mock_run.return_value = _completed_proc(stdout="x" * 5000, stderr="y" * 5000)
        result = _make_worker(max_output_bytes=100).run("task", self.tmp.name)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)
        self.assertIn("[... output truncated]", result.stdout)

    @patch("subprocess.run")
    def test_result_is_json_serializable(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=_result_json())
        result = self.worker.run("task", self.tmp.name)
        self.assertIn("claude", json.dumps(result.to_dict()))


# ---------------------------------------------------------------------------
# Experiment logging
# ---------------------------------------------------------------------------

class ExperimentLoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    @patch("subprocess.run")
    def test_start_and_completion_are_logged(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=_result_json())
        elog = MagicMock()
        _make_worker().run("task", self.tmp.name, experiment_logger=elog, phase="implementation")

        self.assertEqual(elog.debug.call_args_list[0].args[1], "claude_call_start")
        self.assertEqual(elog.log_process.call_args.args[1], "claude_call_completed")

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_failure_is_logged_as_an_error(self, _mock_run):
        elog = MagicMock()
        _make_worker().run("task", self.tmp.name, experiment_logger=elog)
        self.assertEqual(elog.log_process.call_args.args[1], "claude_call_failed")
        self.assertEqual(elog.log_process.call_args.kwargs["level"], "ERROR")


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "agent.db")

    @patch("subprocess.run")
    def test_run_is_recorded_with_provider_and_cost(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=_result_json())
        _make_worker(db_path=self.db_path).run("task", self.tmp.name)

        from modules.run_store import init_db

        conn = init_db(self.db_path)
        row = conn.execute(
            "SELECT provider, model, input_tokens, output_tokens, actual_cost, "
            "cost_status, success FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()

        self.assertEqual(row[0], PROVIDER)
        self.assertEqual(row[1], "claude-sonnet-5")
        self.assertEqual(row[2], 100)
        self.assertEqual(row[3], 20)
        self.assertAlmostEqual(row[4], 0.0123)
        self.assertEqual(row[5], "actual")
        self.assertEqual(row[6], 1)

    @patch("subprocess.run")
    def test_audit_is_skipped_without_a_db(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=_result_json())
        # Must not raise and must not create anything.
        result = _make_worker(db_path=None).run("task", self.tmp.name)
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
