"""Ensure failure-analysis fields survive the stdlib -> JSONL bridge (#151)."""

from __future__ import annotations

import logging
import unittest

from modules.logging_config import StructuredJsonlHandler


class _Sink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict]] = []

    def info(self, phase, event, message, **extra):
        self.calls.append((phase, event, message, extra))

    warning = info
    debug = info

    def error(self, phase, event, message, exc=None, **extra):
        self.calls.append((phase, event, message, extra))


class FailureAnalysisLoggingBridgeTests(unittest.TestCase):
    def test_lifecycle_source_is_not_lost_to_reserved_log_source(self) -> None:
        handler = StructuredJsonlHandler.__new__(StructuredJsonlHandler)
        logging.Handler.__init__(handler)
        sink = _Sink()
        handler._structured = sink

        record = logging.LogRecord(
            "modules.failure_analysis_observability",
            logging.WARNING,
            __file__,
            1,
            "finalization_needs_review",
            (),
            None,
        )
        record.phase = "failure_analysis"
        record.event = "finalization_needs_review"
        record.experiment_id = "exp-1"
        record.mailbox_task_id = "task-1"
        record.source = "recovery"
        record.failure_analysis_requested = True

        handler.emit(record)

        self.assertEqual(len(sink.calls), 1)
        phase, event, _message, extra = sink.calls[0]
        self.assertEqual(phase, "failure_analysis")
        self.assertEqual(event, "finalization_needs_review")
        self.assertEqual(extra["experiment_id"], "exp-1")
        self.assertEqual(extra["mailbox_task_id"], "task-1")
        self.assertEqual(extra["lifecycle_source"], "recovery")
        self.assertTrue(extra["failure_analysis_requested"])
        self.assertNotIn("source", extra)


if __name__ == "__main__":
    unittest.main()
