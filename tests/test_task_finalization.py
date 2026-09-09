from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.experiment_adoption_service import (
    STATUS_ADOPTED,
    STATUS_FAILED,
    STATUS_NOT_STARTED,
    STATUS_QUEUED,
    StaleFailureAssessment,
)
from modules.mailbox.task_service import MailboxTaskService
from modules.task_finalization import (
    PHASE_MERGING,
    PHASE_RESTART_REQUESTED,
    PHASE_SUCCEEDED,
    FinalizationResult,
    TaskFinalizationCoordinator,
)


class _FakeAdoptionService:
    def __init__(self, adoption, *, assessment=None):
        self.adoption = adoption
        self.starts = 0
        self.assessments = 0
        self.superseded_arguments = []
        # Default: no failure is ever superseded, i.e. pre-#136 behaviour.
        self.assessment = assessment or StaleFailureAssessment(
            False, "no_ci_evidence", "no authoritative evidence"
        )

    def get(self, experiment_id):
        return self.adoption

    def assess_failure(self, experiment_id):
        self.assessments += 1
        return self.assessment

    def start_adoption(self, experiment_id, *, superseded=None):
        self.starts += 1
        self.superseded_arguments.append(superseded)
        self.adoption.status = STATUS_QUEUED
        return self.adoption


class TaskFinalizationCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = str(self.tmp / "nexus.db")
        self.approval = SimpleNamespace(id="approval-1", task_body="Add --version and expose it in /health")
        self.experiment = SimpleNamespace(id="experiment-1")

    def _coordinator(self, adoption_service, **kwargs):
        return TaskFinalizationCoordinator(
            experiment_service=MagicMock(),
            github_work_tracking=MagicMock(),
            db_path=self.db_path,
            repo_root=str(self.tmp),
            adoption_service=adoption_service,
            restart_runner=kwargs.pop("restart_runner", lambda services: None),
            live_validator=kwargs.pop(
                "live_validator", lambda approval, experiment: {"summary": "live ok"}
            ),
            **kwargs,
        )

    def _record_approval_status(self, coordinator, approval_id: str, status: str) -> None:
        now = "2026-09-08T00:00:00Z"
        cursor = coordinator._conn.execute(
            """
            INSERT INTO mailbox_messages
                (file_path, source_commit_sha, msg_type, created_by, status, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (f"/tmp/{approval_id}.md", approval_id, "task", "tester", "done", now),
        )
        coordinator._conn.execute(
            """
            INSERT INTO mailbox_task_approvals
                (id, mailbox_msg_id, task_body, sender, file_path, received_at,
                 status, experiment_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                cursor.lastrowid,
                "task",
                "tester",
                f"/tmp/{approval_id}.md",
                now,
                status,
                self.experiment.id,
                now,
                now,
            ),
        )
        coordinator._conn.commit()

    def test_verified_experiment_starts_existing_adoption_pipeline(self):
        adoption = SimpleNamespace(
            status=STATUS_NOT_STARTED,
            reason=None,
            integrated_commit_sha=None,
            local_sync_pending=False,
        )
        fake = _FakeAdoptionService(adoption)
        coordinator = self._coordinator(fake)
        self.addCleanup(coordinator.close)

        result = coordinator.advance(self.approval, self.experiment)

        self.assertEqual(result.status, "running")
        self.assertEqual(result.phase, PHASE_MERGING)
        self.assertEqual(fake.starts, 1)
        self.assertEqual(coordinator.get_phase(self.approval.id), PHASE_MERGING)

    def test_adopted_change_requests_restart_before_live_success(self):
        restart_calls = []
        adoption = SimpleNamespace(
            status=STATUS_ADOPTED,
            reason=None,
            integrated_commit_sha="merge-sha",
            local_sync_pending=False,
        )
        coordinator = self._coordinator(
            _FakeAdoptionService(adoption),
            restart_runner=lambda services: restart_calls.append(services),
            process_started_at=time.time(),
        )
        self.addCleanup(coordinator.close)

        result = coordinator.advance(self.approval, self.experiment)

        self.assertEqual(result.status, "running")
        self.assertEqual(result.phase, PHASE_RESTART_REQUESTED)
        self.assertEqual(restart_calls, [["nexus-mailbox", "agent-api"]])

    def test_restart_resume_runs_live_validation_and_finishes(self):
        adoption = SimpleNamespace(
            status=STATUS_ADOPTED,
            reason=None,
            integrated_commit_sha="merge-sha",
            local_sync_pending=False,
        )
        fake = _FakeAdoptionService(adoption)
        first = self._coordinator(
            fake,
            restart_runner=lambda services: None,
            process_started_at=time.time(),
        )
        first_result = first.advance(self.approval, self.experiment)
        self.assertEqual(first_result.phase, PHASE_RESTART_REQUESTED)
        first.close()

        live_calls = []
        resumed = self._coordinator(
            fake,
            process_started_at=time.time() + 60,
            live_validator=lambda approval, experiment: (
                live_calls.append((approval.id, experiment.id))
                or {"summary": "health and version match"}
            ),
        )
        self.addCleanup(resumed.close)

        result = resumed.advance(self.approval, self.experiment)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.phase, PHASE_SUCCEEDED)
        self.assertEqual(live_calls, [(self.approval.id, self.experiment.id)])
        self.assertEqual(resumed.get_phase(self.approval.id), PHASE_SUCCEEDED)

    def test_new_approval_rebinds_existing_experiment_state_and_resets_attempt_errors(self):
        adoption = SimpleNamespace(
            status=STATUS_ADOPTED,
            reason=None,
            integrated_commit_sha="merge-sha",
            local_sync_pending=False,
        )
        coordinator = self._coordinator(_FakeAdoptionService(adoption))
        self.addCleanup(coordinator.close)

        state = coordinator._get_or_create(self.approval.id, self.experiment.id)
        state.phase = PHASE_RESTART_REQUESTED
        state.restart_requested_at = 123.5
        state.retry_count = 2
        state.merge_commit = "existing-merge"
        state.live_validation_json = '{"previous": true}'
        state.last_error = "previous error"
        coordinator._save(state)

        rebound = coordinator._get_or_create("approval-2", self.experiment.id)

        self.assertEqual(rebound.approval_id, "approval-2")
        self.assertEqual(rebound.phase, PHASE_RESTART_REQUESTED)
        self.assertEqual(rebound.restart_requested_at, 123.5)
        self.assertEqual(rebound.retry_count, 0)
        self.assertEqual(rebound.merge_commit, "existing-merge")
        self.assertEqual(rebound.live_validation_json, '{"previous": true}')
        self.assertIsNone(rebound.last_error)
        self.assertIsNone(coordinator.get_phase(self.approval.id))
        self.assertEqual(coordinator.get_phase("approval-2"), PHASE_RESTART_REQUESTED)
        count = coordinator._conn.execute(
            "SELECT COUNT(*) FROM task_finalizations WHERE experiment_id=?",
            (self.experiment.id,),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_rebound_approval_gets_fresh_retry_budget_for_failed_adoption(self):
        adoption = SimpleNamespace(
            status=STATUS_FAILED,
            reason="previous attempt failed",
            integrated_commit_sha=None,
            local_sync_pending=False,
        )
        fake = _FakeAdoptionService(adoption)
        coordinator = self._coordinator(fake)
        self.addCleanup(coordinator.close)

        state = coordinator._get_or_create(self.approval.id, self.experiment.id)
        state.retry_count = coordinator.max_retries
        state.last_error = "old approval exhausted retries"
        coordinator._save(state)

        new_approval = SimpleNamespace(id="approval-2", task_body=self.approval.task_body)
        result = coordinator.advance(new_approval, self.experiment)

        self.assertEqual(result.status, "running")
        self.assertEqual(result.phase, PHASE_MERGING)
        self.assertEqual(fake.starts, 1)
        rebound = coordinator._get("approval-2")
        self.assertEqual(rebound.retry_count, 1)
        self.assertIsNone(rebound.last_error)

    def test_repeated_new_approvals_reuse_one_finalization_row_idempotently(self):
        adoption = SimpleNamespace(
            status=STATUS_ADOPTED,
            reason=None,
            integrated_commit_sha="merge-sha",
            local_sync_pending=False,
        )
        coordinator = self._coordinator(_FakeAdoptionService(adoption))
        self.addCleanup(coordinator.close)

        state = coordinator._get_or_create(self.approval.id, self.experiment.id)
        state.phase = PHASE_MERGING
        state.retry_count = 1
        coordinator._save(state)

        second = coordinator._get_or_create("approval-2", self.experiment.id)
        same_second = coordinator._get_or_create("approval-2", self.experiment.id)
        third = coordinator._get_or_create("approval-3", self.experiment.id)

        self.assertEqual(second.phase, PHASE_MERGING)
        self.assertEqual(second.retry_count, 0)
        self.assertEqual(same_second.approval_id, "approval-2")
        self.assertEqual(third.approval_id, "approval-3")
        self.assertEqual(third.retry_count, 0)
        rows = coordinator._conn.execute(
            "SELECT approval_id, phase, retry_count FROM task_finalizations WHERE experiment_id=?",
            (self.experiment.id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["approval_id"], "approval-3")
        self.assertEqual(rows[0]["phase"], PHASE_MERGING)
        self.assertEqual(rows[0]["retry_count"], 0)

    def test_active_approval_blocks_concurrent_rebind(self):
        adoption = SimpleNamespace(
            status=STATUS_ADOPTED,
            reason=None,
            integrated_commit_sha="merge-sha",
            local_sync_pending=False,
        )
        coordinator = self._coordinator(_FakeAdoptionService(adoption))
        self.addCleanup(coordinator.close)

        state = coordinator._get_or_create(self.approval.id, self.experiment.id)
        state.phase = PHASE_MERGING
        coordinator._save(state)
        self._record_approval_status(coordinator, self.approval.id, "running")

        with self.assertRaisesRegex(RuntimeError, "already owned by an active approval"):
            coordinator._get_or_create("approval-2", self.experiment.id)

        self.assertEqual(coordinator.get_phase(self.approval.id), PHASE_MERGING)
        self.assertIsNone(coordinator.get_phase("approval-2"))

    def test_terminal_previous_approval_allows_rebind(self):
        adoption = SimpleNamespace(
            status=STATUS_ADOPTED,
            reason=None,
            integrated_commit_sha="merge-sha",
            local_sync_pending=False,
        )
        coordinator = self._coordinator(_FakeAdoptionService(adoption))
        self.addCleanup(coordinator.close)

        state = coordinator._get_or_create(self.approval.id, self.experiment.id)
        state.phase = PHASE_RESTART_REQUESTED
        coordinator._save(state)
        self._record_approval_status(coordinator, self.approval.id, "needs_review")

        rebound = coordinator._get_or_create("approval-2", self.experiment.id)

        self.assertEqual(rebound.approval_id, "approval-2")
        self.assertEqual(rebound.phase, PHASE_RESTART_REQUESTED)

    def test_stale_approval_cannot_save_after_finalization_is_rebound(self):
        adoption = SimpleNamespace(
            status=STATUS_ADOPTED,
            reason=None,
            integrated_commit_sha="merge-sha",
            local_sync_pending=False,
        )
        coordinator = self._coordinator(_FakeAdoptionService(adoption))
        self.addCleanup(coordinator.close)

        stale = coordinator._get_or_create(self.approval.id, self.experiment.id)
        coordinator._get_or_create("approval-2", self.experiment.id)
        stale.phase = PHASE_SUCCEEDED

        with self.assertRaisesRegex(RuntimeError, "ownership changed"):
            coordinator._save(stale)

        self.assertEqual(coordinator.get_phase("approval-2"), "verified")

    def test_new_approval_continues_from_restart_phase_through_live_validation(self):
        adoption = SimpleNamespace(
            status=STATUS_ADOPTED,
            reason=None,
            integrated_commit_sha="merge-sha",
            local_sync_pending=False,
        )
        fake = _FakeAdoptionService(adoption)
        first = self._coordinator(fake, process_started_at=100.0)
        state = first._get_or_create(self.approval.id, self.experiment.id)
        state.phase = PHASE_RESTART_REQUESTED
        state.restart_requested_at = 150.0
        state.retry_count = 1
        state.merge_commit = "merge-sha"
        first._save(state)
        first.close()

        restart_calls = []
        live_calls = []
        resumed = self._coordinator(
            fake,
            process_started_at=200.0,
            restart_runner=lambda services: restart_calls.append(services),
            live_validator=lambda approval, experiment: (
                live_calls.append((approval.id, experiment.id))
                or {"summary": "live after approval rebind"}
            ),
        )
        self.addCleanup(resumed.close)
        new_approval = SimpleNamespace(id="approval-2", task_body=self.approval.task_body)

        result = resumed.advance(new_approval, self.experiment)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.phase, PHASE_SUCCEEDED)
        self.assertEqual(restart_calls, [])
        self.assertEqual(live_calls, [("approval-2", self.experiment.id)])
        self.assertEqual(resumed.get_phase("approval-2"), PHASE_SUCCEEDED)


class FinalizingMailboxTaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _service(self, result: FinalizationResult):
        exp = MagicMock()
        exp.id = "experiment-1"
        exp.status = "succeeded"
        exp.quality_evaluation = {"outcome": "pass"}
        exp.test_command = ["python", "-m", "unittest"]
        exp.test_exit_code = 0
        exp.test_output = "ok"
        exp.diff_stat = "1 file changed"
        exp.error = None
        exp_service = MagicMock()
        exp_service.get_experiment.return_value = exp
        coordinator = MagicMock()
        coordinator.advance.return_value = result
        github = MagicMock()
        service = MailboxTaskService(
            db_path=str(self.tmp / "task.db"),
            experiment_service=exp_service,
            outbox_dir=str(self.tmp / "outbox"),
            repo_path=str(self.tmp),
            operations_registry=MagicMock(),
            github_work_tracking=github,
            finalization_coordinator=coordinator,
            task_log_dir=str(self.tmp / "logs"),
        )
        message = service._conn.execute(
            """INSERT INTO mailbox_messages
               (file_path, source_commit_sha, msg_type, created_by, status, received_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("/tmp/task.md", "abc", "task", "tester", "done", "2026-09-05T00:00:00Z"),
        )
        approval = service.create_approval(
            message.lastrowid,
            "Add --version and /health version",
            "tester",
            "/tmp/task.md",
            "2026-09-05T00:00:00Z",
        )
        with service._conn:
            service._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, experiment_id=? WHERE id=?""",
                ("running", exp.id, approval.id),
            )
        return service, coordinator, approval, exp

    def test_verification_success_is_not_task_success_while_finalizing(self):
        service, coordinator, approval, exp = self._service(
            FinalizationResult("running", PHASE_MERGING, "adoption started")
        )
        self.addCleanup(service.close)

        service.poll_running()

        current = service.get(approval.id)
        self.assertEqual(current.status, "running")
        self.assertIn("finalization=merging", current.result_summary)
        coordinator.advance.assert_called_once()
        self.assertEqual(list((self.tmp / "outbox").glob("*.md")), [])

    def test_task_succeeds_only_after_live_validation_result(self):
        service, coordinator, approval, exp = self._service(
            FinalizationResult("succeeded", PHASE_SUCCEEDED, "health and version match")
        )
        self.addCleanup(service.close)

        service.poll_running()

        current = service.get(approval.id)
        self.assertEqual(current.status, "succeeded")
        self.assertIn("finalization=succeeded", current.result_summary)
        self.assertTrue(list((self.tmp / "outbox").glob("*.md")))


if __name__ == "__main__":
    unittest.main()
