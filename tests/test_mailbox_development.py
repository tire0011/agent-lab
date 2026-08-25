"""Tests for the development mailbox adapter."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules import mailbox
from modules.mailbox_development import development_mode_enabled, install, process_command
from modules.run_store import init_db


class TestMailboxDevelopment(unittest.TestCase):
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
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            body, idea_id = process_command(msg, Path(tmp), None, 1)
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
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            repo = Path(tmp)
            body, _ = process_command(msg, repo, None, 1)
            self.assertEqual((repo / "created.txt").read_text(), "changed")
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("hello", body)

    def test_command_timeout_is_bounded(self):
        msg = SimpleNamespace(body="printf hello", meta={"timeout": "3601"})
        env = {
            "PATH": os.environ.get("PATH", ""),
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                process_command(msg, Path(tmp), None, 1)

    def test_command_rejects_restart_of_agent_api(self):
        msg = SimpleNamespace(
            body="echo before && sudo -n systemctl restart agent-api.service && echo after",
            meta={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "independent maintenance/deployment"):
                process_command(msg, Path(tmp), None, 1)

    def test_command_rejects_restart_of_mailbox_service(self):
        msg = SimpleNamespace(
            body="systemctl stop nexus-mailbox.service",
            meta={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "nexus-mailbox"):
                process_command(msg, Path(tmp), None, 1)

    def test_command_allows_read_only_service_status(self):
        msg = SimpleNamespace(body="printf 'agent-api status check'", meta={})
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            body, _ = process_command(msg, Path(tmp), None, 1)
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
