"""Tests for MailboxTaskService."""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from modules.run_store import init_db
from modules.mailbox.task_service import (
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
from modules.task_orchestrator import (
    ACTION_CLARIFICATION,
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
    TaskDecision,
)


def _make_exp(id="exp1", status="queued", test_exit_code=None, diff_stat=None, error=None):
    exp = MagicMock()
    exp.id = id
    exp.status = status
    exp.test_exit_code = test_exit_code
    exp.diff_stat = diff_stat
    exp.error = error
    return exp


class _FixedOrchestrator:
    decision = TaskDecision(action=ACTION_NEW_EXPERIMENT, reason="test default")
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run(self, task, **kwargs):
        type(self).calls.append((task, kwargs))
        return self.decision

    def close(self):
        return None


def _make_service(tmp_path: Path, exp_service=None, orchestrator_factory=None) -> MailboxTaskService:
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, status, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("/agent/inbox/test.md", "abc123", "task", "tester", "done", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    operations_registry = MagicMock() if exp_service is not None else None
    if operations_registry is not None:
        operations_registry.catalog.return_value = []
    return MailboxTaskService(
        db_path=db_path,
        experiment_service=exp_service,
        outbox_dir=str(tmp_path / "outbox"),
        repo_path=str(tmp_path),
        operations_registry=operations_registry,
        orchestrator_factory=orchestrator_factory or _FixedOrchestrator,
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
        self.assertEqual(a2.task_body, "Do something")


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
        self.assertFalse(any(a.id == approval.id for a in self.svc.list_pending()))


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
        self.assertFalse(any(a.id == self.approval.id for a in self.svc.list_pending()))

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
        results, errors = [], []

        def do_approve():
            try:
                results.append(self.svc.approve(self.approval.id).status)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_approve) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(all(status == _STATUS_APPROVED for status in results))
        self.assertEqual(errors, [])


class TestSubmitApproved(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        _FixedOrchestrator.calls = []

    def _make_exp_service(self, exp):
        es = MagicMock()
        es.create_experiment.return_value = exp
        es.get_experiment.return_value = exp
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
        import time; time.sleep(0.1)
        es.create_experiment.assert_called_once_with("Do work")
        updated = svc.get(approval.id)
        self.assertIn(updated.status, (_STATUS_SUBMITTED, _STATUS_RUNNING, _STATUS_SUCCEEDED, _STATUS_FAILED))
        _, kwargs = _FixedOrchestrator.calls[-1]
        self.assertEqual(kwargs["source_type"], "task")
        self.assertEqual(kwargs["source_id"], approval.id)

    def test_submit_approved_no_double_submit(self):
        exp = _make_exp(id="expXYZ", status="queued")
        es = self._make_exp_service(exp)
        svc = _make_service(self.tmp, exp_service=es)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_approval(msg_id, "Do work", "alice", "/f.md", "2026-01-01T00:00:00Z")
        svc.approve(approval.id)
        svc.submit_approved()
        svc.submit_approved()
        es.create_experiment.assert_called_once()

    def test_resume_decision_reuses_existing_experiment(self):
        existing_id = "bd9720b26ec74f15a2693e5641adf141"
        exp = _make_exp(id=existing_id, status="verifying")
        exp.worktree_path = str(self.tmp / "wt")
        Path(exp.worktree_path).mkdir()
        es = self._make_exp_service(exp)
        es.resolve_experiment.return_value = exp

        class ResumeOrchestrator(_FixedOrchestrator):
            decision = TaskDecision(
                action=ACTION_RESUME_EXPERIMENT,
                experiment_id=existing_id,
                reason="existing experiment requested",
            )

        svc = _make_service(self.tmp, exp_service=es, orchestrator_factory=ResumeOrchestrator)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_approval(msg_id, "Resume experiment", "alice", "/f.md", "2026-01-01T00:00:00Z")
        svc.approve(approval.id)
        svc.submit_approved()
        import time; time.sleep(0.1)
        es.create_experiment.assert_not_called()
        self.assertEqual(svc.get(approval.id).experiment_id, existing_id)

    def test_clarification_can_continue_same_task_and_preserve_followup(self):
        exp = _make_exp(id="expAfterReply", status="queued")
        es = self._make_exp_service(exp)

        class DiscussionOrchestrator(_FixedOrchestrator):
            decisions = [
                TaskDecision(
                    action=ACTION_CLARIFICATION,
                    message="Optimize for cost or latency?",
                    reason="material tradeoff",
                    session_id="session-abc",
                ),
                TaskDecision(action=ACTION_NEW_EXPERIMENT, reason="clarified"),
            ]
            calls = []

            def run(self, task, **kwargs):
                type(self).calls.append((task, kwargs))
                return type(self).decisions.pop(0)

        svc = _make_service(self.tmp, exp_service=es, orchestrator_factory=DiscussionOrchestrator)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_approval(msg_id, "Improve routing", "alice", "/f.md", "2026-01-01T00:00:00Z")
        svc.approve(approval.id)
        svc.submit_approved()
        waiting = svc.get(approval.id)
        self.assertEqual(waiting.status, _STATUS_NEEDS_REVIEW)
        self.assertIn("Nexus session: session-abc", waiting.result_summary)

        svc.continue_discussion(approval.id, "Optimize for cost")
        import time; time.sleep(0.1)

        es.create_experiment.assert_called_once()
        goal = es.create_experiment.call_args[0][0]
        self.assertIn("Improve routing", goal)
        self.assertIn("Optimize for cost", goal)
        self.assertEqual(DiscussionOrchestrator.calls[0][1]["source_id"], approval.id)
        self.assertEqual(DiscussionOrchestrator.calls[1][1]["source_id"], approval.id)


class TestPollRunning(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def _make_submitted(self, exp_id="exp1"):
        es = MagicMock()
        svc = _make_service(self.tmp, exp_service=es)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_approval(msg_id, "Task", "alice", "/f.md", "2026-01-01T00:00:00Z")
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
        self.assertEqual(svc.get(approval_id).status, _STATUS_SUCCEEDED)

    def test_recovery_running_experiment_failed(self):
        exp = _make_exp(id="exp1", status="failed", error="something went wrong")
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp
        svc.poll_running()
        self.assertEqual(svc.get(approval_id).status, _STATUS_FAILED)

    def test_recovery_submitted_experiment_queued(self):
        exp = _make_exp(id="exp1", status="queued")
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp
        svc.poll_running()
        self.assertIn(svc.get(approval_id).status, (_STATUS_RUNNING, _STATUS_SUBMITTED))

    def test_needs_review_creates_problem(self):
        exp = _make_exp(id="exp1", status="needs_review")
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp
        svc.poll_running()
        self.assertEqual(svc.get(approval_id).status, _STATUS_NEEDS_REVIEW)
        problem_files = list((self.tmp / "state" / "runs").glob("*.json"))
        self.assertTrue(problem_files)
        self.assertTrue(json.loads(problem_files[0].read_text())["response"].startswith("ERROR:"))

    def test_result_to_outbox(self):
        exp = _make_exp(id="exp1", status="succeeded", test_exit_code=0, diff_stat="3 files changed")
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp
        svc.poll_running()
        outbox_files = list(svc.outbox_dir.glob("*.md"))
        self.assertTrue(outbox_files)
        self.assertIn("succeeded", outbox_files[0].read_text())

    def test_failed_creates_problem(self):
        exp = _make_exp(id="exp1", status="failed", error="test error")
        svc, approval_id = self._make_submitted("exp1")
        svc.experiment_service.get_experiment.return_value = exp
        svc.poll_running()
        problem_files = list((self.tmp / "state" / "runs").glob("*.json"))
        self.assertTrue(problem_files)
        self.assertIn("failed", json.loads(problem_files[0].read_text())["response"])


class TestMigration(unittest.TestCase):
    def test_migration_existing_records(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        db_path = str(tmp / "test.db")
        conn = sqlite3.connect(db_path)
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
            "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, status, received_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("/agent/inbox/old.md", "sha1", "task", "rene", "done", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()
        conn2 = init_db(db_path)
        conn2.row_factory = sqlite3.Row
        rows = conn2.execute("SELECT * FROM mailbox_task_approvals").fetchall()
        conn2.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending_approval")
        self.assertEqual(rows[0]["sender"], "rene")


class TestApiAuth(unittest.TestCase):
    def test_list_pending_auth(self):
        import os
        os.environ.setdefault("AGENT_API_KEY", "test-key-abc")
        from fastapi.testclient import TestClient
        svc = MagicMock()
        svc.list_recent.return_value = []
        svc.recover_after_restart.return_value = None
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
