"""Tests for modules/mailbox.py."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from modules.mailbox import (
    MAX_FILE_SIZE,
    MailboxLock,
    MailboxMessage,
    MailboxProcessor,
    _atomic_write,
    _build_outbox_content,
    _has_secret,
    _outbox_filename,
    init_mailbox_db,
    parse_front_matter,
)
from modules.run_store import init_db


def _make_repo(tmp: Path) -> Path:
    """Create a minimal git repo at *tmp*."""
    subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp),
        check=True,
        capture_output=True,
    )
    # Initial commit so HEAD exists
    readme = tmp / "README.md"
    readme.write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp),
        check=True,
        capture_output=True,
    )
    return tmp


def _make_inbox_file(inbox: Path, name: str, content: str) -> Path:
    f = inbox / name
    f.write_text(content, encoding="utf-8")
    return f


class TestParseFrontMatter(unittest.TestCase):
    def test_valid(self):
        text = "---\ntype: idea\ncreated_by: rene\n---\n\nSome body text."
        meta, body = parse_front_matter(text)
        self.assertEqual(meta["type"], "idea")
        self.assertEqual(meta["created_by"], "rene")
        self.assertIn("Some body text", body)

    def test_missing_front_matter(self):
        with self.assertRaises(ValueError):
            parse_front_matter("No front matter here")

    def test_unclosed_front_matter(self):
        with self.assertRaises(ValueError):
            parse_front_matter("---\ntype: idea\n")

    def test_invalid_line(self):
        with self.assertRaises(ValueError):
            parse_front_matter("---\ntype idea\n---\n")


class TestSecretDetection(unittest.TestCase):
    def test_sk_key(self):
        self.assertTrue(_has_secret("key: sk-abcdefghij123456"))

    def test_azure_pattern(self):
        self.assertTrue(_has_secret("AZURE_OPENAI_KEY = somevalue"))

    def test_api_key_pattern(self):
        self.assertTrue(_has_secret("API_KEY = abc123"))

    def test_clean_text(self):
        self.assertFalse(_has_secret("Just a regular idea about something."))


class TestMailboxMessageValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, content: str) -> Path:
        return _make_inbox_file(self.inbox, name, content)

    def test_valid_idea(self):
        f = self._write("idea.md", "---\ntype: idea\ncreated_by: rene\n---\n\nMy idea.")
        msg = MailboxMessage.load(f, self.inbox)
        self.assertEqual(msg.msg_type, "idea")
        self.assertEqual(msg.created_by, "rene")

    def test_valid_task(self):
        f = self._write("task.md", "---\ntype: task\ncreated_by: bot\n---\n\nDo something.")
        msg = MailboxMessage.load(f, self.inbox)
        self.assertEqual(msg.msg_type, "task")

    def test_valid_question(self):
        f = self._write("q.md", "---\ntype: question\ncreated_by: user\n---\n\nWhat is X?")
        msg = MailboxMessage.load(f, self.inbox)
        self.assertEqual(msg.msg_type, "question")

    def test_valid_context(self):
        f = self._write("ctx.md", "---\ntype: context\ncreated_by: user\n---\n\nHere is context.")
        msg = MailboxMessage.load(f, self.inbox)
        self.assertEqual(msg.msg_type, "context")

    def test_missing_type(self):
        f = self._write("bad.md", "---\ncreated_by: rene\n---\n\nBody.")
        with self.assertRaises(ValueError):
            MailboxMessage.load(f, self.inbox)

    def test_missing_created_by(self):
        f = self._write("bad.md", "---\ntype: idea\n---\n\nBody.")
        with self.assertRaises(ValueError):
            MailboxMessage.load(f, self.inbox)

    def test_unknown_type(self):
        f = self._write("bad.md", "---\ntype: unknown\ncreated_by: rene\n---\n\nBody.")
        with self.assertRaises(ValueError):
            MailboxMessage.load(f, self.inbox)

    def test_file_too_large(self):
        f = self._write("big.md", "---\ntype: idea\ncreated_by: rene\n---\n\n" + "x" * MAX_FILE_SIZE)
        with self.assertRaises(ValueError):
            MailboxMessage.load(f, self.inbox)

    def test_binary_content(self):
        f = self.inbox / "binary.md"
        f.write_bytes(b"---\ntype: idea\ncreated_by: rene\n---\n\n\x00binary\x00")
        with self.assertRaises(ValueError):
            MailboxMessage.load(f, self.inbox)

    def test_secret_rejected(self):
        f = self._write(
            "secret.md",
            "---\ntype: idea\ncreated_by: rene\n---\n\nMy API_KEY = supersecret123",
        )
        with self.assertRaises(ValueError):
            MailboxMessage.load(f, self.inbox)

    def test_symlink_rejected(self):
        real = self.inbox / "real.md"
        real.write_text("---\ntype: idea\ncreated_by: rene\n---\n\nOK")
        link = self.inbox / "link.md"
        link.symlink_to(real)
        with self.assertRaises(ValueError):
            MailboxMessage.load(link, self.inbox)

    def test_path_traversal_rejected(self):
        # Load a file outside inbox_root
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text("---\ntype: idea\ncreated_by: rene\n---\n\nBody")
        with self.assertRaises(ValueError):
            MailboxMessage.load(outside, self.inbox)


class _ProcessorTestBase(unittest.TestCase):
    """Base class that sets up a real git repo + mailbox processor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _make_repo(self.repo)

        # Set up directory structure
        (self.repo / "communication" / "inbox").mkdir(parents=True)
        (self.repo / "communication" / "outbox").mkdir(parents=True)
        (self.repo / "state").mkdir()

        # IDEAS.md
        (self.repo / "IDEAS.md").write_text(
            "# IDEAS.md\n\n## Ideas\n\n### Existing\n\n- Old idea `new`\n"
        )

        self.db_path = str(self.repo / "state" / "agent.db")
        self.processor = MailboxProcessor(
            repo_path=self.repo,
            db_path=self.db_path,
            dry_run=True,  # no network git operations in tests
        )

    def tearDown(self):
        self.processor.close()
        self.tmp.cleanup()

    @property
    def inbox(self) -> Path:
        return self.repo / "communication" / "inbox"

    @property
    def outbox(self) -> Path:
        return self.repo / "communication" / "outbox"

    def _write(self, name: str, content: str) -> Path:
        return _make_inbox_file(self.inbox, name, content)

    def _conn(self) -> sqlite3.Connection:
        return self.processor.conn


