"""Tests for the two failure-analysis integration points (#145):

* ``modules/mailbox/task_service.py`` - a mailbox task's own experiment
  failing/needing review during development/verification, before
  adoption/finalization is even attempted.
* ``modules/mailbox/finalizing_task_service.py`` - finalization (merge,
  deploy, live validation) landing in needs_review.

Both must call the failure-analysis service best-effort and must never let
a failing/misbehaving analyzer affect the terminal status they already
decided on.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.mailbox.task_service import (
    MailboxTaskService,
    _STATUS_FAILED,
    _STATUS_NEEDS_REVIEW,
    _STATUS_SUCCEEDED,
)
from modules.task_finalization import FinalizationResult, PHASE_NEEDS_REVIEW, PHASE_SUCCEEDED


def _make_exp(id="exp1", status="queued", test_exit_code=None, diff_stat=None, error=None):
    exp = MagicMock()
    exp.id = id
    exp.status = status
    exp.test_exit_code = test_exit_code
    exp.diff_stat = diff_stat
    exp.error = error
    exp.test_command = None
    exp.test_output = None
    exp.quality_evaluation = None
    return exp


def _make_service(tmp_path: Path, exp_service, *, failure_analysis_service=None, finalization_coordinator=None):
    from modules.run_store import init_db

    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, status, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("/agent/inbox/test.md", "abc123", "task", "tester", "done", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    github = MagicMock()
    github.ensure_for_source.return_value = SimpleNamespace(issue_number=1, branch_name="nexus/1")
    github.bind_experiment.side_effect = lambda ref, experiment_id: ref
    github.recover_experiment.return_value = SimpleNamespace(issue_number=1, branch_name="nexus/1")
    return MailboxTaskService(
        db_path=db_path,
        experiment_service=exp_service,
        outbox_dir=str(tmp_path / "outbox"),
        repo_path=str(tmp_path),
        operations_registry=MagicMock(),
        github_work_tracking=github,
        task_log_dir=str(tmp_path / "logs"),
        failure_analysis_service=failure_analysis_service,
        finalization_coordinator=finalization_coordinator,
    )


def _submit(svc: MailboxTaskService, exp_id: str) -> str:
    msg = svc._conn.execute(
        "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, status, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("/agent/inbox/x.md", "sha", "task", "tester", "done", "2026-01-01T00:00:00Z"),
    )
    svc._conn.commit()
    approval = svc.create_approval(msg.lastrowid, "Some task", "alice", "/f.md", "2026-01-01T00:00:00Z")
    svc._conn.execute(
        "UPDATE mailbox_task_approvals SET status=?, experiment_id=? WHERE id=?",
        ("submitted", exp_id, approval.id),
    )
    svc._conn.commit()
    return approval.id


class MailboxTaskFailureIntegrationTests(unittest.TestCase):
    """The non-finalization integration point: modules/mailbox/task_service.py."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_failed_experiment_triggers_analysis(self):
        analyzer = MagicMock()
        exp_service = MagicMock()
        exp = _make_exp(id="exp1", status="failed", error="boom")
        exp_service.get_experiment.return_value = exp
        svc = _make_service(self.tmp, exp_service, failure_analysis_service=analyzer)
        self.addCleanup(svc.close)
        approval_id = _submit(svc, "exp1")

        svc.poll_running()

        self.assertEqual(svc.get(approval_id).status, _STATUS_FAILED)
        analyzer.analyze_from_logs.assert_called_once()
        kwargs = analyzer.analyze_from_logs.call_args.kwargs
        self.assertEqual(kwargs["subject_type"], "mailbox_task")
        self.assertEqual(kwargs["experiment_id"], "exp1")
        self.assertEqual(kwargs["mailbox_task_id"], approval_id)

    def test_needs_review_experiment_triggers_analysis(self):
        analyzer = MagicMock()
        exp_service = MagicMock()
        exp = _make_exp(id="exp2", status="needs_review")
        exp_service.get_experiment.return_value = exp
        svc = _make_service(self.tmp, exp_service, failure_analysis_service=analyzer)
        self.addCleanup(svc.close)
        approval_id = _submit(svc, "exp2")

        svc.poll_running()

        self.assertEqual(svc.get(approval_id).status, _STATUS_NEEDS_REVIEW)
        analyzer.analyze_from_logs.assert_called_once()

    def test_succeeded_experiment_does_not_trigger_analysis(self):
        analyzer = MagicMock()
        exp_service = MagicMock()
        exp = _make_exp(id="exp3", status="succeeded", test_exit_code=0)
        exp_service.get_experiment.return_value = exp
        svc = _make_service(self.tmp, exp_service, failure_analysis_service=analyzer)
        self.addCleanup(svc.close)
        approval_id = _submit(svc, "exp3")

        svc.poll_running()

        self.assertEqual(svc.get(approval_id).status, _STATUS_SUCCEEDED)
        analyzer.analyze_from_logs.assert_not_called()

    def test_analyzer_failure_never_affects_the_terminal_status(self):
        analyzer = MagicMock()
        analyzer.analyze_from_logs.side_effect = RuntimeError("local model unreachable")
        exp_service = MagicMock()
        exp = _make_exp(id="exp4", status="failed", error="boom")
        exp_service.get_experiment.return_value = exp
        svc = _make_service(self.tmp, exp_service, failure_analysis_service=analyzer)
        self.addCleanup(svc.close)
        approval_id = _submit(svc, "exp4")

        svc.poll_running()  # must not raise

        self.assertEqual(svc.get(approval_id).status, _STATUS_FAILED)
        analyzer.analyze_from_logs.assert_called_once()

    def test_lazy_construction_failure_degrades_silently(self):
        # No failure_analysis_service injected: the base class tries to
        # build a real one lazily. Simulate that construction itself
        # blowing up (e.g. an unwritable db path) and confirm the terminal
        # transition still completes.
        exp_service = MagicMock()
        exp = _make_exp(id="exp5", status="failed", error="boom")
        exp_service.get_experiment.return_value = exp
        svc = _make_service(self.tmp, exp_service)
        self.addCleanup(svc.close)
        approval_id = _submit(svc, "exp5")

        def _boom(*args, **kwargs):
            raise RuntimeError("cannot open db")

        import modules.failure_analysis_service as fa_module
        original = fa_module.FailureAnalysisService
        fa_module.FailureAnalysisService = _boom
        try:
            svc.poll_running()
        finally:
            fa_module.FailureAnalysisService = original

        self.assertEqual(svc.get(approval_id).status, _STATUS_FAILED)
        self.assertIsNone(svc.failure_analysis_service)

    def test_lazy_construction_uses_the_service_own_log_dir(self):
        # The lazily-built FailureAnalysisService must read evidence from
        # the exact directory this MailboxTaskService writes task logs to
        # (task_log_dir here), not the module default - otherwise a
        # deployment/test using a non-default log dir would silently get
        # empty evidence.
        exp_service = MagicMock()
        exp = _make_exp(id="exp6", status="failed", error="boom")
        exp_service.get_experiment.return_value = exp
        svc = _make_service(self.tmp, exp_service)
        self.addCleanup(svc.close)
        approval_id = _submit(svc, "exp6")

        captured = {}

        class _Capturing:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def analyze_from_logs(self, **kwargs):
                return None

            def close(self):
                return None

        import modules.failure_analysis_service as fa_module
        original = fa_module.FailureAnalysisService
        fa_module.FailureAnalysisService = _Capturing
        try:
            svc.poll_running()
        finally:
            fa_module.FailureAnalysisService = original

        self.assertEqual(str(captured.get("log_dir")), str(self.tmp / "logs"))


