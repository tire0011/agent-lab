from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.mailbox.config import MailboxConfig
from modules.outbox_message_publisher import (
    OutboxMessage,
    TaskApprovalMessagePublisher,
)
from modules.outbox_publisher import PUBLISH_FAILED, PUBLISH_PUSHED
from modules.run_store import init_db


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "init"], path)


class TestCanonicalOutboxMessagePublisher(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = tempfile.TemporaryDirectory()
        root = Path(self.ctx.name)
        self.code_repo = root / "code"
        self.mailbox_repo = root / "mailbox"
        self.remote = root / "remote.git"
        _init_repo(self.code_repo)
        _init_repo(self.mailbox_repo)
        (self.mailbox_repo / "inbox").mkdir()
        (self.mailbox_repo / "outbox").mkdir()
        _git(["add", "-A"], self.mailbox_repo)
        _git(["commit", "--allow-empty", "-m", "mailbox dirs"], self.mailbox_repo)
        _git(["init", "--bare", str(self.remote)], root)
        _git(["remote", "add", "origin", str(self.remote)], self.mailbox_repo)
        branch = _git(["branch", "--show-current"], self.mailbox_repo).stdout.strip()
        _git(["push", "-u", "origin", branch], self.mailbox_repo)
        self.cfg = MailboxConfig.from_paths(self.code_repo, self.mailbox_repo)
        self.db_path = str(root / "state.db")

    def tearDown(self) -> None:
        self.ctx.cleanup()

    def _insert_failed_task(self, task_id: str, content: str) -> None:
        conn = init_db(self.db_path)
        conn.execute(
            """INSERT INTO mailbox_messages
               (file_path, source_commit_sha, msg_type, created_by, status, received_at)
               VALUES ('inbox/task.md', 'abc', 'task', 'test', 'processed',
                       '2026-09-15T00:00:00Z')"""
        )
        msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO mailbox_task_approvals
               (id, mailbox_msg_id, task_body, sender, file_path, received_at,
                status, outbox_file, outbox_content, publish_status,
                publish_retry_count, created_at, updated_at)
               VALUES (?, ?, 'body', 'test', 'inbox/task.md',
                       '2026-09-15T00:00:00Z', 'succeeded', ?, ?, ?, 3,
                       '2026-09-15T00:00:00Z', '2026-09-15T00:00:00Z')""",
            (task_id, msg_id, "../wrong/outbox.md", content, PUBLISH_FAILED),
        )
        conn.commit()
        conn.close()

    def test_failed_legacy_path_is_replaced_by_publisher_owned_path(self) -> None:
        task_id = "7a2bc0c1c70f421cb2fc77fcfdac6aad"
        content = "---\nstatus: done\n---\n\nresult\n"
        self._insert_failed_task(task_id, content)

        publisher = TaskApprovalMessagePublisher(
            mailbox_config=self.cfg,
            db_path=self.db_path,
        )
        try:
            self.assertEqual(publisher.publish_pending(), 1)
        finally:
            publisher.close()

        conn = init_db(self.db_path)
        row = conn.execute(
            "SELECT outbox_file, publish_status FROM mailbox_task_approvals WHERE id=?",
            (task_id,),
        ).fetchone()
        conn.close()

        outbox_file, status = row
        self.assertEqual(status, PUBLISH_PUSHED)
        self.assertNotIn("..", outbox_file)
        self.assertTrue(outbox_file.startswith("outbox/"))
        self.assertEqual((self.mailbox_repo / outbox_file).read_text(encoding="utf-8"), content)
        self.assertFalse((self.mailbox_repo.parent / "wrong" / "outbox.md").exists())

    def test_logical_id_never_becomes_a_path(self) -> None:
        publisher = TaskApprovalMessagePublisher(
            mailbox_config=self.cfg,
            db_path=self.db_path,
        )
        try:
            rel = publisher._canonical_relative_path("../../etc/passwd")
        finally:
            publisher.close()

        self.assertTrue(rel.startswith("outbox/message-"))
        self.assertTrue(rel.endswith(".md"))
        self.assertNotIn("..", rel)
        self.assertNotIn("passwd", rel)

    def test_outbox_message_contains_only_identity_and_content(self) -> None:
        message = OutboxMessage(id="task-123", content="hello")
        self.assertEqual(message.id, "task-123")
        self.assertEqual(message.content, "hello")
        self.assertEqual(message.__slots__, ("id", "content"))


if __name__ == "__main__":
    unittest.main()