class TestIdeaProcessing(_ProcessorTestBase):
    def test_idea_appended_to_ideas_md(self):
        self._write("idea.md", "---\ntype: idea\ncreated_by: rene\n---\n\nLocal model action selection.")
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "done")

        content = (self.repo / "IDEAS.md").read_text()
        self.assertIn("Local model action selection", content)
        self.assertIn("`new`", content)
        self.assertIn("### Mailbox submissions", content)

    def test_idea_creates_outbox_file(self):
        self._write("idea.md", "---\ntype: idea\ncreated_by: rene\n---\n\nTest idea.")
        results = self.processor.process_inbox()
        # dry_run=True, outbox file not written, but result has outbox key
        self.assertIn("outbox", results[0])
        self.assertIn("idea", results[0]["outbox"])

    def test_idea_creates_db_record(self):
        self._write("idea.md", "---\ntype: idea\ncreated_by: rene\n---\n\nAnother idea.")
        self.processor.process_inbox()
        row = self._conn().execute(
            "SELECT status, msg_type, created_by FROM mailbox_messages"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "done")
        self.assertEqual(row[1], "idea")
        self.assertEqual(row[2], "rene")

    def test_idea_id_set_in_db(self):
        self._write("idea.md", "---\ntype: idea\ncreated_by: rene\n---\n\nIdea with hash.")
        self.processor.process_inbox()
        row = self._conn().execute("SELECT idea_id FROM mailbox_messages").fetchone()
        self.assertIsNotNone(row[0])
        self.assertTrue(row[0].startswith("mailbox-"))

    def test_mailbox_submissions_subsection_created_if_missing(self):
        (self.repo / "IDEAS.md").write_text("# IDEAS.md\n\n## Ideas\n\n- existing `new`\n")
        self._write("idea.md", "---\ntype: idea\ncreated_by: rene\n---\n\nNew thing.")
        self.processor.process_inbox()
        content = (self.repo / "IDEAS.md").read_text()
        self.assertIn("### Mailbox submissions", content)

    def test_second_idea_appended_under_existing_subsection(self):
        (self.repo / "IDEAS.md").write_text(
            "# IDEAS.md\n\n## Ideas\n\n### Mailbox submissions\n\n- First idea `new`\n"
        )
        self._write("idea2.md", "---\ntype: idea\ncreated_by: rene\n---\n\nSecond idea.")
        self.processor.process_inbox()
        content = (self.repo / "IDEAS.md").read_text()
        self.assertEqual(content.count("### Mailbox submissions"), 1)
        self.assertIn("Second idea", content)


class TestTaskProcessing(_ProcessorTestBase):
    def test_task_creates_db_record(self):
        self._write("task.md", "---\ntype: task\ncreated_by: bot\n---\n\nRun something.")
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "done")
        row = self._conn().execute("SELECT status, msg_type FROM mailbox_messages").fetchone()
        self.assertEqual(row[0], "done")
        self.assertEqual(row[1], "task")

    def test_task_outbox_mentions_review(self):
        self._write("task.md", "---\ntype: task\ncreated_by: bot\n---\n\nDo X.")
        self.processor.process_inbox()
        row = self._conn().execute("SELECT outbox_file FROM mailbox_messages").fetchone()
        self.assertIsNotNone(row[0])


