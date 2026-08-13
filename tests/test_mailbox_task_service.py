"""Tests for MailboxTaskService."""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules.run_store import init_db
from modules.mailbox_task_service import (
    ApprovalNotFoundError,
    ApprovalStateError,
    MailboxTaskService,
    _STATUS_PENDING,
    _STATUS_APPROVED,
    _STATUS_REJECTED,
    _STATUS_SUBMITTED,
    _STATUS_RUNNING,
    _STATUS_SUCCEEDED,
    _STATUS_FAILED,
    _STATUS_NEEDS_REVIEW,
)


def _make_exp(id="exp1", status="queued", test_exit_code=None, diff_stat=None, error=None):
    exp = MagicMock()
    exp.id = id
    exp.status = status
    exp.test_exit_code = test_exit_code
    exp.diff_stat = diff_stat
    exp.error = error
    return exp


def _make_service(tmp_path: Path, exp_service=None) -> MailboxTaskService:
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    # Insert a dummy mailbox_messages row
    conn.execute(
        "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, status, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("/agent/inbox/test.md", "abc123", "task", "tester", "done", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return MailboxTaskService(
        db_path=db_path,
        experiment_service=exp_service,
        outbox_dir=str(tmp_path / "outbox"),
        repo_path=str(tmp_path),
    )


def _insert_mailbox_msg(svc: MailboxTaskService, created_by="tester") -> int:
    cur = svc._conn.execute(
        "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, status, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("/agent/inbox/extra.md", "sha", "task", created_by, "done", "2026-01-01T00:00:00Z"),
    )
    svc._conn.commit()
    return cur.lastrowid


