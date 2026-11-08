"""Tests for Issue #131: making the Logs tab readable again.

Three defects stacked up so the feed looked like Nexus had stopped
logging: the newest entry was at the bottom of 500, terminal skips were
re-announced at INFO on every poll until they filled the whole window,
and mailbox lines were classified as `experiment`.
"""

from __future__ import annotations

import logging
import os
import unittest
import unittest.mock

import mailbox_processor
from modules.logging_config import _phase_for_logger

os.environ.setdefault("AGENT_API_KEY", "test-key")


class PermanentSkipTests(unittest.TestCase):
    """A skip that will hold forever must stop being announced at INFO."""

    def test_exhausted_retries_count_as_permanent(self):
        self.assertTrue(mailbox_processor._is_permanent_skip("max retries (3) reached"))
        self.assertTrue(mailbox_processor._is_permanent_skip("Max Retries (3) reached"))

    def test_transient_skips_are_not_permanent(self):
        for reason in ("already done", "cleaned already-done inbox file", "", None):
            with self.subTest(reason=reason):
                self.assertFalse(mailbox_processor._is_permanent_skip(reason))


class PollLoggingTests(unittest.TestCase):
    def _run_main_with(self, results: list[dict]):
        from modules.mailbox.poll_runner import MailboxPollRunResult

        run_result = MailboxPollRunResult(status="processed", results=results)
        with unittest.mock.patch.object(
            mailbox_processor, "run_mailbox_poll_once", return_value=run_result
        ):
            with self.assertLogs("mailbox_processor", level="DEBUG") as captured:
                exit_code = mailbox_processor.main(
                    ["--code-repo-path", "/tmp", "--mailbox-repo-path", "/tmp",
                     "--db-path", ":memory:", "--lock-path", "/tmp/nexus-test.lock"]
                )
        self.assertEqual(exit_code, 0)
        return captured.records

    def test_a_stuck_message_is_not_re_announced_at_info(self):
        records = self._run_main_with([
            {"status": "skipped", "file": "inbox/stuck.md", "reason": "max retries (3) reached"},
        ])

        skip_records = [r for r in records if "stuck.md" in r.getMessage()]
        self.assertEqual(len(skip_records), 1)
        # The detail is still available, just not at INFO on every poll.
        self.assertEqual(skip_records[0].levelno, logging.DEBUG)

    def test_a_transient_skip_is_still_reported_at_info(self):
        records = self._run_main_with([
            {"status": "skipped", "file": "inbox/done.md", "reason": "already done"},
        ])

        skip_records = [r for r in records if "done.md" in r.getMessage()]
        self.assertEqual(skip_records[0].levelno, logging.INFO)

    def test_the_summary_still_accounts_for_skipped_messages(self):
        """Quieting the per-file line must not hide that anything happened."""
        records = self._run_main_with([
            {"status": "skipped", "file": "inbox/a.md", "reason": "max retries (3) reached"},
            {"status": "skipped", "file": "inbox/b.md", "reason": "max retries (3) reached"},
            {"status": "done", "file": "inbox/c.md", "outbox": "out.md"},
        ])

        summary = [r.getMessage() for r in records if "Processed" in r.getMessage()]
        self.assertEqual(len(summary), 1)
        self.assertIn("3 message(s)", summary[0])
        self.assertIn("2 skipped", summary[0])


class MailboxPhaseTests(unittest.TestCase):
    """Mailbox records must not be filed under `experiment`."""

    def test_mailbox_loggers_get_the_mailbox_phase(self):
        for name in (
            "mailbox_processor",
            "modules.mailbox.poll_runner",
            "modules.mailbox.processing",
            "modules.mailbox.retention",
        ):
            with self.subTest(logger=name):
                self.assertEqual(_phase_for_logger(name), "mailbox")

    def test_other_loggers_keep_their_module_phase(self):
        self.assertEqual(_phase_for_logger("modules.experiment_service.service"), "service")
        self.assertEqual(_phase_for_logger("api"), "api")

    def test_a_logger_merely_starting_with_the_prefix_is_not_matched(self):
        self.assertEqual(_phase_for_logger("mailbox_processor_extra"), "mailbox_processor_extra")

    def test_mailbox_phases_are_classified_as_the_mailbox_source(self):
        import api

        for phase in ("mailbox", "queued", "mailbox_processor", "poll_runner", "processing"):
            with self.subTest(phase=phase):
                self.assertEqual(api._derive_log_source(phase), "mailbox")

    def test_unknown_phases_still_fall_back_to_experiment(self):
        import api

        self.assertEqual(api._derive_log_source("implementation"), "experiment")


if __name__ == "__main__":
    unittest.main()
