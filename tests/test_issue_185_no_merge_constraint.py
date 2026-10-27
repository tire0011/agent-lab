"""Regression coverage for PR #182: an explicit no-merge task stays open."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.github_work_tracking import (
    GitHubWorkRef,
    GitHubWorkTrackingError,
    GitHubWorkTrackingService,
)
from modules.mailbox.task_service import MailboxTaskService
from modules.task_finalization import FinalizationResult, PHASE_NEEDS_REVIEW


ISSUE_180_TASK = (
    "Open a PR from fix/180-outbox-message-object to main with Fixes #180 "
    "in the body. Do not merge it."
)


class NoMergeConstraintRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = str(self.tmp / "nexus.db")

    def _approval(self, service: MailboxTaskService):
        message = service._conn.execute(
            """INSERT INTO mailbox_messages
               (file_path, source_commit_sha, msg_type, created_by, status, received_at)
               VALUES ('inbox/180.md', 'sha', 'task', 'nexus', 'done', '2026-08-13T00:00:00Z')"""
        )
        service._conn.commit()
        return service.create_approval(
            message.lastrowid, ISSUE_180_TASK, "nexus", "inbox/180.md",
            "2026-08-13T00:00:00Z",
        )

    def test_pr_182_instruction_is_parsed_and_persisted(self) -> None:
        service = MailboxTaskService(
            db_path=self.db_path, repo_path=str(self.tmp),
            operations_registry=MagicMock(), github_work_tracking=MagicMock(),
        )
        self.addCleanup(service.close)

        approval = self._approval(service)
        reloaded = service.get(approval.id)

        self.assertEqual(reloaded.merge_forbidden, 1)
        self.assertIn("do not merge", reloaded.merge_constraint_reason.lower())

    def test_any_later_merge_request_is_refused_and_audited(self) -> None:
        service = MailboxTaskService(
            db_path=self.db_path, repo_path=str(self.tmp),
            operations_registry=MagicMock(), github_work_tracking=MagicMock(),
        )
        approval = self._approval(service)
        with service._conn:
            service._conn.execute(
                "UPDATE mailbox_task_approvals SET experiment_id='exp-182' WHERE id=?",
                (approval.id,),
            )
        service.close()

        calls = []

        def transport(method, path, payload):
            calls.append((method, path, payload))
            if method == "GET" and path.endswith("/pulls/182"):
                return {"number": 182, "merged": False}
            raise AssertionError(f"merge API must not be called: {method} {path}")

        tracking = GitHubWorkTrackingService(
            db_path=self.db_path, repo_root=str(self.tmp),
            repository="tire0011/agent-lab", transport=transport,
        )
        self.addCleanup(tracking.close)
        ref = GitHubWorkRef(
            issue_number=180, issue_title="Issue 180", branch_name="fix/180-outbox-message-object",
            source_type="task", source_id=approval.id, experiment_id="exp-182", pr_number=182,
        )

        with self.assertRaisesRegex(GitHubWorkTrackingError, "merge refused"):
            tracking.merge_and_complete(ref, expected_head_sha="head-sha")

        self.assertFalse(any(method == "PUT" and path.endswith("/merge") for method, path, _ in calls))
        audit = tracking._conn.execute(
            "SELECT success, error_summary FROM operations_audit WHERE operation='merge_pull_request'"
        ).fetchone()
        self.assertEqual(audit["success"], 0)
        self.assertIn("remains open for review", audit["error_summary"])

    def test_verified_task_finishes_ready_for_review_not_merged(self) -> None:
        coordinator = MagicMock()
        coordinator.advance.return_value = FinalizationResult(
            "needs_review", PHASE_NEEDS_REVIEW,
            "merge refused; pull request is ready for review and was not merged",
        )
        exp_service = MagicMock()
        exp_service.get_experiment.return_value = SimpleNamespace(
            id="exp-182", status="succeeded", quality_evaluation={"passed": True},
            test_exit_code=0, diff_stat="1 file changed", error=None,
        )
        service = MailboxTaskService(
            db_path=self.db_path, experiment_service=exp_service,
            outbox_dir=str(self.tmp / "outbox"), repo_path=str(self.tmp),
            operations_registry=MagicMock(), github_work_tracking=MagicMock(),
            finalization_coordinator=coordinator, task_log_dir=str(self.tmp / "logs"),
        )
        self.addCleanup(service.close)
        approval = self._approval(service)
        with service._conn:
            service._conn.execute(
                "UPDATE mailbox_task_approvals SET status='running', experiment_id='exp-182' WHERE id=?",
                (approval.id,),
            )

        service.poll_running()

        result = service.get(approval.id)
        self.assertEqual(result.status, "ready_for_review")
        self.assertIn("not merged", result.result_summary)
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()