class TestQuestionProcessing(_ProcessorTestBase):
    def test_question_acknowledged(self):
        self._write("q.md", "---\ntype: question\ncreated_by: user\n---\n\nWhat is Y?")
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "done")


class TestContextProcessing(_ProcessorTestBase):
    def test_context_stored_in_knowledge(self):
        self._write("ctx.md", "---\ntype: context\ncreated_by: user\n---\n\nImportant context here.")
        self.processor.process_inbox()
        row = self._conn().execute(
            "SELECT category, key, value FROM knowledge WHERE category='mailbox_context'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "mailbox_context")
        self.assertIn("Important context", row[2])


class TestDuplicatePrevention(_ProcessorTestBase):
    def test_processed_message_not_reprocessed(self):
        self._write("idea.md", "---\ntype: idea\ncreated_by: rene\n---\n\nIdea once.")
        # First run
        r1 = self.processor.process_inbox()
        self.assertEqual(r1[0]["status"], "done")
        # Second run — same file
        r2 = self.processor.process_inbox()
        self.assertEqual(r2[0]["status"], "skipped")
        self.assertEqual(r2[0]["reason"], "already done")

        # Only one DB record
        count = self._conn().execute("SELECT COUNT(*) FROM mailbox_messages").fetchone()[0]
        self.assertEqual(count, 1)


class TestRetryCounter(_ProcessorTestBase):
    def test_retry_count_increments_on_failure(self):
        # Write a malformed file that will fail (missing front matter) — but we
        # need to insert a failed record manually since load() validates first.
        # Simulate by inserting a failed record, then re-running.
        f = self._write("bad.md", "---\ntype: idea\ncreated_by: rene\n---\n\nOK.")
        # Manually insert a 'failed' record
        self._conn().execute(
            """
            INSERT INTO mailbox_messages
                (file_path, source_commit_sha, msg_type, created_by, status, received_at, retry_count)
            VALUES (?, 'sha', 'idea', 'rene', 'failed', '2026-01-01T00:00:00Z', 0)
            """,
            (str(f),),
        )
        self._conn().commit()
        # Process — should retry and succeed, incrementing retry_count
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "done")
        row = self._conn().execute("SELECT retry_count FROM mailbox_messages").fetchone()
        self.assertGreaterEqual(row[0], 1)

    def test_max_retries_prevents_processing(self):
        f = self._write("bad.md", "---\ntype: idea\ncreated_by: rene\n---\n\nOK.")
        self._conn().execute(
            """
            INSERT INTO mailbox_messages
                (file_path, source_commit_sha, msg_type, created_by, status, received_at, retry_count)
            VALUES (?, 'sha', 'idea', 'rene', 'failed', '2026-01-01T00:00:00Z', 3)
            """,
            (str(f),),
        )
        self._conn().commit()
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "skipped")
        self.assertIn("max retries", results[0]["reason"])


class TestLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.tmp.name) / "mailbox.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_lock_acquired_and_released(self):
        lock = MailboxLock(self.lock_path)
        self.assertTrue(lock.acquire())
        lock.release()

    def test_second_lock_fails(self):
        lock1 = MailboxLock(self.lock_path)
        lock2 = MailboxLock(self.lock_path)
        self.assertTrue(lock1.acquire())
        try:
            self.assertFalse(lock2.acquire())
        finally:
            lock1.release()

    def test_context_manager(self):
        with MailboxLock(self.lock_path):
            # Should be locked now
            lock2 = MailboxLock(self.lock_path)
            self.assertFalse(lock2.acquire())

    def test_context_manager_raises_if_locked(self):
        lock1 = MailboxLock(self.lock_path)
        lock1.acquire()
        try:
            with self.assertRaises(RuntimeError):
                with MailboxLock(self.lock_path):
                    pass
        finally:
            lock1.release()

    def test_lock_released_after_context_exit(self):
        with MailboxLock(self.lock_path):
            pass
        # Should be acquirable again
        lock = MailboxLock(self.lock_path)
        self.assertTrue(lock.acquire())
        lock.release()


class TestMalformedMessages(_ProcessorTestBase):
    def test_no_front_matter_fails(self):
        self._write("bad.md", "No front matter at all.")
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "failed")

    def test_db_records_failure(self):
        self._write("bad.md", "No front matter.")
        self.processor.process_inbox()
        row = self._conn().execute(
            "SELECT status, last_error FROM mailbox_messages"
        ).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIsNotNone(row[1])


class TestDatabaseInitialization(unittest.TestCase):
    def test_mailbox_table_created_by_init_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "agent.db")
            conn = init_db(db_path)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("mailbox_messages", tables)
            conn.close()


if __name__ == "__main__":
    unittest.main()
