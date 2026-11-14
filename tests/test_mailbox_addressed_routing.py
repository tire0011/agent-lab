"""Regression tests for addressed routing in the Nexus mailbox processor
(agent-lab companion of tire0011/agent-lab-mailbox issue #7).

A message positively addressed to another agent (``to: home-gpu``) must be
skipped BEFORE any side effect: no task log, no DB row, no retry counter, no
trust evaluation, no command execution, no outbox entry, no deletion - it stays
untouched in ``inbox/``. Legacy messages without ``to`` keep their exact
previous behavior.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.mailbox import MailboxProcessor, is_addressed_to, recipients_of
from modules.mailbox import development as development_module
import modules.mailbox as mailbox_module


def _make_repo(tmp: Path) -> Path:
    subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "49699333+tire0011@users.noreply.github.com"],
        cwd=str(tmp), check=True, capture_output=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp), check=True, capture_output=True)
    (tmp / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp), check=True, capture_output=True)
    return tmp


def _make_inbox_file(inbox: Path, name: str, content: str) -> Path:
    f = inbox / name
    f.write_text(content, encoding="utf-8")
    repo = inbox.parent
    subprocess.run(["git", "add", f"inbox/{name}"], cwd=str(repo), check=False, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"add inbox {name}"], cwd=str(repo), check=False, capture_output=True)
    return f


class AddressedRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _make_repo(Path(self.tmp.name))
        (self.repo / "inbox").mkdir()
        (self.repo / "outbox").mkdir()
        (self.repo / "state").mkdir()
        (self.repo / "IDEAS.md").write_text("# IDEAS.md\n\n## Ideas\n\n### Existing\n\n- Old idea `new`\n")
        self.db_path = str(self.repo / "state" / "agent.db")
        self.task_log_dir = self.repo / "state" / "experiment-logs"
        self.processor = MailboxProcessor(
            repo_path=self.repo,
            db_path=self.db_path,
            dry_run=True,
            task_log_dir=self.task_log_dir,
        )
        self.addCleanup(self.processor.close)
        development_module.install(mailbox_module)

    @property
    def inbox(self) -> Path:
        return self.repo / "inbox"

    @property
    def outbox(self) -> Path:
        return self.repo / "outbox"

    def _db_count(self) -> int:
        return int(self.processor.conn.execute("SELECT COUNT(*) FROM mailbox_messages").fetchone()[0])

    def test_foreign_command_skipped_before_any_side_effect(self):
        _make_inbox_file(
            self.inbox,
            "foreign-command.md",
            "---\ntype: command\ncreated_by: rene\nto: home-gpu\n---\n\necho pwned-marker",
        )
        with patch.object(development_module, "process_command") as mock_cmd:
            mailbox_module._PROCESSORS["command"] = mock_cmd
            results = self.processor.process_inbox()
        # skipped, not processed
        self.assertEqual(results[0]["status"], "skipped")
        self.assertIn("not addressed", results[0]["reason"])
        # no DB row, no task log, no outbox, file untouched, processor never invoked
        self.assertEqual(self._db_count(), 0)
        self.assertFalse(self.task_log_dir.exists() and any(self.task_log_dir.iterdir()))
        self.assertEqual(list(self.outbox.iterdir()), [])
        self.assertTrue((self.inbox / "foreign-command.md").exists())
        mock_cmd.assert_not_called()

    def test_foreign_command_never_reaches_process_command(self):
        _make_inbox_file(
            self.inbox,
            "foreign-command2.md",
            "---\ntype: command\ncreated_by: rene\nto: home-gpu\ntimeout: 5\n---\n\necho pwned-marker",
        )
        with patch.object(development_module, "process_command") as mock_cmd:
            mailbox_module._PROCESSORS["command"] = mock_cmd
            self.processor.process_inbox()
        mock_cmd.assert_not_called()
        self.assertEqual(self._db_count(), 0)

    def test_legacy_no_to_control_still_processed(self):
        _make_inbox_file(
            self.inbox, "legacy-idea.md",
            "---\ntype: idea\ncreated_by: rene\n---\n\nLegacy idea without to field.",
        )
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(self._db_count(), 1)
        self.assertIn("Legacy idea without to field.", (self.repo / "IDEAS.md").read_text())

    def test_explicit_to_nexus_processed(self):
        _make_inbox_file(
            self.inbox, "to-nexus.md",
            "---\ntype: idea\ncreated_by: rene\nto: nexus\n---\n\nExplicit nexus idea.",
        )
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(self._db_count(), 1)

    def test_foreign_idea_skipped_without_db_row(self):
        _make_inbox_file(
            self.inbox, "foreign-idea.md",
            "---\ntype: idea\ncreated_by: rene\nto: home-gpu\n---\n\nForeign idea.",
        )
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "skipped")
        self.assertEqual(self._db_count(), 0)
        self.assertTrue((self.inbox / "foreign-idea.md").exists())
        self.assertNotIn("Foreign idea.", (self.repo / "IDEAS.md").read_text())

    def test_empty_to_matches_nobody_and_is_untouched(self):
        _make_inbox_file(
            self.inbox, "empty-to.md",
            "---\ntype: idea\ncreated_by: rene\nto:\n---\n\nEmpty to idea.",
        )
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "skipped")
        self.assertEqual(self._db_count(), 0)
        self.assertTrue((self.inbox / "empty-to.md").exists())

    def test_unparseable_message_keeps_legacy_rejection(self):
        _make_inbox_file(self.inbox, "broken.md", "not yaml front matter")
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "failed")

    def test_custom_agent_id_processor(self):
        processor = MailboxProcessor(
            repo_path=self.repo,
            db_path=str(self.repo / "state" / "gpu.db"),
            dry_run=True,
            task_log_dir=self.repo / "state" / "gpu-logs",
            agent_id="home-gpu",
        )
        self.addCleanup(processor.close)
        _make_inbox_file(
            self.inbox, "gpu-idea.md",
            "---\ntype: idea\ncreated_by: rene\nto: home-gpu\n---\n\nGPU idea.",
        )
        _make_inbox_file(
            self.inbox, "legacy2.md",
            "---\ntype: idea\ncreated_by: rene\n---\n\nLegacy idea two.",
        )
        results = {r["file"]: r["status"] for r in processor.process_inbox()}
        self.assertEqual(results["inbox/gpu-idea.md"], "done")
        # legacy (no to) is addressed to nexus only, not to this worker
        self.assertEqual(results["inbox/legacy2.md"], "skipped")
        self.assertTrue((self.inbox / "legacy2.md").exists())

    def test_routing_helpers(self):
        self.assertIsNone(recipients_of({"type": "task"}))
        self.assertTrue(is_addressed_to("nexus", {"type": "task"}))
        self.assertFalse(is_addressed_to("home-gpu", {"type": "task"}))
        self.assertEqual(recipients_of({"to": "home-gpu"}), frozenset({"home-gpu"}))
        self.assertEqual(recipients_of({"to": "A, b"}), frozenset({"a", "b"}))
        self.assertEqual(recipients_of({"to": ""}), frozenset())
        self.assertTrue(is_addressed_to("nexus", {"to": "home-gpu, NEXUS"}))
        self.assertFalse(is_addressed_to("nexus", {"to": "home-gpu"}))
        self.assertFalse(is_addressed_to("nexus", {"to": ""}))


if __name__ == "__main__":
    unittest.main()
