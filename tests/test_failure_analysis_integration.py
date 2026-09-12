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
import threading
import time
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
from modules.task_finalization import (
    FinalizationResult,
    PHASE_NEEDS_REVIEW,
    PHASE_SUCCEEDED,
    TaskFinalizationCoordinator,
)
from modules.experiment_adoption_service import STATUS_NOT_STARTED


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
        self.assertTrue(svc._wait_for_failure_analyses(timeout=2))

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
        self.assertTrue(svc._wait_for_failure_analyses(timeout=2))

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
        self.assertTrue(svc._wait_for_failure_analyses(timeout=2))

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
        self.assertTrue(svc._wait_for_failure_analyses(timeout=2))

        self.assertEqual(svc.get(approval_id).status, _STATUS_FAILED)
        analyzer.analyze_from_logs.assert_called_once()

    def test_poll_loop_continues_while_analysis_runs(self):
        analyzer = MagicMock()
        started = threading.Event()
        release = threading.Event()

        def blocking_analysis(**_kwargs):
            started.set()
            release.wait(2)

        analyzer.analyze_from_logs.side_effect = blocking_analysis
        exp_service = MagicMock()
        experiments = {
            "exp-a": _make_exp(id="exp-a", status="failed", error="boom-a"),
            "exp-b": _make_exp(id="exp-b", status="failed", error="boom-b"),
        }
        exp_service.get_experiment.side_effect = experiments.__getitem__
        svc = _make_service(self.tmp, exp_service, failure_analysis_service=analyzer)
        self.addCleanup(svc.close)
        approval_a = _submit(svc, "exp-a")
        approval_b = _submit(svc, "exp-b")

        try:
            before = time.monotonic()
            svc.poll_running()
            elapsed = time.monotonic() - before

            self.assertLess(elapsed, 1.0)
            self.assertEqual(svc.get(approval_a).status, _STATUS_FAILED)
            self.assertEqual(svc.get(approval_b).status, _STATUS_FAILED)
            self.assertTrue(started.wait(1))
        finally:
            release.set()
            self.assertTrue(svc._wait_for_failure_analyses(timeout=2))

        self.assertEqual(analyzer.analyze_from_logs.call_count, 2)

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
            self.assertTrue(svc._wait_for_failure_analyses(timeout=2))
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
            self.assertTrue(svc._wait_for_failure_analyses(timeout=2))
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
        self.assertTrue(svc._wait_for_failure_analyses(timeout=2))

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
        self.assertTrue(svc._wait_for_failure_analyses(timeout=2))

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
        self.assertTrue(svc._wait_for_failure_analyses(timeout=2))

        self.assertEqual(svc.get(approval.id).status, _STATUS_NEEDS_REVIEW)
        self.assertTrue(list((self.tmp / "outbox").glob("*.md")))


class _RaisingAdoptionService:
    """Always fails to start adoption, like a worktree-integrity mismatch (#150)."""

    def __init__(self, error: str):
        self.error = error
        self.starts = 0

    def get(self, experiment_id):
        return SimpleNamespace(
            status=STATUS_NOT_STARTED,
            reason=None,
            integrated_commit_sha=None,
            local_sync_pending=False,
        )

    def start_adoption(self, experiment_id, *, superseded=None):
        self.starts += 1
        raise RuntimeError(self.error)


