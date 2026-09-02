from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.experiment_logger import read_all_log_entries
from modules.logging_config import StructuredJsonlHandler


class StructuredJsonlHandlerTests(unittest.TestCase):
    def test_application_errors_are_redacted_in_unified_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = StructuredJsonlHandler(tmp)
            application_logger = logging.getLogger("modules.worker")
            application_logger.addHandler(handler)
            application_logger.setLevel(logging.INFO)
            application_logger.propagate = False
            try:
                application_logger.error(
                    "worker failed token=abcdef0123456789abcdef0123456789",
                    extra={"job_id": "job-1", "session_id": "session-1"},
                )
            finally:
                application_logger.removeHandler(handler)
                application_logger.propagate = True

            entries = read_all_log_entries(Path(tmp))
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["job_id"], "job-1")
            self.assertEqual(entries[0]["session_id"], "session-1")
            self.assertNotIn(
                "abcdef0123456789abcdef0123456789",
                entries[0]["message"],
            )

    def test_default_level_keeps_info_warning_error_and_excludes_debug(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {}, clear=False
        ):
            from os import environ

            environ.pop("NEXUS_LOG_LEVEL", None)
            handler = StructuredJsonlHandler(tmp)
            application_logger = logging.getLogger("modules.levels")
            application_logger.addHandler(handler)
            application_logger.setLevel(logging.DEBUG)
            application_logger.propagate = False
            try:
                application_logger.debug("debug")
                application_logger.info("info")
                application_logger.warning("warning")
                application_logger.error("error")
            finally:
                application_logger.removeHandler(handler)
                application_logger.propagate = True

            entries = read_all_log_entries(Path(tmp))
            self.assertEqual(
                [entry["level"] for entry in entries],
                ["INFO", "WARNING", "ERROR"],
            )

    def test_system_log_rotates_and_keeps_accepting_new_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = StructuredJsonlHandler(
                tmp,
                max_bytes=700,
                backup_count=2,
            )
            application_logger = logging.getLogger("modules.rollover")
            application_logger.addHandler(handler)
            application_logger.setLevel(logging.INFO)
            application_logger.propagate = False
            try:
                for index in range(12):
                    application_logger.info("rollover entry %d %s", index, "x" * 100)
            finally:
                application_logger.removeHandler(handler)
                application_logger.propagate = True

            self.assertTrue((Path(tmp) / "system-1.jsonl").exists())
            entries = read_all_log_entries(Path(tmp))
            self.assertEqual(entries[-1]["message"], f"rollover entry 11 {'x' * 100}")
            self.assertLessEqual(len(list(Path(tmp).glob("system*.jsonl"))), 3)


if __name__ == "__main__":
    unittest.main()
