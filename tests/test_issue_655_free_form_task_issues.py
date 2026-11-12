"""Issue #655: free-form mailbox tasks that change something get a planned issue.

Fake GitHub and a fake planner: a change task works on the planned issue,
machine work (a mutating command) gets an issue and a result comment, and
questions and read-only commands stay issue-free.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.github_work_tracking import GitHubWorkTrackingService
from modules.mailbox.task_service import MailboxTaskService
from modules.run_store import init_db
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_COMMAND,
    ACTION_NEW_EXPERIMENT,
    TaskDecision,
)
from tests.test_github_work_tracking import FakeGitHub
from tests.test_mailbox_task_service import _insert_mailbox_msg, _make_exp

REPO = "/repos/tire0011/agent-lab"
PLANNED_BODY = (
    "## Goal\nAdd a retry.\n\n## Current state\nNo retry.\n\n"
    "## Plan\nAdd it.\n\n## Acceptance criteria\n- Retries once."
)


class FakePlanner:
    def __init__(self, draft):
        self.draft = draft
        self.calls: list[str] = []

    def __call__(self, task_text):
        self.calls.append(task_text)
        return self.draft


class FreeFormTaskIssueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.fake = FakeGitHub()
        self.fake.issues[288] = {"number": 288, "title": "Existing work", "state": "open"}
        self.db_path = str(self.tmp / "test.db")
        init_db(self.db_path).close()
        self.tracker = GitHubWorkTrackingService(
            db_path=self.db_path,
            repo_root=str(self.tmp),
            repository="tire0011/agent-lab",
            transport=self.fake,
        )
        self.experiment_service = MagicMock()
        self.experiment_service.create_experiment.return_value = _make_exp(id="exp655")
        self.experiment_service.experiment_log_dir = str(self.tmp / "logs")
        self.planner = FakePlanner({"title": "Add a retry to the mailbox poller", "body": PLANNED_BODY})
        self.decision = TaskDecision(action=ACTION_NEW_EXPERIMENT, reason="test")
        test = self

        class Orchestrator:
            def __init__(self, **kwargs):
                pass

            def run(self, task, **kwargs):
                return test.decision

            def close(self):
                return None

        registry = MagicMock()
        registry.catalog.return_value = []
        self.service = MailboxTaskService(
            db_path=self.db_path,
            experiment_service=self.experiment_service,
            outbox_dir=str(self.tmp / "outbox"),
            repo_path=str(self.tmp),
            operations_registry=registry,
            orchestrator_factory=Orchestrator,
            github_work_tracking=self.tracker,
            task_log_dir=str(self.tmp / "logs"),
            task_issue_planner=self.planner,
        )
        # The experiment itself is not under test; keep its thread out.
        self.service._start_thread = lambda experiment_id, target: None

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def _run(self, body, command_output="**Exit code:** 0\n"):
        message_id = _insert_mailbox_msg(self.service)
        approval = self.service.create_task(
            message_id, body, "alice", "/agent/inbox/task.md", "2026-01-01T00:00:00Z"
        )
        with patch(
            "modules.mailbox.task_service.process_command",
            return_value=(command_output, None),
        ) as run:
            self.service.submit_queued()
        self.command_runs = run.call_count
        return self.service.get(approval.id)

    def _created_issues(self):
        return [
            payload
            for method, path, payload in self.fake.calls
            if method == "POST" and path == f"{REPO}/issues"
        ]

    def _created_branches(self):
        return [
            payload["ref"]
            for method, path, payload in self.fake.calls
            if method == "POST" and path == f"{REPO}/git/refs"
        ]

    def test_change_task_gets_the_planned_issue_and_the_experiment_names_it(self):
        self._run("Add a retry to the mailbox poller.")

        self.assertEqual(self.planner.calls, ["Add a retry to the mailbox poller."])
        created = self._created_issues()
        self.assertEqual([issue["title"] for issue in created], ["Add a retry to the mailbox poller"])
        self.assertIn("## Acceptance criteria", created[0]["body"])
        self.assertIn("Add a retry to the mailbox poller.", created[0]["body"])
        goal = self.experiment_service.create_experiment.call_args.args[0]
        self.assertTrue(goal.startswith("GitHub Issue #43: Add a retry to the mailbox poller\n"))
        self.assertEqual(self.tracker.for_experiment("exp655").issue_number, 43)

    def test_planner_none_keeps_the_placeholder_issue(self):
        self.planner.draft = None

        result = self._run("Add a retry to the mailbox poller.")

        self.assertEqual(len(self.planner.calls), 1)
        self.assertEqual(
            [issue["title"] for issue in self._created_issues()],
            [f"Mailbox task {result.id[:8]}"],
        )
        goal = self.experiment_service.create_experiment.call_args.args[0]
        self.assertEqual(goal, "Add a retry to the mailbox poller.")

    def test_solve_github_issue_makes_no_planner_call(self):
        self._run("solve github issue #288")

        self.assertEqual(self.planner.calls, [])
        self.assertEqual(self._created_issues(), [])
        self.assertEqual(self.tracker.for_experiment("exp655").issue_number, 288)
        goal = self.experiment_service.create_experiment.call_args.args[0]
        self.assertEqual(goal, "solve github issue #288")

    def test_mutating_command_gets_an_issue_and_a_result_comment(self):
        self.decision = TaskDecision(action=ACTION_COMMAND, command="git branch x")

        result = self._run("git branch x")

        self.assertEqual(self.command_runs, 1)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(self._created_issues()), 1)
        self.assertEqual(len(self.fake.comments), 1)
        number, body = self.fake.comments[0]
        self.assertEqual(number, 43)
        self.assertIn("git branch x", body)
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("issue #43", result.result_summary)
        # Machine work needs the issue, not a work branch.
        self.assertEqual(self._created_branches(), [])
        self.experiment_service.create_experiment.assert_not_called()

    def test_failed_mutating_command_still_comments_its_result(self):
        self.decision = TaskDecision(action=ACTION_COMMAND, command="git branch x")

        result = self._run("git branch x", command_output="**Exit code:** 128\nfatal")

        self.assertEqual(result.status, "failed")
        self.assertEqual(len(self.fake.comments), 1)
        self.assertIn("failed", self.fake.comments[0][1])

    def test_tracking_failure_blocks_mutating_command(self):
        self.decision = TaskDecision(action=ACTION_COMMAND, command="git branch x")
        with patch.object(
            self.tracker, "ensure_for_source", side_effect=RuntimeError("database unavailable")
        ):
            result = self._run("git branch x")

        self.assertEqual(self.command_runs, 0)
        self.assertEqual(result.status, "needs_review")
        self.assertNotIn("Tracked on GitHub", result.result_summary)

    def test_result_comment_failure_is_durable_and_retried_without_rerunning(self):
        self.decision = TaskDecision(action=ACTION_COMMAND, command="git branch x")
        self.fake.fail_comments = True

        pending = self._run("git branch x")

        self.assertEqual(self.command_runs, 1)
        self.assertEqual(pending.status, "result_comment_pending")
        self.assertNotIn("Tracked on GitHub", pending.result_summary or "")
        self.fake.fail_comments = False
        with patch("modules.mailbox.task_service.process_command") as rerun:
            self.service.submit_queued()
        done = self.service.get(pending.id)
        self.assertEqual(done.status, "succeeded")
        rerun.assert_not_called()
        self.assertEqual(len(self.fake.comments), 1)
        self.assertIn("Tracked on GitHub issue #43", done.result_summary)

    def test_restart_retries_pending_comment_without_rerunning(self):
        self.decision = TaskDecision(action=ACTION_COMMAND, command="git branch x")
        self.fake.fail_comments = True
        pending = self._run("git branch x")

        self.service.recover_after_restart()
        self.assertEqual(self.service.get(pending.id).status, "result_comment_pending")
        self.fake.fail_comments = False
        with patch("modules.mailbox.task_service.process_command") as rerun:
            self.service.submit_queued()
        rerun.assert_not_called()
        self.assertEqual(self.service.get(pending.id).status, "succeeded")

    def test_accepted_then_raised_comment_is_reconciled_without_duplicate(self):
        self.decision = TaskDecision(action=ACTION_COMMAND, command="git branch x")
        self.fake.accept_comment_then_raise = True

        result = self._run("git branch x")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(self.fake.comments), 1)
        self.service.submit_queued()
        self.assertEqual(len(self.fake.comments), 1)

    def test_interruption_after_tracking_does_not_repeat_unknown_mutation(self):
        self.decision = TaskDecision(action=ACTION_COMMAND, command="git branch x")
        message_id = _insert_mailbox_msg(self.service)
        approval = self.service.create_task(
            message_id, "git branch x", "alice", "/agent/inbox/task.md", "2026-01-01T00:00:00Z"
        )
        with patch(
            "modules.mailbox.task_service.process_command", side_effect=KeyboardInterrupt()
        ) as run:
            with self.assertRaises(KeyboardInterrupt):
                self.service.submit_queued()
        self.assertEqual(run.call_count, 1)

        self.service.recover_after_restart()
        with patch("modules.mailbox.task_service.process_command") as rerun:
            self.service.submit_queued()
        rerun.assert_not_called()
        self.assertEqual(self.service.get(approval.id).status, "needs_review")
        self.assertEqual(len(self.fake.comments), 1)
        self.assertIn("outcome is unknown", self.fake.comments[0][1])

    def test_crash_after_remote_acceptance_before_local_ack_is_idempotent(self):
        self.decision = TaskDecision(action=ACTION_COMMAND, command="git branch x")
        original = self.service._comment_machine_result
        message_id = _insert_mailbox_msg(self.service)
        approval = self.service.create_task(
            message_id, "git branch x", "alice", "/agent/inbox/task.md", "2026-01-01T00:00:00Z"
        )

        def accepted_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt("process died before local acknowledgement")

        with patch.object(self.service, "_comment_machine_result", side_effect=accepted_then_crash):
            with self.assertRaises(KeyboardInterrupt):
                with patch(
                    "modules.mailbox.task_service.process_command",
                    return_value=("**Exit code:** 0\n", None),
                ):
                    self.service.submit_queued()
        self.assertEqual(len(self.fake.comments), 1)

        with patch("modules.mailbox.task_service.process_command") as rerun:
            self.service.recover_after_restart()
            self.service.submit_queued()
        rerun.assert_not_called()
        self.assertEqual(self.service.get(approval.id).status, "succeeded")
        self.assertEqual(len(self.fake.comments), 1)

    def test_completion_crash_does_not_duplicate_posted_comment(self):
        self.decision = TaskDecision(action=ACTION_COMMAND, command="git branch x")
        original = self.service._complete_claimed_without_experiment
        with patch.object(
            self.service, "_complete_claimed_without_experiment", side_effect=RuntimeError("crash")
        ):
            with self.assertRaises(RuntimeError):
                self._run("git branch x")
        self.assertEqual(len(self.fake.comments), 1)

        self.service._complete_claimed_without_experiment = original
        with patch("modules.mailbox.task_service.process_command") as rerun:
            self.service.submit_queued()
            self.service.submit_queued()
        rerun.assert_not_called()
        self.assertEqual(len(self.fake.comments), 1)

    def test_read_only_command_gets_no_issue(self):
        self.decision = TaskDecision(action=ACTION_COMMAND, command="git status")

        result = self._run("git status")

        self.assertEqual(self.command_runs, 1)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self._created_issues(), [])
        self.assertEqual(self.fake.comments, [])
        self.assertEqual(self.planner.calls, [])

    def test_question_gets_no_issue(self):
        self.decision = TaskDecision(action=ACTION_ANSWER, message="All green.")

        result = self._run("What is the status of the mailbox worker?")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self._created_issues(), [])
        self.assertEqual(self.planner.calls, [])
        self.assertEqual(self.fake.comments, [])


class EnsureForSourcePlannerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fake = FakeGitHub()
        self.service = GitHubWorkTrackingService(
            db_path=str(Path(self._tmp.name) / "nexus.db"),
            repo_root=self._tmp.name,
            repository="tire0011/agent-lab",
            transport=self.fake,
        )

    def tearDown(self):
        self.service.close()
        self._tmp.cleanup()

    def _source(self, content):
        return SimpleNamespace(
            source_type="task", source_id="abc12345", title="Mailbox task abc12345", content=content
        )

    def test_planner_error_keeps_the_placeholder(self):
        def broken(_text):
            raise RuntimeError("provider down")

        ref = self.service.ensure_for_source(self._source("Add a retry."), planner=broken)

        self.assertEqual(ref.issue_title, "Mailbox task abc12345")

    def test_issue_named_by_the_task_is_reused_without_planning(self):
        planner = FakePlanner({"title": "x", "body": PLANNED_BODY})

        ref = self.service.ensure_for_source(self._source("solve github issue #42"), planner=planner)

        self.assertEqual(ref.issue_number, 42)
        self.assertEqual(planner.calls, [])


if __name__ == "__main__":
    unittest.main()
