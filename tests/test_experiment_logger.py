"""Focused unit tests for modules.experiment_logger.ExperimentLogger.

Covers the requirements from the centralized-logging task: JSONL format,
log levels, experiment/mailbox-task-id correlation, immediate flush,
thread-safe concurrent writes, redaction, and size-limit/truncation
behavior. No real subprocess or Codex call is involved.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from modules.experiment_logger import (
    DEFAULT_LOG_DIR,
    MAX_STDERR_CHARS,
    MAX_STDOUT_CHARS,
    ExperimentLogger,
    _bounded_head_tail_text,
    decode_log_cursor,
    encode_log_cursor,
    get_task_logger,
    read_all_log_entries,
    read_experiment_logs,
    read_log_entries,
    sanitize_command,
)


def _read_lines(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class ExperimentLoggerBasicsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_jsonl_format_one_object_per_line(self):
        elog = ExperimentLogger("exp1", log_dir=self.log_dir)
        elog.info("research", "research_started", "starting")
        elog.debug("research", "file_selected", "chose foo.py")
        elog.warning("selection", "plan_reformat_retry", "retrying")

        lines = _read_lines(elog.path)
        self.assertEqual(len(lines), 3)
        for entry in lines:
            self.assertIsInstance(entry, dict)
            # Required baseline fields per spec.
            for field in ("timestamp", "level", "logger", "message", "experiment_id", "phase", "event"):
                self.assertIn(field, entry)

    def test_log_levels_recorded_correctly(self):
        elog = ExperimentLogger("exp2", log_dir=self.log_dir)
        elog.debug("p", "e1", "debug msg")
        elog.info("p", "e2", "info msg")
        elog.warning("p", "e3", "warning msg")
        elog.error("p", "e4", "error msg")

        levels = [entry["level"] for entry in _read_lines(elog.path)]
        self.assertEqual(levels, ["DEBUG", "INFO", "WARNING", "ERROR"])

    def test_correlation_via_experiment_and_mailbox_task_id(self):
        elog = ExperimentLogger("exp3", mailbox_task_id="task-abc", log_dir=self.log_dir)
        elog.info("mailbox", "mailbox_task_submitted", "submitted")

        entry = _read_lines(elog.path)[0]
        self.assertEqual(entry["experiment_id"], "exp3")
        self.assertEqual(entry["mailbox_task_id"], "task-abc")

    def test_mailbox_task_id_omitted_when_absent(self):
        elog = ExperimentLogger("exp4", log_dir=self.log_dir)
        elog.info("p", "e", "no mailbox task")
        entry = _read_lines(elog.path)[0]
        self.assertNotIn("mailbox_task_id", entry)

    def test_bind_mailbox_task_id_attaches_later(self):
        elog = ExperimentLogger("exp5", log_dir=self.log_dir)
        elog.info("p", "before_bind", "no id yet")
        elog.bind_mailbox_task_id("task-xyz")
        elog.info("p", "after_bind", "has id now")

        entries = _read_lines(elog.path)
        self.assertNotIn("mailbox_task_id", entries[0])
        self.assertEqual(entries[1]["mailbox_task_id"], "task-xyz")

    def test_writes_are_immediately_visible_on_disk(self):
        """Every entry must be durable immediately (flush+fsync), not only
        at process/object teardown - required so a crash or SSH
        disconnect right after logging never loses the entry."""
        elog = ExperimentLogger("exp6", log_dir=self.log_dir)
        elog.info("p", "e", "first")
        # A brand new read from disk (independent of the writer object)
        # must already see the entry.
        self.assertEqual(len(_read_lines(elog.path)), 1)
        elog.info("p", "e2", "second")
        self.assertEqual(len(_read_lines(elog.path)), 2)

    def test_log_process_records_command_exit_code_and_duration(self):
        elog = ExperimentLogger("exp7", log_dir=self.log_dir)
        elog.log_process(
            "verifying", "tests_completed", "done",
            command=["python", "-m", "pytest"],
            cwd="/some/worktree",
            duration_ms=1234,
            exit_code=0,
            stdout="ok",
            stderr="",
        )
        entry = _read_lines(elog.path)[0]
        self.assertEqual(entry["exit_code"], 0)
        self.assertEqual(entry["duration_ms"], 1234)
        self.assertIn("pytest", entry["command"])
        self.assertEqual(entry["level"], "INFO")

    def test_log_process_defaults_to_error_on_nonzero_exit(self):
        elog = ExperimentLogger("exp8", log_dir=self.log_dir)
        elog.log_process(
            "verifying", "tests_completed", "done",
            command=["python", "-m", "pytest"],
            exit_code=1,
        )
        entry = _read_lines(elog.path)[0]
        self.assertEqual(entry["level"], "ERROR")

    def test_log_process_defaults_to_error_on_timeout(self):
        elog = ExperimentLogger("exp9", log_dir=self.log_dir)
        elog.log_process(
            "verifying", "tests_timeout", "timed out",
            command=["python", "-m", "pytest"],
            timed_out=True,
        )
        entry = _read_lines(elog.path)[0]
        self.assertEqual(entry["level"], "ERROR")
        self.assertTrue(entry.get("timed_out"))

    def test_error_captures_exception_details(self):
        elog = ExperimentLogger("exp10", log_dir=self.log_dir)
        try:
            raise ValueError("boom")
        except ValueError as exc:
            elog.error("verifying", "tests_exception", "exception raised", exc=exc)

        entry = _read_lines(elog.path)[0]
        self.assertEqual(entry["exception_type"], "ValueError")
        self.assertEqual(entry["exception_message"], "boom")
        self.assertIn("Traceback", entry["stacktrace"])


class ExperimentLoggerRedactionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_secrets_redacted_from_message(self):
        elog = ExperimentLogger("exp-secret", log_dir=self.log_dir)
        elog.info("p", "e", "api_key=sk-super-secret-value-123456")
        entry = _read_lines(elog.path)[0]
        self.assertNotIn("sk-super-secret-value-123456", json.dumps(entry))

    def test_secrets_redacted_from_stdout_stderr(self):
        elog = ExperimentLogger("exp-secret2", log_dir=self.log_dir)
        elog.log_process(
            "verifying", "tests_completed", "done",
            stdout="token: abcdef0123456789abcdef0123456789",
            stderr="Authorization: Bearer abcdef0123456789abcdef0123456789",
            exit_code=0,
        )
        raw = elog.path.read_text(encoding="utf-8")
        self.assertNotIn("abcdef0123456789abcdef0123456789", raw)

    def test_secrets_redacted_in_command(self):
        cleaned = sanitize_command(["curl", "-H", "Authorization: Bearer sk-abcdef0123456789abcdef"])
        self.assertNotIn("sk-abcdef0123456789abcdef", cleaned)

    def test_read_log_entries_redacts_defensively_on_read(self):
        """Even if a raw line somehow contained an unredacted secret, the
        read path re-sanitizes independently of the write path."""
        elog = ExperimentLogger("exp-secret3", log_dir=self.log_dir)
        # Bypass the normal write path's redaction to simulate a legacy or
        # externally-written line that still has a secret in it.
        raw_entry = {
            "timestamp": "2020-01-01T00:00:00.000Z",
            "level": "INFO",
            "logger": "nexus.experiment",
            "message": "token=abcdef0123456789abcdef0123456789",
            "experiment_id": "exp-secret3",
            "phase": "p",
            "event": "e",
        }
        with open(elog.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(raw_entry) + "\n")

        entries = read_log_entries(elog.path)
        self.assertNotIn("abcdef0123456789abcdef0123456789", json.dumps(entries))


class ExperimentLoggerSizeLimitTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_long_message_is_truncated_and_flagged(self):
        elog = ExperimentLogger("exp-trunc", log_dir=self.log_dir)
        elog.info("p", "e", "x" * 10000)
        entry = _read_lines(elog.path)[0]
        self.assertTrue(entry.get("truncated"))
        self.assertLess(len(entry["message"]), 10000)

    def test_file_size_ceiling_stops_further_writes_with_marker(self):
        elog = ExperimentLogger("exp-cap", log_dir=self.log_dir, max_bytes=500)
        for i in range(200):
            elog.info("p", f"e{i}", f"message number {i} padding padding padding")

        entries = _read_lines(elog.path)
        # Exactly one size-limit marker, and the file never grows far past
        # the ceiling (some slack allowed for the single marker entry).
        markers = [e for e in entries if e["event"] == "log_size_limit_reached"]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["level"], "WARNING")
        self.assertLess(elog.path.stat().st_size, 2000)


class HeadTailTruncationTests(unittest.TestCase):
    """Problem 2 regression: stdout/stderr truncation must preserve both
    the beginning *and* the end of long diagnostic process output, since
    the final unittest failure summary and traceback tail appear at the
    end, not the beginning."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_short_output_is_not_truncated(self):
        text = "hello world"
        result, truncated = _bounded_head_tail_text(text, 100)
        self.assertEqual(result, text)
        self.assertFalse(truncated)

    def test_exact_boundary_is_not_truncated(self):
        text = "x" * 100
        result, truncated = _bounded_head_tail_text(text, 100)
        self.assertEqual(result, text)
        self.assertFalse(truncated)
        self.assertLessEqual(len(result), 100)

    def test_long_output_is_truncated_with_marker_and_stays_within_limit(self):
        text = "A" * 5000
        result, truncated = _bounded_head_tail_text(text, 200)
        self.assertTrue(truncated)
        self.assertLessEqual(len(result), 200)
        self.assertIn("TRUNCATED", result)

    def test_head_is_preserved(self):
        text = "START_MARKER_HEAD" + ("x" * 5000)
        result, truncated = _bounded_head_tail_text(text, 300)
        self.assertTrue(truncated)
        self.assertTrue(result.startswith("START_MARKER_HEAD"))

    def test_tail_preserves_unittest_failure_summary(self):
        # Simulates a long unittest run whose final, most important lines
        # (failure summary + traceback tail) are at the very end.
        body = "test output line\n" * 500
        tail = "\nFAILED (failures=1)\nAssertionError: boom at line 42\n"
        text = body + tail
        result, truncated = _bounded_head_tail_text(text, 1000)
        self.assertTrue(truncated)
        self.assertIn("FAILED (failures=1)", result)
        self.assertIn("AssertionError: boom at line 42", result)
        self.assertLessEqual(len(result), 1000)

    def test_redaction_applied_to_full_text_before_splitting(self):
        # A secret that would fall right at the boundary between head and
        # tail (in the omitted middle) must still be redacted, proving
        # redaction runs on the whole string first, not on the surviving
        # slices only.
        secret = "sk-testsecretkeyabcdefghijklmnopqrstuvwxyz1234567890"
        text = ("a" * 2000) + f"AZURE_OPENAI_API_KEY={secret}" + ("b" * 2000)
        result, truncated = _bounded_head_tail_text(text, 200)
        self.assertTrue(truncated)
        self.assertNotIn(secret, result)

    def test_log_process_stdout_stderr_use_head_tail_truncation(self):
        elog = ExperimentLogger("exp-headtail", log_dir=self.log_dir)
        long_stdout = "start-of-stdout\n" + ("x" * (MAX_STDOUT_CHARS * 2))
        long_stderr = "start-of-stderr\n" + ("y" * (MAX_STDERR_CHARS * 2)) + "\nFAILED (failures=2)"
        elog.log_process(
            "verifying", "tests_completed", "done",
            command=["python", "-m", "unittest"], exit_code=1,
            stdout=long_stdout, stderr=long_stderr,
        )
        entry = _read_lines(elog.path)[0]
        self.assertTrue(entry["truncated"])
        self.assertTrue(entry["stdout"].startswith("start-of-stdout"))
        self.assertTrue(entry["stderr"].startswith("start-of-stderr"))
        self.assertIn("FAILED (failures=2)", entry["stderr"])
        self.assertLessEqual(len(entry["stdout"]), MAX_STDOUT_CHARS)
        self.assertLessEqual(len(entry["stderr"]), MAX_STDERR_CHARS)


class ExperimentLoggerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_concurrent_writes_from_multiple_threads_are_not_corrupted(self):
        """Simulates the mailbox worker thread and the experiment pipeline
        thread both writing to the same experiment's log concurrently."""
        n_threads = 8
        n_per_thread = 25

        def _worker(idx: int) -> None:
            elog = ExperimentLogger("exp-concurrent", log_dir=self.log_dir)
            for j in range(n_per_thread):
                elog.info("p", "concurrent_event", f"thread {idx} message {j}")

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        path = self.log_dir / "exp-concurrent.jsonl"
        entries = _read_lines(path)
        # Every line must be valid, complete JSON (no interleaved/partial
        # writes) and every expected entry must be present exactly once.
        self.assertEqual(len(entries), n_threads * n_per_thread)


class ExperimentLoggerReadPathTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_read_log_entries_missing_file_returns_empty_list(self):
        entries = read_log_entries(self.log_dir / "does-not-exist.jsonl")
        self.assertEqual(entries, [])

    def test_read_log_entries_filters_by_level_and_phase(self):
        elog = ExperimentLogger("exp-filter", log_dir=self.log_dir)
        elog.info("research", "research_started", "starting")
        elog.error("verifying", "tests_failed", "boom")
        elog.warning("verifying", "diff_check_issue", "issue")

        only_errors = read_log_entries(elog.path, level="ERROR")
        self.assertEqual(len(only_errors), 1)
        self.assertEqual(only_errors[0]["event"], "tests_failed")

        only_verifying = read_log_entries(elog.path, phase="verifying")
        self.assertEqual(len(only_verifying), 2)

    def test_read_log_entries_respects_limit_keeping_most_recent(self):
        elog = ExperimentLogger("exp-limit", log_dir=self.log_dir)
        for i in range(10):
            elog.info("p", f"e{i}", f"message {i}")

        limited = read_log_entries(elog.path, limit=3)
        self.assertEqual(len(limited), 3)
        self.assertEqual([e["event"] for e in limited], ["e7", "e8", "e9"])

    def test_default_log_dir_is_plain_relative_path(self):
        self.assertEqual(DEFAULT_LOG_DIR, "state/experiment-logs")


class ReadAllLogEntriesTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_missing_dir_returns_empty_list(self):
        entries = read_all_log_entries(self.log_dir / "does-not-exist")
        self.assertEqual(entries, [])

    def test_merges_multiple_experiments_chronologically(self):
        elog_a = ExperimentLogger("exp-a", mailbox_task_id="task-a", log_dir=self.log_dir)
        elog_b = ExperimentLogger("exp-b", mailbox_task_id="task-b", log_dir=self.log_dir)
        elog_a.info("queued", "task_bound", "experiment a queued")
        time.sleep(0.005)
        elog_b.info("queued", "task_bound", "experiment b queued")
        time.sleep(0.005)
        elog_a.info("implementation", "implementation_started", "experiment a implementing")
        time.sleep(0.005)
        elog_b.info("verifying", "verification_started", "experiment b verifying")

        entries = read_all_log_entries(self.log_dir)
        self.assertEqual(len(entries), 4)
        # Entries interleave in write order across both files, not grouped
        # by experiment - a real chronological merge, not a concatenation.
        experiment_order = [e["experiment_id"] for e in entries]
        self.assertEqual(experiment_order, ["exp-a", "exp-b", "exp-a", "exp-b"])

    def test_filters_by_experiment_id_reads_only_that_file(self):
        ExperimentLogger("exp-a", log_dir=self.log_dir).info("p", "e1", "a message")
        ExperimentLogger("exp-b", log_dir=self.log_dir).info("p", "e2", "b message")

        entries = read_all_log_entries(self.log_dir, experiment_id="exp-a")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["experiment_id"], "exp-a")

    def test_filters_by_mailbox_task_id_across_files(self):
        ExperimentLogger("exp-a", mailbox_task_id="task-1", log_dir=self.log_dir).info(
            "p", "e1", "a message"
        )
        ExperimentLogger("exp-b", mailbox_task_id="task-2", log_dir=self.log_dir).info(
            "p", "e2", "b message"
        )

        entries = read_all_log_entries(self.log_dir, mailbox_task_id="task-2")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["experiment_id"], "exp-b")
        self.assertEqual(entries[0]["mailbox_task_id"], "task-2")

    def test_filters_by_level_and_phase_across_files(self):
        elog_a = ExperimentLogger("exp-a", log_dir=self.log_dir)
        elog_b = ExperimentLogger("exp-b", log_dir=self.log_dir)
        elog_a.info("research", "started", "ok")
        elog_a.error("verifying", "tests_failed", "boom")
        elog_b.warning("verifying", "diff_check_issue", "issue")

        only_errors = read_all_log_entries(self.log_dir, level="ERROR")
        self.assertEqual(len(only_errors), 1)
        self.assertEqual(only_errors[0]["event"], "tests_failed")

        only_verifying = read_all_log_entries(self.log_dir, phase="verifying")
        self.assertEqual(len(only_verifying), 2)

    def test_respects_limit_keeping_most_recent_across_files(self):
        elog_a = ExperimentLogger("exp-a", log_dir=self.log_dir)
        elog_b = ExperimentLogger("exp-b", log_dir=self.log_dir)
        for i in range(3):
            elog_a.info("p", f"a{i}", f"message {i}")
            time.sleep(0.005)
            elog_b.info("p", f"b{i}", f"message {i}")
            time.sleep(0.005)

        limited = read_all_log_entries(self.log_dir, limit=2)
        self.assertEqual(len(limited), 2)
        self.assertEqual([e["event"] for e in limited], ["a2", "b2"])

    def test_malformed_line_in_one_file_does_not_break_the_feed(self):
        elog_a = ExperimentLogger("exp-a", log_dir=self.log_dir)
        elog_a.info("p", "good", "fine")
        with open(self.log_dir / "exp-corrupt.jsonl", "a", encoding="utf-8") as fh:
            fh.write("{not valid json\n")

        entries = read_all_log_entries(self.log_dir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "good")

    def test_redacts_secrets_across_all_files(self):
        secret = "abcdef0123456789abcdef0123456789"
        ExperimentLogger("exp-a", log_dir=self.log_dir).error(
            "verifying", "tests_failed", "boom", stderr=f"token={secret}"
        )
        entries = read_all_log_entries(self.log_dir)
        serialized = json.dumps(entries)
        self.assertNotIn(secret, serialized)


