"""Tests for mailbox retention (Issue #129).

The mailbox never drained: outbox responses accumulated forever, and an
inbox message that exhausted its retries stayed on disk and was re-skipped
on every poll. Pruning fixes both - but must never delete a file that a
publisher or the commit-retry path still needs.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from modules.mailbox import MailboxProcessor, init_mailbox_db
from modules.mailbox.config import MailboxConfig
from modules.mailbox.core import MAX_RETRY, STATUS_DONE, STATUS_DONE_PENDING_COMMIT
from modules.mailbox import retention
from modules.run_store import init_db

os.environ.setdefault("AGENT_API_KEY", "test-key")

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y%m%d%H%M%S")


class RetentionPolicyTests(unittest.TestCase):
    """The decision of what to delete, independent of the filesystem."""

    def test_window_is_configurable_and_zero_disables_pruning(self):
        self.assertEqual(retention.retention_days(None), retention.DEFAULT_RETENTION_DAYS)
        self.assertEqual(retention.retention_days("30"), 30)
        self.assertEqual(retention.retention_days("0"), 0)

    def test_invalid_window_disables_pruning_rather_than_guessing(self):
        """A typo in the environment must not silently start deleting."""
        self.assertEqual(retention.retention_days("five"), 0)
        self.assertEqual(retention.retention_days("-3"), 0)

    def test_timestamp_is_parsed_from_the_outbox_filename(self):
        parsed = retention.message_timestamp("20260906114422-command-48ff369c.md")
        self.assertEqual(parsed, datetime(2026, 9, 6, 11, 44, 22, tzinfo=timezone.utc))

    def test_unrecognized_filenames_have_no_age_and_are_left_alone(self):
        for name in ("README.md", "notes.md", "2026-command.md"):
            with self.subTest(name=name):
                self.assertIsNone(retention.message_timestamp(name))


class _MailboxRepoFixture:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "mailbox"
        self.inbox = self.repo / "inbox"
        self.outbox = self.repo / "outbox"
        self.inbox.mkdir(parents=True)
        self.outbox.mkdir(parents=True)
        _git(self.repo.parent, "init", str(self.repo))
        _git(self.repo, "config", "user.email", "test@test.com")
        _git(self.repo, "config", "user.name", "Test")
        (self.inbox / ".gitkeep").write_text("")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")

        self.db_path = str(Path(self._tmp.name) / "agent.db")
        self.conn = init_db(self.db_path)
        init_mailbox_db(self.conn)
        self.addCleanup(self.conn.close)

    def _outbox_file(self, days_ago: float, suffix: str = "command-abc") -> Path:
        path = self.outbox / f"{_stamp(days_ago)}-{suffix}.md"
        path.write_text("response\n", encoding="utf-8")
        return path

    def _message_row(self, *, outbox_file: str, status: str) -> None:
        self.conn.execute(
            """
            INSERT INTO mailbox_messages
                (file_path, source_commit_sha, msg_type, created_by, status,
                 received_at, outbox_file)
            VALUES (?, 'sha', 'command', 'tester', ?, '2026-09-01T00:00:00Z', ?)
            """,
            (str(self.inbox / f"{outbox_file}.in"), status, outbox_file),
        )
        self.conn.commit()

    def _plan(self, days: int = 5):
        return retention.plan_prune(
            self.conn, inbox=self.inbox, outbox=self.outbox, days=days, now=NOW,
        )


class PruneTests(_MailboxRepoFixture, unittest.TestCase):
    """What the policy selects, given real files and real DB rows."""

    def test_outbox_older_than_the_window_is_pruned(self):
        old = self._outbox_file(days_ago=9)
        recent = self._outbox_file(days_ago=2, suffix="command-def")

        plan = self._plan()

        self.assertEqual([p.name for p in plan.outbox], [old.name])
        self.assertNotIn(recent.name, [p.name for p in plan.outbox])

    def test_boundary_file_inside_the_window_is_kept(self):
        self._outbox_file(days_ago=4.9)
        self.assertEqual(self._plan().outbox, [])

    def test_pruning_is_disabled_when_the_window_is_zero(self):
        self._outbox_file(days_ago=90)
        self.assertFalse(self._plan(days=0))

    def test_file_still_needed_to_finalize_a_commit_is_never_pruned(self):
        """The hard constraint: retry_pending_git_commits() re-reads this
        file, so deleting it would strand the row forever."""
        pending = self._outbox_file(days_ago=30)
        self._message_row(outbox_file=pending.name, status=STATUS_DONE_PENDING_COMMIT)

        plan = self._plan()

        self.assertEqual(plan.outbox, [])
        self.assertEqual(plan.protected, 1)

    def test_the_same_file_is_pruned_once_its_row_is_done(self):
        done = self._outbox_file(days_ago=30)
        self._message_row(outbox_file=done.name, status=STATUS_DONE)

        self.assertEqual([p.name for p in self._plan().outbox], [done.name])

    def test_unpublished_operation_chain_and_approval_files_are_protected(self):
        chain = self._outbox_file(days_ago=30, suffix="operation-chain-result-x")
        approval = self._outbox_file(days_ago=30, suffix="task-approval-y")
        pushed = self._outbox_file(days_ago=30, suffix="task-approval-z")
        self.conn.execute(
            "INSERT INTO operation_chains (request_id, outbox_file, publish_status, "
            "status, request_json, created_at, updated_at) "
            "VALUES (?, ?, 'written', 'succeeded', '{}', ?, ?)",
            ("req-1", chain.name, "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"),
        )
        parent = self.conn.execute(
            """
            INSERT INTO mailbox_messages
                (file_path, source_commit_sha, msg_type, created_by, status, received_at)
            VALUES ('task.md', 'sha', 'task', 'tester', 'done', '2026-08-01T00:00:00Z')
            """
        ).lastrowid
        for approval_id, name, status in (
            ("ap-1", approval.name, "committed"),
            ("ap-2", pushed.name, "pushed"),
        ):
            self.conn.execute(
                """
                INSERT INTO mailbox_task_approvals
                    (id, mailbox_msg_id, task_body, sender, file_path, received_at,
                     status, created_at, updated_at, outbox_file, publish_status)
                VALUES (?, ?, 'body', 'tester', 'f', '2026-08-01T00:00:00Z',
                        'approved', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z', ?, ?)
                """,
                (approval_id, parent, name, status),
            )
        self.conn.commit()

        pruned = [p.name for p in self._plan().outbox]

        self.assertNotIn(chain.name, pruned)
        self.assertNotIn(approval.name, pruned)
        # A fully pushed record no longer needs its file.
        self.assertIn(pushed.name, pruned)

    def test_inbox_messages_that_exhausted_their_retries_are_dropped(self):
        stuck = self.inbox / "202608291430-read-chat-job-chatgpt.md"
        stuck.write_text("---\ntype: command\ncreated_by: t\n---\nbody\n", encoding="utf-8")
        self.conn.execute(
            """
            INSERT INTO mailbox_messages
                (file_path, source_commit_sha, msg_type, created_by, status,
                 received_at, retry_count, last_error)
            VALUES (?, 'sha', 'command', 'chatgpt', 'failed',
                    '2026-08-29T14:30:00Z', ?, 'boom')
            """,
            (str(stuck), MAX_RETRY),
        )
        self.conn.commit()

        self.assertEqual([p.name for p in self._plan().inbox], [stuck.name])

    def test_inbox_messages_with_retries_left_are_kept(self):
        retrying = self.inbox / "20260907-1000-live-chatgpt.md"
        retrying.write_text("body\n", encoding="utf-8")
        self.conn.execute(
            """
            INSERT INTO mailbox_messages
                (file_path, source_commit_sha, msg_type, created_by, status,
                 received_at, retry_count)
            VALUES (?, 'sha', 'command', 'chatgpt', 'failed', '2026-09-07T10:00:00Z', ?)
            """,
            (str(retrying), MAX_RETRY - 1),
        )
        self.conn.commit()

        self.assertEqual(self._plan().inbox, [])

    def test_deletions_are_bounded_per_poll(self):
        for index in range(12):
            self._outbox_file(days_ago=30, suffix=f"command-{index:03d}")

        plan = retention.plan_prune(
            self.conn, inbox=self.inbox, outbox=self.outbox, days=5, now=NOW,
            max_deletions=5,
        )

        self.assertEqual(len(plan.outbox), 5)
        self.assertTrue(plan.truncated)


class PruneCommitTests(_MailboxRepoFixture, unittest.TestCase):
    """The processor's side: delete, commit with explicit pathspecs, and
    stay silent when there is nothing to do."""

    def _processor(self, dry_run: bool = False) -> MailboxProcessor:
        code_repo = Path(self._tmp.name) / "code"
        code_repo.mkdir(exist_ok=True)
        config = MailboxConfig.from_paths(code_repo, self.repo)
        processor = MailboxProcessor(
            repo_path=self.repo,
            db_path=self.db_path,
            dry_run=dry_run,
            mailbox_config=config,
            task_log_dir=Path(self._tmp.name) / "logs",
        )
        self.addCleanup(processor.close)
        return processor

    def _head_subject(self) -> str:
        return subprocess.run(
            ["git", "log", "-1", "--pretty=%s"], cwd=str(self.repo),
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def _commit_count(self) -> int:
        return int(subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=str(self.repo),
            capture_output=True, text=True, check=True,
        ).stdout.strip())

    def test_pruning_deletes_the_files_and_commits_them(self):
        old = self._outbox_file(days_ago=30)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "add outbox")
        before = self._commit_count()

        with patch.dict(os.environ, {retention.ENV_RETENTION_DAYS: "5"}):
            self._processor().prune_old_messages(now=NOW)

        self.assertFalse(old.exists())
        self.assertEqual(self._commit_count(), before + 1)
        self.assertIn("prune", self._head_subject())

    def test_a_poll_with_nothing_to_prune_makes_no_commit(self):
        self._outbox_file(days_ago=1)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "add outbox")
        before = self._commit_count()

        with patch.dict(os.environ, {retention.ENV_RETENTION_DAYS: "5"}):
            self._processor().prune_old_messages(now=NOW)

        self.assertEqual(self._commit_count(), before)

    def test_repeated_pruning_is_idempotent(self):
        self._outbox_file(days_ago=30)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "add outbox")

        with patch.dict(os.environ, {retention.ENV_RETENTION_DAYS: "5"}):
            processor = self._processor()
            processor.prune_old_messages(now=NOW)
            after_first = self._commit_count()
            processor.prune_old_messages(now=NOW)

        self.assertEqual(self._commit_count(), after_first)

    def test_dry_run_deletes_nothing(self):
        old = self._outbox_file(days_ago=30)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "add outbox")

        with patch.dict(os.environ, {retention.ENV_RETENTION_DAYS: "5"}):
            self._processor(dry_run=True).prune_old_messages(now=NOW)

        self.assertTrue(old.exists())

    def test_a_discarded_inbox_message_is_logged_before_it_is_deleted(self):
        stuck = self.inbox / "202608291430-read-chat-job-chatgpt.md"
        stuck.write_text("body\n", encoding="utf-8")
        self.conn.execute(
            """
            INSERT INTO mailbox_messages
                (file_path, source_commit_sha, msg_type, created_by, status,
                 received_at, retry_count, last_error)
            VALUES (?, 'sha', 'command', 'chatgpt', 'failed',
                    '2026-08-29T14:30:00Z', ?, 'exit code 255')
            """,
            (str(stuck), MAX_RETRY),
        )
        self.conn.commit()
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "add inbox")

        with patch.dict(os.environ, {retention.ENV_RETENTION_DAYS: "5"}):
            self._processor().prune_old_messages(now=NOW)

        self.assertFalse(stuck.exists())
        # The reason survives the file, so a trace can still explain it.
        logged = [
            json.loads(line)
            for path in (Path(self._tmp.name) / "logs").glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        discarded = [e for e in logged if e.get("event") == "message_discarded"]
        self.assertEqual(len(discarded), 1)
        self.assertIn("exit code 255", json.dumps(discarded[0]))


if __name__ == "__main__":
    unittest.main()
