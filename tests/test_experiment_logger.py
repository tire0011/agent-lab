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
import unittest
from pathlib import Path

from modules.experiment_logger import (
    DEFAULT_LOG_DIR,
    ExperimentLogger,
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


if __name__ == "__main__":
    unittest.main()