class TaskLoggerPreExperimentTests(unittest.TestCase):
    """Gap 1: mailbox intake/queueing events must be durable and visible
    before any experiment id exists, and later correlatable once one is
    created."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_get_task_logger_writes_to_task_prefixed_file(self):
        task_log = get_task_logger("mailbox-task-1", log_dir=self.log_dir)
        task_log.info("mailbox", "task_received", "task received")
        self.assertEqual(task_log.path, self.log_dir / "task-mailbox-task-1.jsonl")
        entries = _read_lines(task_log.path)
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]["experiment_id"])
        self.assertEqual(entries[0]["mailbox_task_id"], "mailbox-task-1")

    def test_experiment_logger_requires_experiment_id_or_mailbox_task_id(self):
        with self.assertRaises(ValueError):
            ExperimentLogger(None, log_dir=self.log_dir)

    def test_experiment_logger_rejects_unsafe_ids(self):
        with self.assertRaises(ValueError):
            ExperimentLogger("../../etc/passwd", log_dir=self.log_dir)
        with self.assertRaises(ValueError):
            get_task_logger("../../etc/passwd", log_dir=self.log_dir)

    def test_task_log_entries_are_visible_before_an_experiment_exists(self):
        get_task_logger("task-9", log_dir=self.log_dir).info(
            "mailbox", "task_received", "task received"
        )
        entries = read_all_log_entries(self.log_dir)
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]["experiment_id"])
        self.assertEqual(entries[0]["mailbox_task_id"], "task-9")

    def test_experiment_created_event_correlates_task_with_new_experiment(self):
        task_log = get_task_logger("task-9", log_dir=self.log_dir)
        task_log.info("mailbox", "task_received", "task received")
        task_log.info("queued", "experiment_created", "experiment created", experiment_id="exp-42")

        entries = read_all_log_entries(self.log_dir)
        self.assertEqual(len(entries), 2)
        self.assertIsNone(entries[0]["experiment_id"])
        self.assertEqual(entries[1]["experiment_id"], "exp-42")
        self.assertEqual(entries[1]["mailbox_task_id"], "task-9")

    def test_read_experiment_logs_merges_experiment_with_its_task_log(self):
        get_task_logger("task-9", log_dir=self.log_dir).info(
            "mailbox", "task_received", "task received"
        )
        time.sleep(0.005)
        ExperimentLogger("exp-42", mailbox_task_id="task-9", log_dir=self.log_dir).info(
            "implementation", "implementation_started", "starting"
        )

        merged = read_experiment_logs(self.log_dir, "exp-42", mailbox_task_id="task-9")
        self.assertEqual([e["event"] for e in merged], ["task_received", "implementation_started"])

    def test_read_experiment_logs_without_mailbox_task_id_reads_only_experiment_file(self):
        get_task_logger("task-9", log_dir=self.log_dir).info(
            "mailbox", "task_received", "task received"
        )
        ExperimentLogger("exp-42", log_dir=self.log_dir).info(
            "implementation", "implementation_started", "starting"
        )
        merged = read_experiment_logs(self.log_dir, "exp-42")
        self.assertEqual([e["event"] for e in merged], ["implementation_started"])

    def test_read_experiment_logs_rejects_unsafe_experiment_id(self):
        ExperimentLogger("exp-42", log_dir=self.log_dir).info(
            "implementation", "implementation_started", "starting"
        )
        self.assertEqual(read_experiment_logs(self.log_dir, "../../etc/passwd"), [])
        self.assertEqual(read_experiment_logs(self.log_dir, ""), [])


class MalformedEntryValidationTests(unittest.TestCase):
    """Gap 3: a syntactically-valid JSON object missing a required field or
    with the wrong type for a field must be skipped, not surfaced as an
    unhandled error further up the stack."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def _write_raw_lines(self, path: Path, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")

    def test_entry_missing_required_field_is_skipped(self):
        path = self.log_dir / "exp-a.jsonl"
        good = json.dumps({
            "timestamp": "2026-01-01T00:00:00Z", "level": "INFO", "logger": "nexus.experiment",
            "experiment_id": "exp-a", "phase": "p", "event": "good",
        })
        # Missing "phase" - a syntactically valid JSON object, but not a
        # well-formed log entry.
        missing_field = json.dumps({
            "timestamp": "2026-01-01T00:00:01Z", "level": "INFO", "logger": "nexus.experiment",
            "experiment_id": "exp-a", "event": "bad",
        })
        self._write_raw_lines(path, [good, missing_field])

        entries = read_log_entries(path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "good")

    def test_entry_with_wrong_field_type_is_skipped(self):
        path = self.log_dir / "exp-a.jsonl"
        good = json.dumps({
            "timestamp": "2026-01-01T00:00:00Z", "level": "INFO", "logger": "nexus.experiment",
            "experiment_id": "exp-a", "phase": "p", "event": "good",
        })
        # exit_code must be an int, not a string.
        wrong_type = json.dumps({
            "timestamp": "2026-01-01T00:00:01Z", "level": "INFO", "logger": "nexus.experiment",
            "experiment_id": "exp-a", "phase": "p", "event": "bad", "exit_code": "not-a-number",
        })
        self._write_raw_lines(path, [good, wrong_type])

        entries = read_log_entries(path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "good")

    def test_entry_with_null_experiment_id_is_accepted(self):
        # Pre-experiment task-log entries legitimately have no experiment_id.
        path = self.log_dir / "task-1.jsonl"
        entry = json.dumps({
            "timestamp": "2026-01-01T00:00:00Z", "level": "INFO", "logger": "nexus.experiment",
            "experiment_id": None, "mailbox_task_id": "task-1", "phase": "mailbox", "event": "task_received",
        })
        self._write_raw_lines(path, [entry])
        entries = read_log_entries(path)
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]["experiment_id"])

    def test_malformed_entry_does_not_break_unified_feed(self):
        elog = ExperimentLogger("exp-a", log_dir=self.log_dir)
        elog.info("p", "good", "fine")
        bad = json.dumps({
            "timestamp": "2026-01-01T00:00:01Z", "level": "INFO", "logger": "nexus.experiment",
            "experiment_id": "exp-a", "event": "missing_phase",
        })
        with open(elog.path, "a", encoding="utf-8") as fh:
            fh.write(bad + "\n")

        entries = read_all_log_entries(self.log_dir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "good")