class TestCreateApproval(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.svc = _make_service(self.tmp)

    def test_create_approval(self):
        msg_id = _insert_mailbox_msg(self.svc)
        approval = self.svc.create_approval(msg_id, "Do something", "alice", "/inbox/f.md", "2026-01-01T00:00:00Z")
        self.assertEqual(approval.status, _STATUS_PENDING)
        self.assertEqual(approval.task_body, "Do something")
        self.assertEqual(approval.sender, "alice")
        self.assertEqual(approval.mailbox_msg_id, msg_id)

    def test_create_approval_idempotent(self):
        msg_id = _insert_mailbox_msg(self.svc)
        a1 = self.svc.create_approval(msg_id, "Do something", "alice", "/inbox/f.md", "2026-01-01T00:00:00Z")
        a2 = self.svc.create_approval(msg_id, "Do something else", "bob", "/inbox/g.md", "2026-01-02T00:00:00Z")
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(a2.task_body, "Do something")  # first record preserved


class TestListPending(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.svc = _make_service(self.tmp)

    def test_list_pending(self):
        msg_id = _insert_mailbox_msg(self.svc)
        self.svc.create_approval(msg_id, "Task body", "alice", "/f.md", "2026-01-01T00:00:00Z")
        pending = self.svc.list_pending()
        self.assertTrue(any(a.mailbox_msg_id == msg_id for a in pending))

    def test_list_pending_excludes_approved(self):
        msg_id = _insert_mailbox_msg(self.svc)
        approval = self.svc.create_approval(msg_id, "Task body", "alice", "/f.md", "2026-01-01T00:00:00Z")
        self.svc.approve(approval.id)
        pending = self.svc.list_pending()
        self.assertFalse(any(a.id == approval.id for a in pending))


class TestApprove(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.svc = _make_service(self.tmp)
        msg_id = _insert_mailbox_msg(self.svc)
        self.approval = self.svc.create_approval(msg_id, "Task body", "alice", "/f.md", "2026-01-01T00:00:00Z")

    def test_approve(self):
        approved = self.svc.approve(self.approval.id)
        self.assertEqual(approved.status, _STATUS_APPROVED)
        self.assertIsNotNone(approved.approved_at)

    def test_approve_idempotent(self):
        a1 = self.svc.approve(self.approval.id)
        a2 = self.svc.approve(self.approval.id)
        self.assertEqual(a1.status, _STATUS_APPROVED)
        self.assertEqual(a2.status, _STATUS_APPROVED)
        self.assertEqual(a1.id, a2.id)

    def test_approve_after_reject_fails(self):
        self.svc.reject(self.approval.id, "No thanks")
        with self.assertRaises(ApprovalStateError):
            self.svc.approve(self.approval.id)


class TestReject(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.svc = _make_service(self.tmp)
        msg_id = _insert_mailbox_msg(self.svc)
        self.approval = self.svc.create_approval(msg_id, "Task body", "alice", "/f.md", "2026-01-01T00:00:00Z")

    def test_reject(self):
        rejected = self.svc.reject(self.approval.id, "Not needed")
        self.assertEqual(rejected.status, _STATUS_REJECTED)
        self.assertEqual(rejected.rejection_reason, "Not needed")

    def test_reject_prevents_execution(self):
        self.svc.reject(self.approval.id, "No")
        pending = self.svc.list_pending()
        self.assertFalse(any(a.id == self.approval.id for a in pending))

    def test_reject_after_approve_fails(self):
        self.svc.approve(self.approval.id)
        with self.assertRaises(ApprovalStateError):
            self.svc.reject(self.approval.id, "Too late")

    def test_reject_invalid_reason(self):
        with self.assertRaises(ValueError):
            self.svc.reject(self.approval.id, "<script>alert(1)</script>")


class TestConcurrentApprove(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.svc = _make_service(self.tmp)
        msg_id = _insert_mailbox_msg(self.svc)
        self.approval = self.svc.create_approval(msg_id, "Task", "a", "/f.md", "2026-01-01T00:00:00Z")

    def test_concurrent_approve(self):
        results = []
        errors = []

        def do_approve():
            try:
                a = self.svc.approve(self.approval.id)
                results.append(a.status)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_approve) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should succeed (idempotent) and all should be approved
        self.assertTrue(all(s == _STATUS_APPROVED for s in results))
        self.assertEqual(len(errors), 0)

        # Only one record should exist
        row = self.svc._conn.execute(
            "SELECT COUNT(*) FROM mailbox_task_approvals WHERE mailbox_msg_id = ?",
            (self.approval.mailbox_msg_id,)
        ).fetchone()[0]
        self.assertEqual(row, 1)


class TestSubmitApproved(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def _make_exp_service(self, exp):
        es = MagicMock()
        es.create_experiment.return_value = exp
        es.get_experiment.return_value = exp
        # run_experiment blocks until done
        es.run_experiment.return_value = exp
        return es

    def test_submit_approved_calls_experiment_service_once(self):
        exp = _make_exp(id="expABC", status="queued")
        es = self._make_exp_service(exp)
        svc = _make_service(self.tmp, exp_service=es)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_approval(msg_id, "Do work", "alice", "/f.md", "2026-01-01T00:00:00Z")
        svc.approve(approval.id)

        svc.submit_approved()
        # Wait briefly for thread
        import time; time.sleep(0.1)

        es.create_experiment.assert_called_once()
        call_goal = es.create_experiment.call_args[0][0]
        self.assertEqual(call_goal, "Do work")

        # Row should now be submitted (or running since thread may have started)
        updated = svc.get(approval.id)
        self.assertIn(updated.status, (_STATUS_SUBMITTED, _STATUS_RUNNING, _STATUS_SUCCEEDED, _STATUS_FAILED))

    def test_submit_approved_no_double_submit(self):
        exp = _make_exp(id="expXYZ", status="queued")
        es = self._make_exp_service(exp)
        svc = _make_service(self.tmp, exp_service=es)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_approval(msg_id, "Do work", "alice", "/f.md", "2026-01-01T00:00:00Z")
        svc.approve(approval.id)
        svc.submit_approved()
        svc.submit_approved()  # second call should find no approved records

        es.create_experiment.assert_called_once()


class TestPollRunning(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def _make_submitted(self, exp_id="exp1") -> tuple[MailboxTaskService, str]:
        es = MagicMock()
        svc = _make_service(self.tmp, exp_service=es)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_approval(msg_id, "Task", "alice", "/f.md", "2026-01-01T00:00:00Z")
        # Manually put into submitted state with experiment_id
        svc._conn.execute(
            "UPDATE mailbox_task_approvals SET status=?, experiment_id=?, updated_at=? WHERE id=?",
            (_STATUS_SUBMITTED, exp_id, "2026-01-01T00:00:00Z", approval.id),
        )
        svc._conn.commit()
        return svc, approval.id

    def test_recovery_submitted_experiment_succeeded(self):
        exp = _make_exp(id="exp1", status="succeeded", test_exit_code=0)
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp

        svc.poll_running()

        updated = svc.get(approval_id)
        self.assertEqual(updated.status, _STATUS_SUCCEEDED)

    def test_recovery_running_experiment_failed(self):
        exp = _make_exp(id="exp1", status="failed", error="something went wrong")
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp

        svc.poll_running()

        updated = svc.get(approval_id)
        self.assertEqual(updated.status, _STATUS_FAILED)

    def test_recovery_submitted_experiment_queued(self):
        exp = _make_exp(id="exp1", status="queued")
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp

        svc.poll_running()

        updated = svc.get(approval_id)
        # queued experiment → approval transitions to running
        self.assertIn(updated.status, (_STATUS_RUNNING, _STATUS_SUBMITTED))

    def test_needs_review_creates_problem(self):
        exp = _make_exp(id="exp1", status="needs_review")
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp

        svc.poll_running()

        updated = svc.get(approval_id)
        self.assertEqual(updated.status, _STATUS_NEEDS_REVIEW)
        # Check a problem file was written
        runs_dir = self.tmp / "state" / "runs"
        problem_files = list(runs_dir.glob("*.json")) if runs_dir.exists() else []
        self.assertTrue(len(problem_files) >= 1)
        data = json.loads(problem_files[0].read_text())
        self.assertTrue(data["response"].startswith("ERROR:"))

    def test_result_to_outbox(self):
        exp = _make_exp(id="exp1", status="succeeded", test_exit_code=0, diff_stat="3 files changed")
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp

        svc.poll_running()

        outbox_files = list(svc.outbox_dir.glob("*.md")) if svc.outbox_dir.exists() else []
        self.assertTrue(len(outbox_files) >= 1)
        content = outbox_files[0].read_text()
        self.assertIn("succeeded", content)

    def test_failed_creates_problem(self):
        exp = _make_exp(id="exp1", status="failed", error="test error")
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp

        svc.poll_running()

        runs_dir = self.tmp / "state" / "runs"
        problem_files = list(runs_dir.glob("*.json")) if runs_dir.exists() else []
        self.assertTrue(len(problem_files) >= 1)
        data = json.loads(problem_files[0].read_text())
        self.assertIn("failed", data["response"])


class TestMigration(unittest.TestCase):
    def test_migration_existing_records(self):
        """Existing mailbox_messages with msg_type='task' and no approval → migration creates pending_approval."""
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        db_path = str(tmp / "test.db")
        conn = sqlite3.connect(db_path)
        # Create minimal mailbox_messages schema (simulating old DB without task_body)
        conn.executescript("""
            CREATE TABLE mailbox_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                source_commit_sha TEXT NOT NULL,
                msg_type TEXT NOT NULL,
                created_by TEXT NOT NULL,
                status TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processed_at TEXT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NULL,
                idea_id TEXT NULL,
                outbox_file TEXT NULL
            );
        """)
        conn.execute(
            "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, status, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("/agent/inbox/old.md", "sha1", "task", "rene", "done", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        # Now run init_db (which runs migrations)
        from modules.run_store import init_db
        conn2 = init_db(db_path)
        conn2.row_factory = sqlite3.Row
        rows = conn2.execute("SELECT * FROM mailbox_task_approvals").fetchall()
        conn2.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending_approval")
        self.assertEqual(rows[0]["sender"], "rene")


class TestApiAuth(unittest.TestCase):
    """Test that the API endpoints require auth."""

    def test_list_pending_auth(self):
        """GET /mailbox/tasks without API key returns 401."""
        import os
        os.environ.setdefault("AGENT_API_KEY", "test-key-abc")
        from fastapi.testclient import TestClient
        import importlib
        import api as api_module
        importlib.reload(api_module)
        # Override dependency to avoid real DB
        svc = MagicMock()
        svc.list_recent.return_value = []
        svc.recover_after_restart.return_value = None

        # Use a fresh import to avoid lru_cache issues
        from api import app, get_mailbox_task_service, get_codex_service
        codex_svc = MagicMock()
        codex_svc.mark_interrupted_tasks_failed.return_value = None
        codex_svc.list_tasks.return_value = []

        app.dependency_overrides[get_mailbox_task_service] = lambda: svc
        app.dependency_overrides[get_codex_service] = lambda: codex_svc

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/mailbox/tasks")
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
