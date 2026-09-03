"""Focused tests for modules.log_reader: parsing/normalizing systemd
journal lines into the same shape as a structured JSONL log entry, and
merging several services' journals concurrently without one failing
service blanking out the others.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from modules.log_reader import LogReader, parse_journal_line


class ParseJournalLineTests(unittest.TestCase):
    def test_parses_timestamp_service_and_message(self):
        line = "2026-08-17T12:00:00+0000 host agent-api[123]: request handled"
        entry = parse_journal_line("agent-api", line)
        self.assertEqual(entry["timestamp"], "2026-08-17T12:00:00+0000")
        self.assertEqual(entry["message"], "request handled")
        self.assertEqual(entry["source"], "agent-api")
        self.assertEqual(entry["phase"], "system")
        self.assertEqual(entry["logger"], "journalctl")
        self.assertEqual(entry["event"], "journal:agent-api")
        self.assertEqual(entry["level"], "INFO")

    def test_detects_error_level(self):
        line = "2026-08-17T12:00:01+0000 host agent-api[123]: TaskOrchestratorError: boom"
        entry = parse_journal_line("agent-api", line)
        self.assertEqual(entry["level"], "ERROR")

    def test_detects_warning_level(self):
        line = "2026-08-17T12:00:02+0000 host nexus-mailbox[1]: WARNING retrying"
        entry = parse_journal_line("nexus-mailbox", line)
        self.assertEqual(entry["level"], "WARNING")

    def test_blank_line_returns_none(self):
        self.assertIsNone(parse_journal_line("agent-api", "   "))


class ReadAllJournalEntriesTests(unittest.TestCase):
    def test_merges_entries_from_every_service(self):
        reader = LogReader()

        async def _fake_tail(service, lines=100):
            return [parse_journal_line(service, f"2026-08-17T12:00:00+0000 host {service}: hi")]

        with patch.object(reader, "tail_journal_entries", side_effect=_fake_tail):
            entries, errors = asyncio.run(
                reader.read_all_journal_entries(services=("agent-api", "nexus-mailbox"))
            )
        self.assertEqual(errors, {})
        self.assertEqual({e["source"] for e in entries}, {"agent-api", "nexus-mailbox"})

    def test_one_failing_service_does_not_drop_the_others(self):
        reader = LogReader()

        async def _fake_tail(service, lines=100):
            if service == "llama-server":
                raise RuntimeError("journalctl failed for llama-server (exit code 1)")
            return [parse_journal_line(service, f"2026-08-17T12:00:00+0000 host {service}: hi")]

        with patch.object(reader, "tail_journal_entries", side_effect=_fake_tail):
            entries, errors = asyncio.run(
                reader.read_all_journal_entries(
                    services=("agent-api", "nexus-mailbox", "llama-server")
                )
            )
        self.assertEqual({e["source"] for e in entries}, {"agent-api", "nexus-mailbox"})
        self.assertEqual(
            errors, {"llama-server": "journalctl failed for llama-server (exit code 1)"}
        )

    def test_tail_journal_entries_raises_on_nonzero_returncode(self):
        reader = LogReader()
        with patch("modules.log_reader._run_journalctl", new=AsyncMock(return_value=(1, ""))):
            with self.assertRaises(RuntimeError):
                asyncio.run(reader.tail_journal_entries("agent-api"))


if __name__ == "__main__":
    unittest.main()
