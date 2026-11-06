"""Tests for MailboxTaskService."""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.run_store import init_db
from modules.github_work_tracking import GitHubWorkTrackingError
from modules.mailbox.task_service import (
    MailboxTaskService,
    _STATUS_QUEUED,
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
    exp.test_command = None
    exp.test_output = None
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


def _make_service(
    tmp_path: Path,
    exp_service=None,
    orchestrator_factory=None,
    github_work_tracking=None,
) -> MailboxTaskService:
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
    github = github_work_tracking or MagicMock()
    github.ensure_for_source.return_value = SimpleNamespace(
        issue_number=42, branch_name="nexus/42-mailbox"
    )
    github.bind_experiment.side_effect = lambda ref, experiment_id: ref
    github.recover_experiment.return_value = SimpleNamespace(
        issue_number=42, branch_name="nexus/42-mailbox"
    )
    return MailboxTaskService(
        db_path=db_path,
        experiment_service=exp_service,
        outbox_dir=str(tmp_path / "outbox"),
        repo_path=str(tmp_path),
        operations_registry=operations_registry,
        orchestrator_factory=orchestrator_factory or _FixedOrchestrator,
        github_work_tracking=github,
        task_log_dir=str(tmp_path / "experiment-logs"),
    )


def _insert_mailbox_msg(svc: MailboxTaskService, created_by="tester") -> int:
    cur = svc._conn.execute(
        "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, status, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("/agent/inbox/extra.md", "sha", "task", created_by, "done", "2026-01-01T00:00:00Z"),
    )
    svc._conn.commit()
    return cur.lastrowid


class TestCreateTask(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.svc = _make_service(self.tmp)

    def test_create_task_goes_straight_to_queue(self):
        msg_id = _insert_mailbox_msg(self.svc)
        task = self.svc.create_task(msg_id, "Do something", "alice", "/inbox/f.md", "2026-01-01T00:00:00Z")
        self.assertEqual(task.status, _STATUS_QUEUED)
        self.assertIsNone(task.approved_at)
        self.assertEqual(task.task_body, "Do something")
        self.assertEqual(task.sender, "alice")
        self.assertEqual(task.mailbox_msg_id, msg_id)

    def test_create_task_idempotent(self):
        msg_id = _insert_mailbox_msg(self.svc)
        a1 = self.svc.create_task(msg_id, "Do something", "alice", "/inbox/f.md", "2026-01-01T00:00:00Z")
        a2 = self.svc.create_task(msg_id, "Do something else", "bob", "/inbox/g.md", "2026-01-02T00:00:00Z")
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(a2.task_body, "Do something")

    def test_approval_step_is_gone(self):
        # #264: nobody approves or rejects a mailbox task any more.
        self.assertFalse(hasattr(self.svc, "approve"))
        self.assertFalse(hasattr(self.svc, "reject"))


class TestListPending(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.svc = _make_service(self.tmp)

    def test_list_pending_returns_queued_tasks(self):
        msg_id = _insert_mailbox_msg(self.svc)
        self.svc.create_task(msg_id, "Task body", "alice", "/f.md", "2026-01-01T00:00:00Z")
        pending = self.svc.list_pending()
        self.assertTrue(any(a.mailbox_msg_id == msg_id for a in pending))

    def test_list_pending_excludes_submitted(self):
        msg_id = _insert_mailbox_msg(self.svc)
        task = self.svc.create_task(msg_id, "Task body", "alice", "/f.md", "2026-01-01T00:00:00Z")
        self.svc._conn.execute(
            "UPDATE mailbox_task_approvals SET status=? WHERE id=?", (_STATUS_SUBMITTED, task.id)
        )
        self.svc._conn.commit()
        self.assertFalse(any(a.id == task.id for a in self.svc.list_pending()))


def _insert_legacy_record(svc: MailboxTaskService, status: str, **extra: str) -> str:
    """A row as the removed approval step (#264) left it."""
    msg_id = _insert_mailbox_msg(svc)
    record_id = f"legacy-{status}"
    svc._conn.execute(
        """INSERT INTO mailbox_task_approvals
           (id, mailbox_msg_id, task_body, sender, file_path, received_at, status,
            approved_at, rejected_at, rejection_reason, created_at, updated_at)
           VALUES (?, ?, 'Old task', 'alice', '/f.md', '2026-01-01T00:00:00Z', ?, ?, ?, ?,
                   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (record_id, msg_id, status, extra.get("approved_at"), extra.get("rejected_at"),
         extra.get("rejection_reason")),
    )
    svc._conn.commit()
    return record_id


class TestLegacyApprovalRecords(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.es = MagicMock()
        self.es.experiment_log_dir = str(self.tmp / "experiment-logs")
        self.svc = _make_service(self.tmp, exp_service=self.es)
        _FixedOrchestrator.calls = []

    def test_legacy_records_stay_readable(self):
        pending_id = _insert_legacy_record(self.svc, "pending_approval")
        rejected_id = _insert_legacy_record(
            self.svc, "rejected", rejected_at="2026-01-02T00:00:00Z", rejection_reason="No"
        )
        self.assertEqual(self.svc.get(pending_id).status, "pending_approval")
        rejected = self.svc.get(rejected_id)
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.rejection_reason, "No")
        listed = {t.id: t.status for t in self.svc.list_recent()}
        self.assertEqual(listed[pending_id], "pending_approval")
        self.assertEqual(listed[rejected_id], "rejected")

    def test_legacy_pending_approval_is_never_run(self):
        pending_id = _insert_legacy_record(self.svc, "pending_approval")
        self.svc.submit_queued()
        self.assertEqual(_FixedOrchestrator.calls, [])
        self.es.create_experiment.assert_not_called()
        self.assertEqual(self.svc.get(pending_id).status, "pending_approval")

    def test_legacy_approved_record_becomes_queued_on_migration(self):
        approved_id = _insert_legacy_record(
            self.svc, "approved", approved_at="2026-01-01T00:00:00Z"
        )
        init_db(self.svc.db_path).close()
        task = self.svc.get(approved_id)
        self.assertEqual(task.status, _STATUS_QUEUED)
        self.assertEqual(task.approved_at, "2026-01-01T00:00:00Z")


class TestSubmitQueued(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        _FixedOrchestrator.calls = []

    def _make_exp_service(self, exp):
        es = MagicMock()
        es.create_experiment.return_value = exp
        es.get_experiment.return_value = exp
        es.run_experiment.return_value = exp
        es.experiment_log_dir = str(self.tmp / "experiment-logs")
        return es

    def test_submit_queued_calls_experiment_service_once(self):
        exp = _make_exp(id="expABC", status="queued")
        es = self._make_exp_service(exp)
        svc = _make_service(self.tmp, exp_service=es)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_task(msg_id, "Do work", "alice", "/f.md", "2026-01-01T00:00:00Z")
        svc.submit_queued()
        import time; time.sleep(0.1)
        es.create_experiment.assert_called_once_with(
            "Do work", complexity="large", task_key=f"mailbox:{approval.id}"
        )
        updated = svc.get(approval.id)
        self.assertIn(updated.status, (_STATUS_SUBMITTED, _STATUS_RUNNING, _STATUS_SUCCEEDED, _STATUS_FAILED))
        _, kwargs = _FixedOrchestrator.calls[-1]
        self.assertEqual(kwargs["source_type"], "task")
        self.assertEqual(kwargs["source_id"], approval.id)

    def test_submit_queued_no_double_submit(self):
        exp = _make_exp(id="expXYZ", status="queued")
        es = self._make_exp_service(exp)
        svc = _make_service(self.tmp, exp_service=es)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_task(msg_id, "Do work", "alice", "/f.md", "2026-01-01T00:00:00Z")
        svc.submit_queued()
        svc.submit_queued()
        es.create_experiment.assert_called_once()

    def test_missing_github_configuration_blocks_before_experiment_creation(self):
        exp = _make_exp(id="expBLOCK", status="queued")
        es = self._make_exp_service(exp)
        github = MagicMock()
        github.ensure_for_source.side_effect = GitHubWorkTrackingError(
            "Configure GITHUB_TOKEN/GH_TOKEN"
        )
        svc = _make_service(
            self.tmp,
            exp_service=es,
            github_work_tracking=github,
        )
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_task(
            msg_id, "Do work", "alice", "/f.md", "2026-01-01T00:00:00Z"
        )

        svc.submit_queued()

        es.create_experiment.assert_not_called()
        updated = svc.get(approval.id)
        self.assertEqual(updated.status, _STATUS_NEEDS_REVIEW)
        self.assertIn("GitHub lifecycle blocked", updated.result_summary)

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
        approval = svc.create_task(msg_id, "Resume experiment", "alice", "/f.md", "2026-01-01T00:00:00Z")
        svc.submit_queued()
        import time; time.sleep(0.1)
        es.create_experiment.assert_not_called()
        self.assertEqual(svc.get(approval.id).experiment_id, existing_id)



class TestPollRunning(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def _make_submitted(self, exp_id="exp1"):
        es = MagicMock()
        svc = _make_service(self.tmp, exp_service=es)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_task(msg_id, "Task", "alice", "/f.md", "2026-01-01T00:00:00Z")
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
        # The body was never stored, so the backfilled task is not run (#264).
        self.assertEqual(rows[0]["status"], "needs_review")
        self.assertIn("not handed to Nexus", rows[0]["error"])
        self.assertEqual(rows[0]["sender"], "rene")

    def test_backfill_with_stored_body_is_queued(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        db_path = str(tmp / "test.db")
        conn = init_db(db_path)
        conn.execute(
            "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, "
            "status, received_at, task_body) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("/agent/inbox/t.md", "sha1", "task", "rene", "done", "2026-01-01T00:00:00Z", "Do it"),
        )
        conn.commit()
        conn.close()
        conn2 = init_db(db_path)
        conn2.row_factory = sqlite3.Row
        rows = conn2.execute("SELECT status, task_body FROM mailbox_task_approvals").fetchall()
        conn2.close()
        self.assertEqual([(r["status"], r["task_body"]) for r in rows], [("queued", "Do it")])


class TestApiAuth(unittest.TestCase):
    def test_list_pending_auth(self):
        # No AGENT_API_KEY set here: conftest has already set it, this request
        # deliberately sends no key, and 401 is the assertion. The setdefault
        # that used to sit here named a third key nothing else used, which is
        # how the suite drifted into disagreeing with itself about the one
        # value every API test depends on.
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


class TestApprovalEndpointsRemoved(unittest.TestCase):
    def test_approve_and_reject_routes_are_gone(self):
        from api import app

        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertIn("/mailbox/tasks", paths)
        self.assertIn("/mailbox/tasks/{approval_id}", paths)
        self.assertFalse([p for p in paths if p.endswith(("/approve", "/reject")) and "mailbox" in p])


class TestTaskLogging(unittest.TestCase):
    """Gap 1: mailbox intake and queueing/experiment
    creation must produce durable structured log entries visible in the
    unified Logs feed before an experiment id exists, and correlatable
    with it once one is created."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def _read_task_log(self, svc: MailboxTaskService, approval_id: str) -> list[dict]:
        path = Path(svc._task_logger(approval_id).path)
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_create_task_logs_task_received(self):
        svc = _make_service(self.tmp)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_task(msg_id, "Do something", "alice", "/f.md", "2026-01-01T00:00:00Z")
        entries = self._read_task_log(svc, approval.id)
        events = [(e["phase"], e["event"]) for e in entries]
        self.assertIn(("mailbox", "task_received"), events)
        self.assertIsNone(entries[0]["experiment_id"])

    def test_experiment_creation_correlates_task_log_with_experiment_id(self):
        exp = _make_exp(id="expLOG", status="queued")
        es = MagicMock()
        es.create_experiment.return_value = exp
        es.get_experiment.return_value = exp
        es.run_experiment.return_value = exp
        es.experiment_log_dir = str(self.tmp / "experiment-logs")
        svc = _make_service(self.tmp, exp_service=es)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_task(msg_id, "Do work", "alice", "/f.md", "2026-01-01T00:00:00Z")
        svc.submit_queued()
        import time
        time.sleep(0.1)

        entries = self._read_task_log(svc, approval.id)
        correlating = [e for e in entries if e["event"] == "experiment_created"]
        self.assertEqual(len(correlating), 1)
        self.assertEqual(correlating[0]["experiment_id"], "expLOG")
        # Every earlier event (intake, GitHub tracking start) has
        # no experiment_id yet - the whole point of the pre-experiment log.
        earlier = [e for e in entries if e["event"] != "experiment_created"]
        self.assertTrue(earlier)
        self.assertTrue(all(e["experiment_id"] is None for e in earlier))

    def test_github_failure_before_experiment_creation_is_logged(self):
        exp = _make_exp(id="expBLOCK", status="queued")
        es = MagicMock()
        es.create_experiment.return_value = exp
        es.experiment_log_dir = str(self.tmp / "experiment-logs")
        github = MagicMock()
        github.ensure_for_source.side_effect = GitHubWorkTrackingError("Configure GITHUB_TOKEN")
        svc = _make_service(self.tmp, exp_service=es, github_work_tracking=github)
        msg_id = _insert_mailbox_msg(svc)
        approval = svc.create_task(msg_id, "Do work", "alice", "/f.md", "2026-01-01T00:00:00Z")
        svc.submit_queued()

        entries = self._read_task_log(svc, approval.id)
        events = [(e["level"], e["event"]) for e in entries]
        self.assertIn(("ERROR", "github_work_tracking_failed"), events)
        es.create_experiment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