class FinalizationFailureIntegrationTests(unittest.TestCase):
    """The finalization integration point: modules/mailbox/finalizing_task_service.py."""

    def _service(self, result: FinalizationResult, *, failure_analysis_service=None):
        exp = _make_exp(id="experiment-1", status="succeeded", test_exit_code=0, diff_stat="1 file changed")
        exp.quality_evaluation = {"outcome": "pass"}
        exp_service = MagicMock()
        exp_service.get_experiment.return_value = exp
        coordinator = MagicMock()
        coordinator.advance.return_value = result
        svc = _make_service(
            self.tmp, exp_service,
            failure_analysis_service=failure_analysis_service,
            finalization_coordinator=coordinator,
        )
        message = svc._conn.execute(
            """INSERT INTO mailbox_messages
               (file_path, source_commit_sha, msg_type, created_by, status, received_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("/tmp/task.md", "abc", "task", "tester", "done", "2026-09-05T00:00:00Z"),
        )
        approval = svc.create_approval(
            message.lastrowid, "Add --version", "tester", "/tmp/task.md", "2026-09-05T00:00:00Z",
        )
        with svc._conn:
            svc._conn.execute(
                "UPDATE mailbox_task_approvals SET status=?, experiment_id=? WHERE id=?",
                ("running", exp.id, approval.id),
            )
        return svc, coordinator, approval, exp

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_needs_review_finalization_triggers_analysis(self):
        analyzer = MagicMock()
        svc, coordinator, approval, exp = self._service(
            FinalizationResult("needs_review", PHASE_NEEDS_REVIEW, "adoption requires review: stale evidence"),
            failure_analysis_service=analyzer,
        )
        self.addCleanup(svc.close)

        svc.poll_running()

        self.assertEqual(svc.get(approval.id).status, _STATUS_NEEDS_REVIEW)
        analyzer.analyze_from_logs.assert_called_once()
        kwargs = analyzer.analyze_from_logs.call_args.kwargs
        self.assertEqual(kwargs["subject_type"], "task_finalization")
        self.assertEqual(kwargs["experiment_id"], "experiment-1")
        self.assertEqual(kwargs["mailbox_task_id"], approval.id)
        self.assertEqual(kwargs["failure"]["status"], "needs_review")

    def test_succeeded_finalization_does_not_trigger_analysis(self):
        analyzer = MagicMock()
        svc, coordinator, approval, exp = self._service(
            FinalizationResult("succeeded", PHASE_SUCCEEDED, "live ok"),
            failure_analysis_service=analyzer,
        )
        self.addCleanup(svc.close)

        svc.poll_running()

        self.assertEqual(svc.get(approval.id).status, _STATUS_SUCCEEDED)
        analyzer.analyze_from_logs.assert_not_called()

    def test_analyzer_failure_never_blocks_finalization_needs_review(self):
        analyzer = MagicMock()
        analyzer.analyze_from_logs.side_effect = RuntimeError("boom")
        svc, coordinator, approval, exp = self._service(
            FinalizationResult("needs_review", PHASE_NEEDS_REVIEW, "adoption requires review"),
            failure_analysis_service=analyzer,
        )
        self.addCleanup(svc.close)

        svc.poll_running()  # must not raise

        self.assertEqual(svc.get(approval.id).status, _STATUS_NEEDS_REVIEW)
        self.assertTrue(list((self.tmp / "outbox").glob("*.md")))


if __name__ == "__main__":
    unittest.main()
