from __future__ import annotations

import unittest

from modules.outbox_publisher import PUBLISH_FAILED, PUBLISH_PUSHED, TaskApprovalOutboxPublisher
from modules.run_store import init_db
from tests.test_outbox_publisher import (
    _external_config,
    _get_approval,
    _make_repo,
    _make_repo_with_remote,
    _remote_head,
)


class TestIssue180StuckOutboxRecovery(unittest.TestCase):
    """Regression for the live `outbox path escapes outbox directory` failure."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.ctx = tempfile.TemporaryDirectory()
        root = Path(self.ctx.name)
        self.code_repo = root / "code"
        _make_repo(self.code_repo)
        self.mailbox_repo, self.remote = _make_repo_with_remote(
            root, "mailbox-remote.git", "mailbox"
        )
        (self.mailbox_repo / "outbox").mkdir(exist_ok=True)
        self.config = _external_config(self.code_repo, self.mailbox_repo)
        self.db_path = str(root / "state.db")

    def tearDown(self) -> None:
        self.ctx.cleanup()

    def test_failed_legacy_single_repo_path_rebuilds_canonical_outbox_file(self) -> None:
        task_id = "7a2bc0c1c70f421cb2fc77fcfdac6aad"
        content = "---\nstatus: done\n---\n\nPR #116 integration result\n"
        legacy_path = "communication/outbox/task-result.md"

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
                publish_retry_count, publish_last_error, created_at, updated_at)
               VALUES (?, ?, 'body', 'test', 'inbox/task.md',
                       '2026-09-15T00:00:00Z', 'succeeded', ?, ?, ?, 9, ?,
                       '2026-09-15T00:00:00Z', '2026-09-15T00:00:00Z')""",
            (
                task_id,
                msg_id,
                legacy_path,
                content,
                PUBLISH_FAILED,
                "outbox path escapes outbox directory",
            ),
        )
        conn.commit()
        conn.close()

        publisher = TaskApprovalOutboxPublisher(
            mailbox_config=self.config,
            db_path=self.db_path,
        )
        try:
            self.assertEqual(publisher.publish_pending(), 1)
        finally:
            publisher.close()

        row = _get_approval(self.db_path, task_id)
        outbox_file = row["outbox_file"]
        self.assertEqual(row["publish_status"], PUBLISH_PUSHED)
        self.assertTrue(outbox_file.startswith("outbox/task-approval-result-"))
        self.assertNotEqual(outbox_file, legacy_path)
        self.assertNotIn("..", outbox_file)
        self.assertEqual((self.mailbox_repo / outbox_file).read_text(encoding="utf-8"), content)
        self.assertEqual(_remote_head(self.remote, outbox_file), content)
        self.assertFalse((self.code_repo / outbox_file).exists())


if __name__ == "__main__":
    unittest.main()
