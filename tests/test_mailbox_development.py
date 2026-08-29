"""Tests for the development mailbox adapter."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules import mailbox
from modules.mailbox_development import development_mode_enabled, install, process_command
from modules.run_store import init_db


class TestMailboxDevelopment(unittest.TestCase):
    def _run_command(self, msg, repo: Path, db_msg_id: int = 1):
        result = process_command(msg, repo, None, db_msg_id)
        deadline = time.monotonic() + 5
        while result is mailbox._DEFERRED_PROCESSING:
            if time.monotonic() >= deadline:
                self.fail("command did not finish")
            time.sleep(0.01)
            result = process_command(msg, repo, None, db_msg_id)
        return result

    def test_install_registers_command_type_and_processor(self):
        install(mailbox)
        self.assertIn("command", mailbox.SUPPORTED_TYPES)
        self.assertIs(mailbox._PROCESSORS["command"], process_command)

    def test_development_mode_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(development_mode_enabled())
        with patch.dict(os.environ, {"NEXUS_DEVELOPMENT_MODE": "1"}, clear=True):
            self.assertTrue(development_mode_enabled())

    def test_command_executes_without_development_mode(self):
        msg = SimpleNamespace(body="printf hello", meta={})
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        with tempfile.TemporaryDirectory() as tmp:
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                body, idea_id = self._run_command(msg, Path(tmp))
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("hello", body)
        self.assertIsNone(idea_id)

    def test_command_executes_and_can_change_workspace(self):
        msg = SimpleNamespace(
            body="printf hello && printf changed > created.txt",
            meta={"timeout": "5"},
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        with tempfile.TemporaryDirectory() as tmp:
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                repo = Path(tmp)
                body, _ = self._run_command(msg, repo)
                self.assertEqual((repo / "created.txt").read_text(), "changed")
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("hello", body)

    def test_command_timeout_metadata_is_ignored(self):
        msg = SimpleNamespace(body="sleep 0.05; printf done", meta={"timeout": "1"})
        env = {"PATH": os.environ.get("PATH", "")}
        with tempfile.TemporaryDirectory() as tmp:
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                first = process_command(msg, Path(tmp), None, 1)
                self.assertIs(first, mailbox._DEFERRED_PROCESSING)
                body, _ = self._run_command(msg, Path(tmp))
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("done", body)

    def test_command_allows_read_only_service_status(self):
        msg = SimpleNamespace(body="printf 'agent-api status check'", meta={})
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        with tempfile.TemporaryDirectory() as tmp:
            env["NEXUS_MAILBOX_COMMAND_STATE_DIR"] = str(Path(tmp) / "jobs")
            with patch.dict(os.environ, env, clear=True):
                body, _ = self._run_command(msg, Path(tmp))
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("agent-api status check", body)

    def test_task_is_approved_without_human_step(self):
        install(mailbox)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state.db")
            conn = init_db(db_path)
            mailbox.init_mailbox_db(conn)
            now = mailbox._now_iso()
            cur = conn.execute(
                """
                INSERT INTO mailbox_messages
                    (file_path, source_commit_sha, msg_type, created_by, status, received_at)
                VALUES (?, 'sha', 'task', 'rene', 'processing', ?)
                """,
                (str(Path(tmp) / "task.md"), now),
            )
            db_msg_id = cur.lastrowid
            conn.commit()
            msg = SimpleNamespace(
                body="Inspect and improve the project.",
                meta={},
                created_by="rene",
                file_path=Path(tmp) / "task.md",
            )
            with patch.dict(os.environ, {}, clear=True):
                result, _ = mailbox._PROCESSORS["task"](
                    msg, Path(tmp), conn, db_msg_id
                )
            row = conn.execute(
                "SELECT status, approved_at FROM mailbox_task_approvals WHERE mailbox_msg_id=?",
                (db_msg_id,),
            ).fetchone()
            conn.close()
        self.assertEqual(row[0], "approved")
        self.assertIsNotNone(row[1])
        self.assertIn("approved automatically", result)


if __name__ == "__main__":
    unittest.main()
