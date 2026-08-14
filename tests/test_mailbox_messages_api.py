"""Tests for GET /mailbox/messages (public, sanitized inbox/outbox listing)."""

import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from api import get_codex_service, get_experiment_service, get_mailbox_task_service
from modules.mailbox import init_mailbox_db

SECRET = "sk-abcdefghijklmnop"


def _mock_codex_service():
    svc = MagicMock()
    svc.mark_interrupted_tasks_failed.return_value = None
    return svc


def _mock_mailbox_task_service():
    svc = MagicMock()
    svc.recover_after_restart.return_value = None
    return svc


class MailboxMessagesApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_dir = Path(self.temp_dir.name)
        self.outbox_dir = self.repo_dir / "communication" / "outbox"
        self.inbox_dir = self.repo_dir / "communication" / "inbox"
        self.outbox_dir.mkdir(parents=True)
        self.inbox_dir.mkdir(parents=True)

        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        init_mailbox_db(self.conn)

        self.experiment_service = MagicMock()
        self.experiment_service._conn = self.conn

        api.app.dependency_overrides[get_codex_service] = _mock_codex_service
        api.app.dependency_overrides[get_mailbox_task_service] = _mock_mailbox_task_service
        api.app.dependency_overrides[get_experiment_service] = lambda: self.experiment_service
        self.addCleanup(api.app.dependency_overrides.clear)

        self._orig_outbox_dir = api._OUTBOX_DIR
        api._OUTBOX_DIR = self.outbox_dir
        self.addCleanup(self._restore_outbox_dir)

        self.client = TestClient(api.app)

    def _restore_outbox_dir(self):
        api._OUTBOX_DIR = self._orig_outbox_dir

    def _insert_message(self, **overrides):
        values = {
            "file_path": str(self.inbox_dir / "msg.md"),
            "source_commit_sha": "abc123",
            "msg_type": "task",
            "created_by": "rene",
            "status": "done",
            "received_at": "2026-08-13T09:00:00Z",
            "processed_at": "2026-08-13T09:01:00Z",
            "outbox_file": None,
        }
        values.update(overrides)
        self.conn.execute(
            """
            INSERT INTO mailbox_messages
                (file_path, source_commit_sha, msg_type, created_by, status,
                 received_at, processed_at, outbox_file)
            VALUES (:file_path, :source_commit_sha, :msg_type, :created_by, :status,
                    :received_at, :processed_at, :outbox_file)
            """,
            values,
        )
        self.conn.commit()

    def _write_outbox(self, name, *, type_="task", status="done", created_by="rene",
                       processed_at="2026-08-13T09:01:00Z", body="## Result\n\nAll good."):
        content = (
            "---\n"
            f"type: {type_}\n"
            f"status: {status}\n"
            f"processed_at: {processed_at}\n"
            f"created_by: {created_by}\n"
            "---\n\n" + body + "\n"
        )
        (self.outbox_dir / name).write_text(content, encoding="utf-8")

    def test_requires_no_api_key(self):
        response = self.client.get("/mailbox/messages")
        self.assertEqual(response.status_code, 200)

    def test_empty_when_no_data(self):
        response = self.client.get("/mailbox/messages")
        body = response.json()
        self.assertEqual(body["inbox_count"], 0)
        self.assertEqual(body["outbox_count"], 0)
        self.assertEqual(body["inbox"], [])
        self.assertEqual(body["outbox"], [])

    def test_inbox_lists_db_messages_newest_first(self):
        self._insert_message(received_at="2026-08-13T09:00:00Z", msg_type="idea", created_by="alice")
        self._insert_message(received_at="2026-08-13T10:00:00Z", msg_type="task", created_by="bob")
        response = self.client.get("/mailbox/messages")
        body = response.json()
        self.assertEqual(body["inbox_count"], 2)
        self.assertEqual(body["inbox"][0]["sender"], "bob")
        self.assertEqual(body["inbox"][0]["type"], "task")
        self.assertEqual(body["inbox"][0]["title"], "Auftrag")
        self.assertEqual(body["inbox"][1]["sender"], "alice")

    def test_inbox_preview_none_once_processed_file_deleted(self):
        # File referenced by file_path no longer exists (already processed).
        self._insert_message()
        response = self.client.get("/mailbox/messages")
        entry = response.json()["inbox"][0]
        self.assertIsNone(entry["preview"])

    def test_inbox_preview_available_for_still_pending_file(self):
        pending = self.inbox_dir / "pending.md"
        pending.write_text(
            "---\ntype: task\ncreated_by: rene\n---\n\nPlease do the thing.\n",
            encoding="utf-8",
        )
        self._insert_message(file_path=str(pending), status="processing")
        response = self.client.get("/mailbox/messages")
        entry = response.json()["inbox"][0]
        self.assertIn("Please do the thing.", entry["preview"])

    def test_outbox_lists_files_newest_first_with_sanitized_preview(self):
        self._write_outbox(
            "20260813090100-task-aaa.md",
            processed_at="2026-08-13T09:01:00Z",
            body=f"## Result\n\nDone. token={SECRET} /agent/private/file.py",
        )
        self._write_outbox(
            "20260813100000-idea-bbb.md",
            type_="idea",
            processed_at="2026-08-13T10:00:00Z",
            body="## Result\n\nIdea stored.",
        )
        response = self.client.get("/mailbox/messages")
        body = response.json()
        self.assertEqual(body["outbox_count"], 2)
        self.assertEqual(body["outbox"][0]["type"], "idea")
        self.assertEqual(body["outbox"][1]["type"], "task")
        preview = body["outbox"][1]["preview"]
        self.assertNotIn(SECRET, preview)
        self.assertNotIn("/agent", preview)

    def test_sender_with_unsafe_characters_is_omitted(self):
        self._insert_message(created_by="rene; DROP TABLE x")
        response = self.client.get("/mailbox/messages")
        entry = response.json()["inbox"][0]
        self.assertIsNone(entry["sender"])

    def test_limit_query_param_bounds_results(self):
        for i in range(5):
            self._insert_message(received_at=f"2026-08-13T09:0{i}:00Z")
        response = self.client.get("/mailbox/messages?limit=2")
        self.assertEqual(len(response.json()["inbox"]), 2)

    def test_never_leaks_absolute_paths_or_secrets(self):
        self._insert_message(file_path=str(self.inbox_dir / "x.md"))
        self._write_outbox(
            "20260813090100-task-ccc.md",
            body=f"## Result\n\nAPI_KEY={SECRET} at /agent/state/secrets.env",
        )
        response = self.client.get("/mailbox/messages")
        serialized = response.text
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn(str(self.repo_dir), serialized)


if __name__ == "__main__":
    unittest.main()
