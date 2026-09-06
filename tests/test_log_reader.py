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
        self.assertEqual(entry["timestamp"], "2026-08-17T12:00:00.000Z")
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

    def test_sentinel_no_entries_line_returns_none(self):
        self.assertIsNone(parse_journal_line("agent-api", "-- No entries --"))

    def test_sentinel_logs_begin_header_returns_none(self):
        line = "-- Logs begin at Mon 2026-01-01, end at Tue 2026-01-02. --"
        self.assertIsNone(parse_journal_line("agent-api", line))

    def test_unparsable_timestamp_is_kept_as_is(self):
        entry = parse_journal_line("agent-api", "not-a-timestamp host agent-api: hi there now")
        self.assertEqual(entry["timestamp"], "not-a-timestamp")


class ReadAllJournalEntriesTests(unittest.TestCase):
    def test_merges_entries_from_every_service(self):
        reader = LogReader()

        async def _fake_tail(service, lines=100, before=None, search=None):
            return [parse_journal_line(service, f"2026-08-17T12:00:00+0000 host {service}: hi")]

        with patch.object(reader, "tail_journal_entries", side_effect=_fake_tail):
            entries, errors = asyncio.run(
                reader.read_all_journal_entries(services=("agent-api", "nexus-mailbox"))
            )
        self.assertEqual(errors, {})
        self.assertEqual({e["source"] for e in entries}, {"agent-api", "nexus-mailbox"})

    def test_one_failing_service_does_not_drop_the_others(self):
        reader = LogReader()

        async def _fake_tail(service, lines=100, before=None, search=None):
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

    def test_before_is_forwarded_to_every_service(self):
        reader = LogReader()
        seen_before = []

        async def _fake_tail(service, lines=100, before=None, search=None):
            seen_before.append(before)
            return []

        with patch.object(reader, "tail_journal_entries", side_effect=_fake_tail):
            asyncio.run(
                reader.read_all_journal_entries(
                    services=("agent-api", "nexus-mailbox"),
                    before="2026-08-17T12:00:00.000Z",
                )
            )
        self.assertEqual(seen_before, ["2026-08-17T12:00:00.000Z", "2026-08-17T12:00:00.000Z"])


    def test_tail_journal_entries_raises_on_nonzero_returncode(self):
        reader = LogReader()
        with patch("modules.log_reader._run_journalctl", new=AsyncMock(return_value=(1, ""))):
            with self.assertRaises(RuntimeError):
                asyncio.run(reader.tail_journal_entries("agent-api"))

    def test_tail_journal_entries_attaches_source_and_rseq_tie_breakers(self):
        reader = LogReader()
        out = (
            "2026-08-17T12:00:00+00:00 host agent-api[1]: first\n"
            "2026-08-17T12:00:01+00:00 host agent-api[1]: second\n"
        )
        with patch("modules.log_reader._run_journalctl", new=AsyncMock(return_value=(0, out))):
            entries = asyncio.run(reader.tail_journal_entries("agent-api"))
        self.assertEqual([e["_source"] for e in entries], ["journal:agent-api", "journal:agent-api"])
        # Newest line (last in the batch) has the smallest _rseq (0),
        # mirroring central_logging.read_log_entries's own convention.
        self.assertEqual([e["_rseq"] for e in entries], [1, 0])

    def test_tail_journal_entries_passes_until_derived_from_before(self):
        reader = LogReader()
        run_mock = AsyncMock(return_value=(0, ""))
        with patch("modules.log_reader._run_journalctl", new=run_mock):
            asyncio.run(
                reader.tail_journal_entries(
                    "agent-api", before="2026-08-17T12:00:00.000Z",
                )
            )
        args = run_mock.await_args.args[0]
        self.assertIn("--until=2026-08-17 12:00:00", args)

    def test_tail_journal_entries_skips_until_for_unparsable_before(self):
        reader = LogReader()
        run_mock = AsyncMock(return_value=(0, ""))
        with patch("modules.log_reader._run_journalctl", new=run_mock):
            asyncio.run(reader.tail_journal_entries("agent-api", before="not-a-timestamp"))
        args = run_mock.await_args.args[0]
        self.assertFalse(any(a.startswith("--until=") for a in args))


if __name__ == "__main__":
    unittest.main()