class FinalizationResumedRecoveryIntegrationTests(unittest.TestCase):
    """A finalization needing review after resuming across a process restart (#150).

    The other ``FinalizationFailureIntegrationTests`` inject a
    ``FinalizationResult`` directly, exercising only the direct terminal
    transition. Production finalization instead accumulates retries across
    repeated ``advance()`` calls (a real ``TaskFinalizationCoordinator``,
    not a mock), and a process restart mid-finalization rebuilds both the
    ``FinalizingMailboxTaskService`` and its ``TaskFinalizationCoordinator``
    from the persisted ``task_finalizations`` row rather than reusing the
    original in-memory objects. This exercises that whole resumed path -
    matching the issue's concrete reproduction (adoption repeatedly failing
    with "could not start adoption: ...") - end to end into the analyzer.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = str(self.tmp / "nexus.db")

    def _service(self, *, finalization_coordinator, failure_analysis_service):
        exp = _make_exp(id="experiment-1", status="succeeded", test_exit_code=0)
        exp.quality_evaluation = {"outcome": "pass"}
        exp_service = MagicMock()
        exp_service.get_experiment.return_value = exp
        github = MagicMock()
        return MailboxTaskService(
            db_path=self.db_path,
            experiment_service=exp_service,
            outbox_dir=str(self.tmp / "outbox"),
            repo_path=str(self.tmp),
            operations_registry=MagicMock(),
            github_work_tracking=github,
            task_log_dir=str(self.tmp / "logs"),
            failure_analysis_service=failure_analysis_service,
            finalization_coordinator=finalization_coordinator,
        ), exp

    def _coordinator(self, adoption_service, *, max_retries):
        return TaskFinalizationCoordinator(
            experiment_service=MagicMock(),
            github_work_tracking=MagicMock(),
            db_path=self.db_path,
            repo_root=str(self.tmp),
            adoption_service=adoption_service,
            restart_runner=lambda services: None,
            live_validator=lambda approval, experiment: {"summary": "live ok"},
            max_retries=max_retries,
        )

    def test_needs_review_after_restart_mid_finalization_triggers_analysis(self):
        error = "could not start adoption: worktree changed since verification"

        # First process: one failed adoption attempt, then it "restarts"
        # before retries are exhausted, exactly as a deploy-triggered
        # restart would interrupt finalization mid-retry.
        coordinator_1 = self._coordinator(_RaisingAdoptionService(error), max_retries=1)
        svc_1, exp = self._service(
            finalization_coordinator=coordinator_1, failure_analysis_service=MagicMock()
        )
        message = svc_1._conn.execute(
            """INSERT INTO mailbox_messages
               (file_path, source_commit_sha, msg_type, created_by, status, received_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("/tmp/task.md", "abc", "task", "tester", "done", "2026-09-11T00:00:00Z"),
        )
        approval = svc_1.create_approval(
            message.lastrowid, "Add --version", "tester", "/tmp/task.md", "2026-09-11T00:00:00Z",
        )
        with svc_1._conn:
            svc_1._conn.execute(
                "UPDATE mailbox_task_approvals SET status=?, experiment_id=? WHERE id=?",
                ("running", exp.id, approval.id),
            )

        svc_1.poll_running()  # first attempt: adoption fails, retry budget not yet exhausted
        self.assertTrue(svc_1._wait_for_failure_analyses(timeout=2))

        self.assertEqual(svc_1.get(approval.id).status, "running")
        coordinator_1.close()
        svc_1.close()

        # Second process: a fresh service and a fresh coordinator, resuming
        # purely from the persisted db_path - nothing in-memory carries over.
        analyzer = MagicMock()
        coordinator_2 = self._coordinator(_RaisingAdoptionService(error), max_retries=1)
        svc_2, exp_2 = self._service(
            finalization_coordinator=coordinator_2, failure_analysis_service=analyzer
        )
        self.addCleanup(coordinator_2.close)
        self.addCleanup(svc_2.close)

        svc_2.poll_running()  # second attempt: retry budget now exhausted -> needs_review
        self.assertTrue(svc_2._wait_for_failure_analyses(timeout=2))

        self.assertEqual(svc_2.get(approval.id).status, _STATUS_NEEDS_REVIEW)
        analyzer.analyze_from_logs.assert_called_once()
        kwargs = analyzer.analyze_from_logs.call_args.kwargs
        self.assertEqual(kwargs["subject_type"], "task_finalization")
        self.assertEqual(kwargs["experiment_id"], "experiment-1")
        self.assertEqual(kwargs["mailbox_task_id"], approval.id)
        self.assertEqual(kwargs["failure"]["status"], "needs_review")
        self.assertIn("could not start adoption", kwargs["failure"]["reason"])


if __name__ == "__main__":
    unittest.main()