class BoundedReadAndCursorTests(unittest.TestCase):
    """Gap 2: the unified multi-file scan must not read unbounded data per
    file, and cursor-based pagination must not re-scan already-seen data."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_tail_bytes_reads_only_the_trailing_window(self):
        path = self.log_dir / "big.jsonl"
        lines = []
        for i in range(200):
            lines.append(json.dumps({
                "timestamp": f"2026-01-01T00:{i:02d}:00Z", "level": "INFO",
                "logger": "nexus.experiment", "experiment_id": "exp-big",
                "phase": "p", "event": f"e{i}",
            }))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        entries = read_log_entries(path, tail_bytes=200)
        self.assertGreater(len(entries), 0)
        self.assertLess(len(entries), 200)
        # Only the newest events survive a small tail window.
        self.assertEqual(entries[-1]["event"], "e199")

    def test_unified_feed_applies_a_default_tail_bound_per_file(self):
        elog = ExperimentLogger("exp-a", log_dir=self.log_dir)
        for i in range(50):
            elog.info("p", f"e{i}", "x" * 2000)

        full = read_log_entries(elog.path)
        bounded = read_all_log_entries(self.log_dir, tail_bytes=500)
        self.assertLess(len(bounded), len(full))

    def test_before_cursor_excludes_entries_at_or_after_the_cursor(self):
        elog = ExperimentLogger("exp-a", log_dir=self.log_dir)
        for i in range(5):
            elog.info("p", f"e{i}", "msg")
            time.sleep(0.002)

        all_entries = read_all_log_entries(self.log_dir)
        cursor = encode_log_cursor(all_entries[2])
        earlier = read_all_log_entries(self.log_dir, before=cursor)
        self.assertEqual([e["event"] for e in earlier], ["e0", "e1"])

    def test_legacy_or_malformed_cursor_is_treated_as_no_cursor(self):
        # A bare ISO timestamp (the pre-fix cursor format) or garbage
        # string must never fail the request - just behave as if no
        # cursor were given, matching an empty/legacy bookmarked URL.
        elog = ExperimentLogger("exp-a", log_dir=self.log_dir)
        elog.info("p", "e0", "msg")
        self.assertIsNone(decode_log_cursor("2026-01-01T00:00:00Z"))
        self.assertIsNone(decode_log_cursor("not-base64-json!!"))
        entries = read_all_log_entries(self.log_dir, before="2026-01-01T00:00:00Z")
        self.assertEqual([e["event"] for e in entries], ["e0"])

    def _fetch_page(self, *, limit: int, cursor: str | None = None):
        """Mirror the API layer's pagination contract: fetch ``limit + 1``
        chronological entries, keep the newest ``limit``, and cursor off
        the oldest entry actually kept (never the extra look-ahead one)."""
        fetched = read_all_log_entries(self.log_dir, limit=limit + 1, before=cursor)
        if len(fetched) <= limit:
            return fetched, None
        page = fetched[-limit:] if limit > 0 else []
        next_cursor = encode_log_cursor(page[0]) if page else None
        return page, next_cursor

    def test_pagination_via_cursor_produces_no_duplicates(self):
        elog = ExperimentLogger("exp-a", log_dir=self.log_dir)
        for i in range(6):
            elog.info("p", f"e{i}", "msg")
            time.sleep(0.002)

        first_page, cursor = self._fetch_page(limit=3)
        self.assertEqual([e["event"] for e in first_page], ["e3", "e4", "e5"])
        self.assertIsNotNone(cursor)
        second_page, next_cursor = self._fetch_page(limit=3, cursor=cursor)
        self.assertEqual([e["event"] for e in second_page], ["e0", "e1", "e2"])
        self.assertIsNone(next_cursor)
        seen = [e["event"] for e in second_page] + [e["event"] for e in first_page]
        self.assertEqual(seen, ["e0", "e1", "e2", "e3", "e4", "e5"])
        self.assertEqual(len(set(seen)), 6)

    def test_pagination_across_many_boundaries_reproduces_every_entry_once(self):
        # Concatenating every page (walking `next_cursor` until exhausted)
        # must reproduce the complete, deduplicated source exactly once -
        # regardless of how the boundary happens to fall.
        elog = ExperimentLogger("exp-a", log_dir=self.log_dir)
        for i in range(17):
            elog.info("p", f"e{i}", "msg")
            time.sleep(0.002)

        collected: list[str] = []
        cursor = None
        for _ in range(50):
            page, cursor = self._fetch_page(limit=4, cursor=cursor)
            collected = [e["event"] for e in page] + collected
            if cursor is None:
                break
        self.assertEqual(collected, [f"e{i}" for i in range(17)])

    def test_pagination_breaks_ties_between_entries_sharing_a_timestamp(self):
        # Several entries written back-to-back can legitimately share the
        # exact same millisecond timestamp; the tie-breaker must still
        # give a stable, gap-free, duplicate-free page boundary.
        elog = ExperimentLogger("exp-a", log_dir=self.log_dir)
        for i in range(9):
            elog.info("p", f"e{i}", "msg")  # no sleep: identical timestamps likely

        collected: list[str] = []
        cursor = None
        for _ in range(50):
            page, cursor = self._fetch_page(limit=2, cursor=cursor)
            collected = [e["event"] for e in page] + collected
            if cursor is None:
                break
        self.assertEqual(collected, [f"e{i}" for i in range(9)])

    def test_pagination_reaches_history_older_than_the_tail_window(self):
        # The default (no-cursor) read only inspects the trailing
        # DEFAULT_UNIFIED_TAIL_BYTES of each file; once a cursor is
        # supplied (an explicit "load older" step), older entries beyond
        # that window must still be reachable, never permanently hidden.
        elog = ExperimentLogger("exp-a", log_dir=self.log_dir)
        for i in range(400):
            elog.info("p", f"e{i}", "x" * 200)

        tiny_tail_bytes = 500
        first = read_all_log_entries(self.log_dir, tail_bytes=tiny_tail_bytes, limit=5)
        self.assertNotIn("e0", [e["event"] for e in first])

        oldest_visible = first[0]
        cursor = encode_log_cursor(oldest_visible)
        # Supplying a cursor - an explicit "load older" step - escapes the
        # tail bound automatically (even though the caller still passes
        # the same small tail_bytes), reaching all the way back to the
        # very first entry instead of returning nothing.
        older = read_all_log_entries(
            self.log_dir, tail_bytes=tiny_tail_bytes, before=cursor, limit=1000,
        )
        self.assertIn("e0", [e["event"] for e in older])


if __name__ == "__main__":
    unittest.main()
