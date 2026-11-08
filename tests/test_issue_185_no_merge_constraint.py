"""Regression coverage for PR #182: an explicit no-merge task stays open."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.experiment_adoption_service import (
    STATUS_ADOPTING,
    STATUS_READY_FOR_REVIEW,
    ExperimentAdoptionService,
)
from modules.github_work_tracking import (
    GitHubWorkRef,
    GitHubWorkTrackingError,
    GitHubWorkTrackingService,
    MergeForbiddenError,
)
from modules.mailbox.core import MailboxMessage
from modules.mailbox.processing import _process_task
from modules.mailbox.task_service import MailboxTaskService
from modules.run_store import init_db
from modules.task_finalization import (
    FinalizationResult,
    PHASE_READY_FOR_REVIEW,
    TaskFinalizationCoordinator,
)
from modules.work_directives import parse_work_directives
from tests.test_experiment_adoption_service import AdoptionRepoFixture
from tests.test_nexus_initiative import InitiativeFixture


ISSUE_180_TASK = (
    "Open a PR from fix/180-outbox-message-object to main with Fixes #180 "
    "in the body. Do not merge it."
)


def _phase_of(coordinator, approval_id):
    state = coordinator._get(approval_id)
    return state.phase if state is not None else None

def _wait_for_thread(service, experiment_id, timeout=None):
    thread = service._threads.get(experiment_id)
    if thread is not None:
        thread.join(timeout)

def _insert_approval(db_path: str, *, body: str, experiment_id: str | None, approval_id: str = "a182") -> None:
    """A task row as an older release stored it: the constraint not parsed yet."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO mailbox_messages
               (file_path, source_commit_sha, msg_type, created_by, status, received_at)
               VALUES (?, 'sha', 'task', 'nexus', 'done', '2026-08-13T00:00:00Z')""",
            (f"inbox/{approval_id}.md",),
        )
        conn.execute(
            """INSERT INTO mailbox_task_approvals
               (id, mailbox_msg_id, task_body, sender, file_path, received_at,
                status, experiment_id, created_at, updated_at)
               VALUES (?, ?, ?, 'nexus', 'inbox/180.md', '2026-08-13T00:00:00Z',
                       'running', ?, '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')""",
            (approval_id, cursor.lastrowid, body, experiment_id),
        )


class ParsingTests(unittest.TestCase):
    def test_instructions_that_forbid_the_merge(self) -> None:
        for text in (
            ISSUE_180_TASK,
            "don't merge this",
            "Never auto-merge the result.",
            "Push the branch without merging.",
            "Leave the PR open for review.",
            "Stop before the merge.",
            "Bitte nicht mergen.",
        ):
            with self.subTest(text=text):
                directives = parse_work_directives(text)
                self.assertTrue(directives.forbid_merge)
                self.assertIn("explicit task constraint", directives.merge_constraint_reason)

    def test_ordinary_tasks_stay_mergeable(self) -> None:
        for text in (
            "Merge the fix once CI is green.",
            "Fix the merge conflict handling.",
            "Do not create a new issue.",
            None,
        ):
            with self.subTest(text=text):
                directives = parse_work_directives(text)
                self.assertFalse(directives.forbid_merge)
                self.assertIsNone(directives.merge_constraint_reason)


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = str(self.tmp / "nexus.db")

    def test_pr_182_instruction_is_parsed_and_persisted(self) -> None:
        service = MailboxTaskService(
            db_path=self.db_path, repo_path=str(self.tmp),
            operations_registry=MagicMock(), github_work_tracking=MagicMock(),
        )
        self.addCleanup(service.close)
        message = service._conn.execute(
            """INSERT INTO mailbox_messages
               (file_path, source_commit_sha, msg_type, created_by, status, received_at)
               VALUES ('inbox/180.md', 'sha', 'task', 'nexus', 'done', '2026-08-13T00:00:00Z')"""
        )
        service._conn.commit()

        # create_task on main, create_approval before the mailbox-to-Nexus handover.
        create = getattr(service, "create_task", None) or service.create_approval
        approval = create(
            message.lastrowid, ISSUE_180_TASK, "nexus", "inbox/180.md", "2026-08-13T00:00:00Z",
        )
        reloaded = service.get(approval.id)

        self.assertEqual(reloaded.merge_forbidden, 1)
        self.assertIn("do not merge", reloaded.merge_constraint_reason.lower())

    def test_mailbox_intake_persists_the_constraint(self) -> None:
        init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        message_id = conn.execute(
            """INSERT INTO mailbox_messages
               (file_path, source_commit_sha, msg_type, created_by, status, received_at)
               VALUES ('inbox/180.md', 'sha', 'task', 'nexus', 'done', '2026-08-13T00:00:00Z')"""
        ).lastrowid
        conn.commit()

        _process_task(
            MailboxMessage(Path("inbox/180.md"), "task", "nexus", {}, ISSUE_180_TASK),
            self.tmp, conn, message_id,
        )

        forbidden = conn.execute("SELECT merge_forbidden FROM mailbox_task_approvals").fetchone()[0]
        self.assertEqual(forbidden, 1)

    def test_restart_backfills_an_older_task(self) -> None:
        init_db(self.db_path)
        _insert_approval(self.db_path, body=ISSUE_180_TASK, experiment_id=None)

        init_db(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            forbidden = conn.execute("SELECT merge_forbidden FROM mailbox_task_approvals").fetchone()[0]
        self.assertEqual(forbidden, 1)


class MergeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = str(self.tmp / "nexus.db")
        init_db(self.db_path)
        self.calls: list[tuple] = []
        self.merged = False
        self.tracking = GitHubWorkTrackingService(
            db_path=self.db_path, repo_root=str(self.tmp),
            repository="tire0011/agent-lab", transport=self._transport,
        )
        self.addCleanup(self.tracking.close)

    def _transport(self, method, path, payload):
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/pulls/182"):
            return {"number": 182, "merged": self.merged, "merge_commit_sha": "human-sha"}
        if method == "PUT" and path.endswith("/merge"):
            return {"merged": True, "sha": "merge-sha"}
        return {}

    def _ref(self, **overrides) -> GitHubWorkRef:
        values = {
            "issue_number": 180, "issue_title": "Issue 180",
            "branch_name": "fix/180-outbox-message-object",
            "source_type": "task", "source_id": "a182",
            "experiment_id": "exp-182", "pr_number": 182,
        }
        return GitHubWorkRef(**{**values, **overrides})

    def _merge_calls(self) -> list[tuple]:
        return [call for call in self.calls if call[0] == "PUT" and call[1].endswith("/merge")]

    def test_any_merge_request_is_refused_and_audited(self) -> None:
        _insert_approval(self.db_path, body=ISSUE_180_TASK, experiment_id="exp-182")
        init_db(self.db_path)

        with self.assertRaisesRegex(GitHubWorkTrackingError, "merge refused"):
            self.tracking.merge_and_complete(self._ref(), expected_head_sha="head-sha")

        self.assertEqual(self._merge_calls(), [])
        audit = self.tracking._conn.execute(
            "SELECT success, error_summary FROM operations_audit WHERE operation='merge_pull_request'"
        ).fetchone()
        self.assertEqual(audit["success"], 0)
        self.assertIn("remains open for review", audit["error_summary"])

    def test_a_retry_rebinding_the_task_keeps_the_old_experiment_refused(self) -> None:
        _insert_approval(self.db_path, body=ISSUE_180_TASK, experiment_id="exp-retry")
        init_db(self.db_path)

        with self.assertRaises(MergeForbiddenError):
            self.tracking.merge_and_complete(self._ref(experiment_id="exp-old"))

        self.assertEqual(self._merge_calls(), [])

    def test_another_workflow_on_the_same_pull_request_is_refused(self) -> None:
        _insert_approval(self.db_path, body=ISSUE_180_TASK, experiment_id="exp-182")
        init_db(self.db_path)
        with self.tracking._conn:
            self.tracking._conn.execute(
                """INSERT INTO github_experiment_links
                   (experiment_id, issue_number, issue_title, branch_name, pr_number, created_at, updated_at)
                   VALUES ('exp-182', 180, 'Issue 180', 'fix/180', 182, 'now', 'now')"""
            )

        with self.assertRaises(MergeForbiddenError):
            self.tracking.merge_and_complete(
                self._ref(source_type="issue", source_id="180", experiment_id="exp-initiative")
            )

        self.assertEqual(self._merge_calls(), [])

    def test_an_instruction_added_after_intake_still_counts(self) -> None:
        _insert_approval(self.db_path, body="Fix #180.", experiment_id="exp-182")
        init_db(self.db_path)
        with self.tracking._conn:
            self.tracking._conn.execute(
                "UPDATE mailbox_task_approvals SET task_body=?", (f"Fix #180.\n\n{ISSUE_180_TASK}",)
            )

        self.assertIn("do not merge", self.tracking.merge_refusal(self._ref()).lower())

    def test_a_task_without_the_constraint_merges(self) -> None:
        _insert_approval(self.db_path, body="Fix #180 and merge it.", experiment_id="exp-182")
        init_db(self.db_path)

        self.assertEqual(self.tracking.merge_and_complete(self._ref()), "merge-sha")
        self.assertEqual(len(self._merge_calls()), 1)

    def test_a_pull_request_a_human_merged_is_reported_not_refused(self) -> None:
        _insert_approval(self.db_path, body=ISSUE_180_TASK, experiment_id="exp-182")
        init_db(self.db_path)
        self.merged = True

        self.assertEqual(self.tracking.merge_and_complete(self._ref()), "human-sha")
        self.assertEqual(self._merge_calls(), [])


class AdoptionScenarioTests(unittest.TestCase):
    """The PR #182 run: real git, real pipeline, green verification."""

    experiment_id = "b1" * 16

    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)
        self.fx.create_experiment(experiment_id=self.experiment_id, changed_files=["app.py"])
        _insert_approval(self.fx.db_path, body=ISSUE_180_TASK, experiment_id=self.experiment_id)
        init_db(self.fx.db_path)

    def _adopt(self):
        self.fx.adoption_service.start_adoption(self.experiment_id)
        _wait_for_thread(self.fx.adoption_service, self.experiment_id, timeout=30)
        return self.fx.wait_terminal(self.experiment_id, timeout=30)

    def _merge_calls(self) -> list[tuple]:
        return [
            call for call in self.fx.github_api.calls
            if call[0] == "PUT" and call[1].endswith("/merge")
        ]

    def _refusals(self) -> int:
        with sqlite3.connect(self.fx.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM operations_audit "
                "WHERE operation='merge_pull_request' AND success=0"
            ).fetchone()[0]

    def test_green_run_opens_the_pull_request_and_stops_before_the_merge(self) -> None:
        adoption = self._adopt()

        self.assertEqual(adoption.status, STATUS_READY_FOR_REVIEW)
        self.assertIn("merge refused", adoption.reason)
        self.assertEqual(self._merge_calls(), [])
        self.assertIsNotNone(self.fx.github_work_tracking.for_experiment(self.experiment_id).pr_number)
        self.assertEqual(self._refusals(), 1)

    def test_a_later_adoption_request_is_refused_again(self) -> None:
        self._adopt()

        adoption = self._adopt()

        self.assertEqual(adoption.status, STATUS_READY_FOR_REVIEW)
        self.assertEqual(self._merge_calls(), [])
        self.assertEqual(self._refusals(), 2)

    def test_restart_mid_adoption_keeps_the_constraint(self) -> None:
        self._adopt()
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("UPDATE experiment_adoptions SET status=?", (STATUS_ADOPTING,))
        restarted = ExperimentAdoptionService(
            experiment_service=self.fx.experiment_service,
            db_path=self.fx.db_path,
            repo_root=str(self.fx.repo),
            worktrees_root=str(self.fx.worktrees),
            lock_path=self.fx.lock_path,
            github_work_tracking=self.fx.github_work_tracking,
        )
        self.addCleanup(restarted.close)

        restarted.recover_after_restart()

        _wait_for_thread(restarted, self.experiment_id, timeout=30)
        self.assertEqual(restarted.get(self.experiment_id).status, STATUS_READY_FOR_REVIEW)
        self.assertEqual(self._merge_calls(), [])


class FinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = str(self.tmp / "nexus.db")

    def test_coordinator_reports_ready_for_review_without_deploying(self) -> None:
        adoption_service = MagicMock()
        adoption_service.get.return_value = SimpleNamespace(
            status=STATUS_READY_FOR_REVIEW, reason="merge refused: do not merge",
        )
        init_db(self.db_path)
        coordinator = TaskFinalizationCoordinator(
            experiment_service=MagicMock(), github_work_tracking=MagicMock(),
            adoption_service=adoption_service, db_path=self.db_path, repo_root=str(self.tmp),
        )
        self.addCleanup(coordinator.close)

        result = coordinator.advance(SimpleNamespace(id="a182"), SimpleNamespace(id="exp-182"))

        self.assertEqual(result.status, "ready_for_review")
        self.assertEqual(_phase_of(coordinator, "a182"), PHASE_READY_FOR_REVIEW)
        self.assertIn("not merged", result.summary)
        adoption_service.start_adoption.assert_not_called()

    def test_verified_task_finishes_ready_for_review_not_merged(self) -> None:
        coordinator = MagicMock()
        coordinator.advance.return_value = FinalizationResult(
            "ready_for_review", PHASE_READY_FOR_REVIEW,
            "pull request open, not merged: merge refused",
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
        _insert_approval(self.db_path, body=ISSUE_180_TASK, experiment_id="exp-182")

        service.poll_running()

        result = service.get("a182")
        self.assertEqual(result.status, "ready_for_review")
        self.assertIn("ready_for_review", result.result_summary)
        self.assertIn("not merged", result.result_summary)
        self.assertIsNone(result.error)

    def test_a_real_review_stop_of_a_no_merge_task_stays_needs_review(self) -> None:
        coordinator = MagicMock()
        coordinator.advance.return_value = FinalizationResult(
            "needs_review", "needs_review", "adoption requires review: conflict",
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
        _insert_approval(self.db_path, body=ISSUE_180_TASK, experiment_id="exp-182")

        with patch.object(service, "_schedule_failure_analysis"):
            service.poll_running()

        self.assertEqual(service.get("a182").status, "needs_review")


class InitiativeTests(unittest.TestCase):
    def test_nexus_neither_retries_nor_resumes_the_refused_merge(self) -> None:
        fx = InitiativeFixture()
        fx.add_experiment("e1", "succeeded", adoption="ready_for_review")
        with sqlite3.connect(fx.db_path) as conn:
            conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES ('e1', 180)"
            )
        service = fx.service(issue_lister=lambda: [{"number": 180, "author": {"login": "tire0011"}, "title": "Issue 180", "body": ""}])
        self.addCleanup(service.close)

        service.tick()

        self.assertEqual(fx.controller.issues, [])
        self.assertEqual(fx.adoption.started, [])


if __name__ == "__main__":
    unittest.main()
