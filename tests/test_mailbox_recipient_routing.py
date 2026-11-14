"""The legacy Nexus processor leaves messages for other agents untouched (mailbox issue #9).

A shared inbox may hold `to: home-gpu` messages, including `type: command`.
Nexus must not run, log, record, retry, archive or answer them.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from modules.mailbox import routing
from tests.test_mailbox import _ProcessorTestBase


def _command(to_line: str | None, body: str = "echo hi") -> str:
    head = "---\ntype: command\ncreated_by: Claude\n" + (f"{to_line}\n" if to_line is not None else "") + "---\n\n"
    return head + body


class RecipientParsingTests(unittest.TestCase):
    def test_rules_match_the_mailbox_client(self):
        self.assertIsNone(routing.recipients(_command(None)))
        self.assertEqual(routing.recipients(_command("to: Nexus")), frozenset({"nexus"}))
        self.assertEqual(routing.recipients(_command("to: home-gpu, NEXUS")), frozenset({"home-gpu", "nexus"}))
        self.assertEqual(routing.recipients(_command('to: "home-gpu"')), frozenset({"home-gpu"}))
        self.assertEqual(routing.recipients(_command("to:")), frozenset())
        self.assertEqual(routing.recipients(_command("to: ,")), frozenset())
        self.assertEqual(routing.recipients(_command("to: nexus\nto: home-gpu")), frozenset())

    def test_a_to_line_in_an_unclosed_header_still_counts(self):
        self.assertEqual(routing.recipients("---\ntype: command\nTo: home-gpu\necho hi"), frozenset({"home-gpu"}))


class ProcessorRoutingTests(_ProcessorTestBase):
    def setUp(self):
        super().setUp()
        self.calls: list[str] = []

        def fake_command(msg, *args, **kwargs):
            self.calls.append(msg.file_path.name)
            return "ran", None

        processing = __import__("modules.mailbox.processing", fromlist=["_PROCESSORS"])
        patcher = patch.dict(processing._PROCESSORS, {"command": fake_command})
        patcher.start()
        self.addCleanup(patcher.stop)
        core = __import__("modules.mailbox.core", fromlist=["SUPPORTED_TYPES"])
        added = "command" not in core.SUPPORTED_TYPES
        core.SUPPORTED_TYPES.add("command")
        if added:
            self.addCleanup(core.SUPPORTED_TYPES.discard, "command")

    def _records(self):
        return [row[0] for row in self._conn().execute("SELECT file_path FROM mailbox_messages")]

    def _logs(self):
        return list((self.repo / "state" / "experiment-logs").glob("*.jsonl"))

    def test_a_command_for_home_gpu_is_never_run_or_touched(self):
        path = self._write("gpu.md", _command("to: home-gpu"))
        for _ in range(3):  # repeated polling
            self.assertEqual(self.processor.process_inbox(), [])
        self.assertEqual(self.calls, [])
        self.assertTrue(path.exists())
        self.assertEqual(self._records(), [])
        self.assertEqual(self._logs(), [])
        self.assertEqual(list(self.outbox.iterdir()), [])
        self.assertEqual(self.processor.list_inbox_files(), [])

    def test_an_explicit_batch_is_filtered_too(self):
        path = self._write("gpu.md", _command("to: home-gpu"))
        [result] = self.processor.process_inbox([path])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual((self.calls, self._records(), self._logs()), ([], [], []))

    def test_unknown_empty_and_repeated_targets_are_left_alone(self):
        for name, line in (("unknown.md", "to: somebody-else"), ("empty.md", "to:"),
                           ("blank.md", 'to: ""'), ("twice.md", "to: nexus\nto: home-gpu")):
            self._write(name, _command(line))
        self.assertEqual(self.processor.process_inbox(), [])
        self.assertEqual((self.calls, self._records()), ([], []))

    def test_legacy_and_nexus_addressed_commands_keep_their_handling(self):
        self._write("legacy.md", _command(None))
        self._write("nexus.md", _command("to: NEXUS"))
        self._write("both.md", _command("to: home-gpu, nexus"))
        results = self.processor.process_inbox()
        self.assertEqual(len(results), 3)
        self.assertEqual(sorted(self.calls), ["both.md", "legacy.md", "nexus.md"])

    def test_a_legacy_message_without_header_still_gets_its_diagnostic(self):
        self._write("invalid.md", "not yaml front matter")
        [result] = self.processor.process_inbox()
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
