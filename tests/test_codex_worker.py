"""Unit tests for modules.codex_worker.

All tests use a fake subprocess runner — no real Codex calls, no quota used.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules.codex_worker import (
    SANDBOX_MODES,
    CodexResult,
    CodexWorker,
    WorkerConfigError,
    _parse_jsonl,
    _truncate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_worker(**kwargs) -> CodexWorker:
    defaults = dict(
        allowed_roots=("/agent",),
        codex_bin="/fake/codex",
        max_output_bytes=64 * 1024,
        db_path=None,
    )
    defaults.update(kwargs)
    return CodexWorker(**defaults)


def _completed_proc(stdout="", stderr="", returncode=0):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


SAMPLE_JSONL = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "tid-abc"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "Hello world"}}),
    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 50}}),
])


# ---------------------------------------------------------------------------
# _parse_jsonl
# ---------------------------------------------------------------------------

class ParseJsonlTests(unittest.TestCase):
    def test_parses_thread_id_final_message_usage(self):
        thread_id, msg, usage = _parse_jsonl(SAMPLE_JSONL)
        self.assertEqual(thread_id, "tid-abc")
        self.assertEqual(msg, "Hello world")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 20)

    def test_handles_empty_stdout(self):
        thread_id, msg, usage = _parse_jsonl("")
        self.assertIsNone(thread_id)
        self.assertIsNone(msg)
        self.assertIsNone(usage)

    def test_ignores_non_json_lines(self):
        mixed = "not json\n" + json.dumps({"type": "thread.started", "thread_id": "x"})
        thread_id, _, _ = _parse_jsonl(mixed)
        self.assertEqual(thread_id, "x")

    def test_last_agent_message_wins(self):
        lines = "\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "last"}}),
        ])
        _, msg, _ = _parse_jsonl(lines)
        self.assertEqual(msg, "last")

    def test_selects_correct_final_message_amid_multiple_event_types(self):
        # Real Codex JSONL streams interleave thread/turn lifecycle events,
        # command_execution tool-call items, and agent_message items. Only
        # the last item.completed agent_message text must be used.
        lines = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "tid-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "planning..."}}),
            json.dumps({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "ls", "status": "in_progress"}}),
            json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "ls", "aggregated_output": "file.txt", "exit_code": 0, "status": "completed"}}),
            json.dumps({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": '{"title": "final"}'}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}}),
        ])
        thread_id, msg, usage = _parse_jsonl(lines)
        self.assertEqual(thread_id, "tid-1")
        self.assertEqual(msg, '{"title": "final"}')
        self.assertEqual(usage["input_tokens"], 5)

    def test_command_execution_items_never_become_final_message(self):
        lines = "\n".join([
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "real answer"}}),
            json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "cat secret.txt", "aggregated_output": "should not be final_message", "exit_code": 0}}),
        ])
        _, msg, _ = _parse_jsonl(lines)
        self.assertEqual(msg, "real answer")

    def test_reasoning_items_never_become_final_message(self):
        # Some Codex versions may emit reasoning/thinking items with their
        # own "type" (e.g. "reasoning"); these must never be treated as the
        # final agent response even if they carry a "text" field.
        lines = "\n".join([
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking about it..."}}),
            json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "actual final answer"}}),
        ])
        _, msg, _ = _parse_jsonl(lines)
        self.assertEqual(msg, "actual final answer")

    def test_reasoning_only_stream_yields_no_final_message(self):
        lines = json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "just thinking"}})
        _, msg, _ = _parse_jsonl(lines)
        self.assertIsNone(msg)


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

class TruncateTests(unittest.TestCase):
    def test_short_text_not_truncated(self):
        text, truncated = _truncate("hello", 100)
        self.assertEqual(text, "hello")
        self.assertFalse(truncated)

    def test_long_text_is_truncated_and_flagged(self):
        text, truncated = _truncate("x" * 200, 100)
        self.assertTrue(truncated)
        self.assertIn("[... output truncated]", text)
        self.assertLessEqual(len(text.encode("utf-8")), 200)  # reasonable bound

    def test_truncation_at_exact_limit(self):
        text, truncated = _truncate("a" * 10, 10)
        self.assertFalse(truncated)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = _make_worker(allowed_roots=(self.tmp.name,))
        self.addCleanup(self.tmp.cleanup)

    def test_empty_task_rejected(self):
        result = self.worker.run("", self.tmp.name)
        self.assertFalse(result.success)
        self.assertIn("empty", result.error)

    def test_whitespace_only_task_rejected(self):
        result = self.worker.run("   ", self.tmp.name)
        self.assertFalse(result.success)
        self.assertIn("empty", result.error)

    def test_relative_working_dir_rejected(self):
        result = self.worker.run("do thing", "relative/path")
        self.assertFalse(result.success)
        self.assertIn("absolute", result.error)

    def test_nonexistent_working_dir_rejected(self):
        result = self.worker.run("do thing", "/nonexistent/xyz")
        self.assertFalse(result.success)
        self.assertIn("not exist", result.error)

    def test_existing_working_dir_outside_old_roots_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = CodexWorker(allowed_roots=("/definitely/not/here",), codex_bin="/fake/codex")
            result = worker.run("test", tmpdir)
            self.assertNotIn("allowed roots", result.error or "")
            self.assertIn("Codex binary not found", result.error or "")

    def test_invalid_sandbox_rejected(self):
        result = self.worker.run("do thing", self.tmp.name, sandbox="invalid-mode")
        self.assertFalse(result.success)
        self.assertIn("invalid sandbox", result.error)

    def test_valid_subdir_is_allowed(self):
        subdir = Path(self.tmp.name) / "sub"
        subdir.mkdir()
        # Should not be rejected at validation (will fail later at process launch)
        worker = _make_worker(allowed_roots=(self.tmp.name,))
        with patch("subprocess.run", return_value=_completed_proc(stdout=SAMPLE_JSONL)):
            result = worker.run("do thing", str(subdir))
        self.assertIsNone(result.error)


# ---------------------------------------------------------------------------
# Successful execution
# ---------------------------------------------------------------------------

class SuccessfulRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = _make_worker(allowed_roots=(self.tmp.name,))
        self.addCleanup(self.tmp.cleanup)

    @patch("subprocess.run")
    def test_successful_run_populates_result(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL, returncode=0)
        result = self.worker.run("summarize this", self.tmp.name)

        self.assertTrue(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.thread_id, "tid-abc")
        self.assertEqual(result.final_message, "Hello world")
        self.assertEqual(result.usage["input_tokens"], 100)
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.error)

    @patch("subprocess.run")
    def test_usage_preserved_when_stdout_exceeds_max_output_bytes(self, mock_run):
        # Regression test: a long-running, tool-heavy phase (e.g. research
        # investigation with many command_execution events) can easily
        # produce raw stdout larger than max_output_bytes. The trailing
        # turn.completed event (which carries usage) must still be parsed
        # from the *full* stdout, not from the truncated copy that is
        # stored/displayed.
        worker = _make_worker(allowed_roots=(self.tmp.name,), max_output_bytes=200)
        noise_line = json.dumps({
            "type": "item.completed",
            "item": {"id": "noise", "type": "agent_message", "text": "x" * 500},
        })
        full_stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "tid-big"}),
            noise_line,
            json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "final answer"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 999, "output_tokens": 42}}),
        ])
        self.assertGreater(len(full_stdout.encode("utf-8")), 200)
        mock_run.return_value = _completed_proc(stdout=full_stdout, returncode=0)

        result = worker.run("investigate", self.tmp.name)

        self.assertEqual(result.usage, {"input_tokens": 999, "output_tokens": 42})
        self.assertEqual(result.final_message, "final answer")
        # The *stored* stdout is still capped, even though usage/parsing
        # used the full, untruncated stream.
        self.assertTrue(result.stdout_truncated)

    @patch("subprocess.run")
    def test_command_built_without_shell(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL)
        self.worker.run("task", self.tmp.name)

        call_args = mock_run.call_args
        # First positional arg is the command list
        cmd = call_args[0][0]
        self.assertIsInstance(cmd, list)
        # shell kwarg must not be True
        self.assertNotEqual(call_args.kwargs.get("shell"), True)

    @patch("subprocess.run")
    def test_sandbox_flag_in_command(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL)
        self.worker.run("task", self.tmp.name, sandbox="read-only")

        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--sandbox")
        self.assertEqual(cmd[idx + 1], "read-only")

    @patch("subprocess.run")
    def test_approve_for_me_only_in_workspace_write(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL)
        # read-only: must NOT have --approve-for-me
        self.worker.run("task", self.tmp.name, sandbox="read-only")
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--approve-for-me", cmd)

    @patch("subprocess.run")
    def test_approve_for_me_present_in_workspace_write(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL)
        self.worker.run("task", self.tmp.name, sandbox="workspace-write")
        cmd = mock_run.call_args[0][0]
        self.assertIn("--approve-for-me", cmd)

    @patch("subprocess.run")
    def test_special_characters_in_task_passed_safely(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL)
        task = 'summarize; rm -rf / && echo "pwned"'
        self.worker.run(task, self.tmp.name)

        cmd = mock_run.call_args[0][0]
        # Task must be a single element after '--'
        self.assertIn("--", cmd)
        sep_idx = cmd.index("--")
        self.assertEqual(cmd[sep_idx + 1], task)

    # ------------------------------------------------------------------
    # output_schema (native structured output support)
    # ------------------------------------------------------------------

    @patch("subprocess.run")
    def test_no_output_schema_flag_when_schema_is_none(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL)
        self.worker.run("task", self.tmp.name)

        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--output-schema", cmd)

    @patch("subprocess.run")
    def test_output_schema_flag_present_only_when_schema_given(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL)
        schema = {"type": "object", "properties": {"title": {"type": "string"}}}
        self.worker.run("task", self.tmp.name, output_schema=schema)

        cmd = mock_run.call_args[0][0]
        self.assertIn("--output-schema", cmd)
        idx = cmd.index("--output-schema")
        schema_path = cmd[idx + 1]
        self.assertTrue(os.path.isabs(schema_path))

    @patch("subprocess.run")
    def test_schema_file_contains_expected_json(self, mock_run):
        captured_path = {}

        def fake_run(cmd, **kwargs):
            if "--output-schema" in cmd:
                idx = cmd.index("--output-schema")
                path = cmd[idx + 1]
                with open(path) as f:
                    captured_path["content"] = json.load(f)
            return _completed_proc(stdout=SAMPLE_JSONL)

        mock_run.side_effect = fake_run
        schema = {"type": "object", "properties": {"title": {"type": "string"}}}
        self.worker.run("task", self.tmp.name, output_schema=schema)

        self.assertEqual(captured_path["content"], schema)

    @patch("subprocess.run")
    def test_schema_file_removed_after_successful_run(self, mock_run):
        captured_path = {}

        def fake_run(cmd, **kwargs):
            if "--output-schema" in cmd:
                idx = cmd.index("--output-schema")
                captured_path["path"] = cmd[idx + 1]
            return _completed_proc(stdout=SAMPLE_JSONL)

        mock_run.side_effect = fake_run
        schema = {"type": "object"}
        self.worker.run("task", self.tmp.name, output_schema=schema)

        self.assertFalse(os.path.exists(captured_path["path"]))

    @patch("subprocess.run")
    def test_schema_file_removed_after_timeout(self, mock_run):
        captured_path = {}

        def fake_run(cmd, **kwargs):
            if "--output-schema" in cmd:
                idx = cmd.index("--output-schema")
                captured_path["path"] = cmd[idx + 1]
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        mock_run.side_effect = fake_run
        schema = {"type": "object"}
        result = self.worker.run("task", self.tmp.name, output_schema=schema, timeout_seconds=1)

        self.assertTrue(result.timed_out)
        self.assertFalse(os.path.exists(captured_path["path"]))

    # ------------------------------------------------------------------
    # skip_git_repo_check (required for isolated, non-git scratch dirs)
    # ------------------------------------------------------------------

    @patch("subprocess.run")
    def test_no_skip_git_repo_check_flag_by_default(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL)
        self.worker.run("task", self.tmp.name)

        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--skip-git-repo-check", cmd)

    @patch("subprocess.run")
    def test_skip_git_repo_check_flag_present_when_requested(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL)
        self.worker.run("task", self.tmp.name, skip_git_repo_check=True)

        cmd = mock_run.call_args[0][0]
        self.assertIn("--skip-git-repo-check", cmd)

    @patch("subprocess.run")
    def test_schema_file_removed_after_exception(self, mock_run):
        captured_path = {}

        def fake_run(cmd, **kwargs):
            if "--output-schema" in cmd:
                idx = cmd.index("--output-schema")
                captured_path["path"] = cmd[idx + 1]
            raise OSError("boom")

        mock_run.side_effect = fake_run
        schema = {"type": "object"}
        result = self.worker.run("task", self.tmp.name, output_schema=schema)

        self.assertIsNotNone(result.error)
        self.assertFalse(os.path.exists(captured_path["path"]))


# ---------------------------------------------------------------------------
# Non-zero exit code
# ---------------------------------------------------------------------------

class NonZeroExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = _make_worker(allowed_roots=(self.tmp.name,))
        self.addCleanup(self.tmp.cleanup)

    @patch("subprocess.run")
    def test_non_zero_exit_marks_failure(self, mock_run):
        mock_run.return_value = _completed_proc(
            stdout="", stderr="something went wrong", returncode=1
        )
        result = self.worker.run("task", self.tmp.name)

        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)
        self.assertIsNone(result.error)  # not a worker error, just Codex exit


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

class TimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = _make_worker(allowed_roots=(self.tmp.name,))
        self.addCleanup(self.tmp.cleanup)

    @patch("subprocess.run")
    def test_timeout_sets_timed_out_flag(self, mock_run):
        exc = subprocess.TimeoutExpired(cmd=["codex"], timeout=5)
        exc.stdout = b""
        exc.stderr = b""
        mock_run.side_effect = exc

        result = self.worker.run("task", self.tmp.name, timeout_seconds=5)

        self.assertTrue(result.timed_out)
        self.assertFalse(result.success)
        self.assertIn("timed out", result.error)


# ---------------------------------------------------------------------------
# Missing binary
# ---------------------------------------------------------------------------

class MissingBinaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = _make_worker(
            allowed_roots=(self.tmp.name,),
            codex_bin="/does/not/exist/codex",
        )
        self.addCleanup(self.tmp.cleanup)

    @patch("subprocess.run")
    def test_missing_binary_returns_error(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        result = self.worker.run("task", self.tmp.name)

        self.assertFalse(result.success)
        self.assertIn("not found", result.error)


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------

class OutputTruncationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = _make_worker(
            allowed_roots=(self.tmp.name,),
            max_output_bytes=50,
        )
        self.addCleanup(self.tmp.cleanup)

    @patch("subprocess.run")
    def test_oversized_stdout_is_truncated_and_flagged(self, mock_run):
        big_output = "x" * 200
        mock_run.return_value = _completed_proc(stdout=big_output, returncode=0)
        result = self.worker.run("task", self.tmp.name)

        self.assertTrue(result.stdout_truncated)
        self.assertIn("[... output truncated]", result.stdout)

    @patch("subprocess.run")
    def test_oversized_stderr_is_truncated_and_flagged(self, mock_run):
        mock_run.return_value = _completed_proc(
            stdout=SAMPLE_JSONL, stderr="e" * 200, returncode=0
        )
        result = self.worker.run("task", self.tmp.name)

        self.assertTrue(result.stderr_truncated)
        self.assertIn("[... output truncated]", result.stderr)

    @patch("subprocess.run")
    def test_small_output_not_truncated(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL, returncode=0)
        worker = _make_worker(
            allowed_roots=(self.tmp.name,),
            max_output_bytes=64 * 1024,
        )
        result = worker.run("task", self.tmp.name)

        self.assertFalse(result.stdout_truncated)
        self.assertFalse(result.stderr_truncated)


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------

class ToDictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    @patch("subprocess.run")
    def test_to_dict_is_json_serializable(self, mock_run):
        worker = _make_worker(allowed_roots=(self.tmp.name,))
        mock_run.return_value = _completed_proc(stdout=SAMPLE_JSONL, returncode=0)
        result = worker.run("task", self.tmp.name)
        d = result.to_dict()
        # Should not raise
        serialized = json.dumps(d)
        self.assertIn("success", serialized)


if __name__ == "__main__":
    unittest.main()
