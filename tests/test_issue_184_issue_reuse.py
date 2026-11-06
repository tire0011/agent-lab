"""Regression for issue #184: reuse an explicitly supplied GitHub Issue.

The live failure: a mailbox task said "implement GitHub issue #180 ... Do not
create another branch or issue" and Nexus opened tracking issue #181 anyway.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.github_work_tracking import GitHubWorkTrackingError, GitHubWorkTrackingService
from modules.mailbox.task_service import MailboxTaskService
from modules.run_store import init_db
from modules.work_directives import parse_work_directives
from tests.test_github_work_tracking import FakeGitHub
from tests.test_mailbox_task_service import (
    _FixedOrchestrator,
    _insert_mailbox_msg,
    _make_exp,
)

_ISSUE_180_TASK = (
    "Implement GitHub issue #180 (outbox message object).\n"
    "Use the already-created branch fix/180-outbox-message-object. "
    "Do not create another branch or issue."
)


class WorkDirectiveParsingTests(unittest.TestCase):
    def test_parses_the_live_issue_180_task(self):
        directives = parse_work_directives(_ISSUE_180_TASK)

        self.assertEqual(directives.issue_number, 180)
        self.assertTrue(directives.forbid_new_issue)
        self.assertTrue(directives.forbid_new_branch)
        self.assertEqual(directives.branch_name, "fix/180-outbox-message-object")
        self.assertTrue(directives.forbid_new_issue or directives.issue_number is not None)

    def test_plain_task_carries_no_directives(self):
        directives = parse_work_directives("Add a retry to the mailbox poller.")

        self.assertIsNone(directives.issue_number)
        self.assertFalse(directives.forbid_new_issue)
        self.assertFalse(directives.forbid_new_branch)
        self.assertIsNone(directives.branch_name)
        self.assertFalse(directives.forbid_new_issue or directives.issue_number is not None)

    def test_background_mention_is_not_a_work_target(self):
        directives = parse_work_directives(
            "See issue #12 for background, then add structured logging."
        )

        self.assertIsNone(directives.issue_number)
        self.assertEqual(directives.mentioned_issue_numbers, (12,))

    def test_unrelated_issue_wording_is_not_a_creation_ban(self):
        directives = parse_work_directives(
            "Do not create a PR that closes the issue automatically."
        )

        self.assertFalse(directives.forbid_new_issue)

    def test_ban_without_a_target_still_requires_reuse(self):
        directives = parse_work_directives("Do not create another issue for this.")

        self.assertIsNone(directives.issue_number)
        self.assertTrue(directives.forbid_new_issue)
        self.assertTrue(directives.forbid_new_issue or directives.issue_number is not None)

    def test_reuse_phrasings_and_issue_urls_are_recognised(self):
        self.assertEqual(parse_work_directives("Reuse issue #180.").issue_number, 180)
        self.assertEqual(parse_work_directives("Work on issue 180 now.").issue_number, 180)
        self.assertEqual(
            parse_work_directives(
                "Fix https://github.com/tire0011/agent-lab/issues/180 today."
            ).issue_number,
            180,
        )


class IssueReuseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeGitHub()
        self.fake.issues[180] = {
            "number": 180,
            "title": "Outbox message object",
            "state": "open",
        }
        self.fake.refs["fix/180-outbox-message-object"] = "branch-sha"
        self.service = GitHubWorkTrackingService(
            db_path=str(Path(self.tmp.name) / "nexus.db"),
            repo_root=self.tmp.name,
            repository="tire0011/agent-lab",
            transport=self.fake,
        )

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def _source(self, content: str, source_id: str = "a9ef876f"):
        return SimpleNamespace(
            source_type="task",
            source_id=source_id,
            title=f"Mailbox task {source_id[:8]}",
            content=content,
        )

    def _created_branches(self):
        return [
            (payload or {}).get("ref")
            for method, path, payload in self.fake.calls
            if method == "POST" and path == "/repos/tire0011/agent-lab/git/refs"
        ]

    def _created_issue_titles(self):
        return [
            payload.get("title")
            for method, path, payload in self.fake.calls
            if method == "POST" and path == "/repos/tire0011/agent-lab/issues"
        ]

    def test_supplied_issue_is_reused_instead_of_creating_a_duplicate(self):
        ref = self.service.ensure_for_source(self._source(_ISSUE_180_TASK))

        self.assertEqual(ref.issue_number, 180)
        self.assertEqual(ref.issue_title, "Outbox message object")
        self.assertEqual(self._created_issue_titles(), [])

    def test_supplied_branch_is_reused_instead_of_a_second_branch(self):
        ref = self.service.ensure_for_source(self._source(_ISSUE_180_TASK))

        self.assertEqual(ref.branch_name, "fix/180-outbox-message-object")
        self.assertNotIn("nexus/180-outbox-message-object", self.fake.refs)
        self.assertEqual(self._created_branches(), [])

    def test_missing_supplied_branch_stops_instead_of_creating_it(self):
        del self.fake.refs["fix/180-outbox-message-object"]

        with self.assertRaises(GitHubWorkTrackingError) as ctx:
            self.service.ensure_for_source(self._source(_ISSUE_180_TASK))

        self.assertIn("fix/180-outbox-message-object", str(ctx.exception))
        self.assertIn("does not exist", str(ctx.exception))
        self.assertEqual(self._created_branches(), [])

    def test_refused_branch_leaves_no_link_to_reanimate_it(self):
        # The persisted link is the second way in: an earlier run's row must
        # not let a later one re-create the branch behind the constraint.
        del self.fake.refs["fix/180-outbox-message-object"]
        with self.assertRaises(GitHubWorkTrackingError):
            self.service.ensure_for_source(self._source(_ISSUE_180_TASK))

        with self.assertRaises(GitHubWorkTrackingError):
            self.service.ensure_for_source(self._source(_ISSUE_180_TASK))

        self.assertEqual(self._created_branches(), [])

    def test_deleted_branch_is_not_recreated_for_a_persisted_link(self):
        self.service.ensure_for_source(self._source(_ISSUE_180_TASK))
        del self.fake.refs["fix/180-outbox-message-object"]

        with self.assertRaises(GitHubWorkTrackingError) as ctx:
            self.service.ensure_for_source(self._source(_ISSUE_180_TASK))

        self.assertIn("fix/180-outbox-message-object", str(ctx.exception))
        self.assertEqual(self._created_branches(), [])

    def test_branch_ban_reuses_the_existing_derived_branch(self):
        self.fake.refs["nexus/180-outbox-message-object"] = "derived-sha"

        ref = self.service.ensure_for_source(
            self._source("Implement issue #180. Do not create another branch.")
        )

        self.assertEqual(ref.branch_name, "nexus/180-outbox-message-object")
        self.assertEqual(self._created_branches(), [])

    def test_branch_ban_without_a_reusable_branch_stops_and_reports(self):
        with self.assertRaises(GitHubWorkTrackingError) as ctx:
            self.service.ensure_for_source(
                self._source("Implement issue #180. Do not create another branch.")
            )

        self.assertIn("forbids creating a new branch", str(ctx.exception))
        self.assertEqual(self._created_branches(), [])

    def test_reused_identity_survives_a_second_lookup(self):
        first = self.service.ensure_for_source(self._source(_ISSUE_180_TASK))
        second = self.service.ensure_for_source(self._source(_ISSUE_180_TASK))

        self.assertEqual(first.issue_number, second.issue_number)
        self.assertEqual(first.branch_name, second.branch_name)
        self.assertEqual(self._created_issue_titles(), [])

    def test_closed_supplied_issue_is_reopened_not_replaced(self):
        self.fake.issues[180]["state"] = "closed"

        ref = self.service.ensure_for_source(self._source(_ISSUE_180_TASK))

        self.assertEqual(ref.issue_number, 180)
        self.assertEqual(self.fake.issues[180]["state"], "open")
        self.assertEqual(self._created_issue_titles(), [])

    def test_pull_request_number_stops_the_task_instead_of_creating_a_replacement(self):
        self.fake.issues[181] = {
            "number": 181,
            "title": "Some PR",
            "state": "open",
            "pull_request": {"url": "https://example.test/pulls/181"},
        }

        with self.assertRaises(GitHubWorkTrackingError) as ctx:
            self.service.ensure_for_source(
                self._source("Implement issue #181. Do not create another issue.")
            )

        self.assertIn("#181", str(ctx.exception))
        self.assertIn("pull request", str(ctx.exception))
        self.assertEqual(self._created_issue_titles(), [])

    def test_ban_without_a_reusable_issue_stops_and_reports(self):
        with self.assertRaises(GitHubWorkTrackingError) as ctx:
            self.service.ensure_for_source(
                self._source("Refactor the poller. Do not create another issue.")
            )

        self.assertIn("forbids creating a new tracking issue", str(ctx.exception))
        self.assertEqual(self._created_issue_titles(), [])

    def test_ambiguous_candidates_stop_and_name_them(self):
        with self.assertRaises(GitHubWorkTrackingError) as ctx:
            self.service.ensure_for_source(
                self._source(
                    "Related to issue #180 and issue #190. Do not create another issue."
                )
            )

        message = str(ctx.exception)
        self.assertIn("#180", message)
        self.assertIn("#190", message)
        self.assertEqual(self._created_issue_titles(), [])

    def test_task_without_directives_still_creates_a_tracking_issue(self):
        ref = self.service.ensure_for_source(
            self._source("Add a retry to the mailbox poller.", source_id="plain001")
        )

        self.assertEqual(self._created_issue_titles(), ["Mailbox task plain001"])
        self.assertTrue(ref.branch_name.startswith("nexus/"))
        self.assertEqual(self._created_branches(), [f"refs/heads/{ref.branch_name}"])


class MailboxTaskIssueReuseTests(unittest.TestCase):
    """The #180/#181 scenario end to end, through the mailbox task service."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = FakeGitHub()
        self.fake.issues[180] = {
            "number": 180,
            "title": "Outbox message object",
            "state": "open",
        }
        self.fake.refs["fix/180-outbox-message-object"] = "branch-sha"
        self.db_path = str(self.tmp / "test.db")
        conn = init_db(self.db_path)
        conn.close()
        self.tracker = GitHubWorkTrackingService(
            db_path=self.db_path,
            repo_root=str(self.tmp),
            repository="tire0011/agent-lab",
            transport=self.fake,
        )
        self.experiment = _make_exp(id="exp184", status="queued")
        self.experiment_service = MagicMock()
        self.experiment_service.create_experiment.return_value = self.experiment
        self.experiment_service.get_experiment.return_value = self.experiment
        self.experiment_service.run_experiment.return_value = self.experiment
        self.experiment_service.experiment_log_dir = str(self.tmp / "experiment-logs")
        registry = MagicMock()
        registry.catalog.return_value = []
        self.service = MailboxTaskService(
            db_path=self.db_path,
            experiment_service=self.experiment_service,
            outbox_dir=str(self.tmp / "outbox"),
            repo_path=str(self.tmp),
            operations_registry=registry,
            orchestrator_factory=_FixedOrchestrator,
            github_work_tracking=self.tracker,
            task_log_dir=str(self.tmp / "experiment-logs"),
        )

    def tearDown(self):
        self.tracker.close()

    def _run_task(self, body: str) -> str:
        message_id = _insert_mailbox_msg(self.service)
        approval = self.service.create_task(
            message_id, body, "alice", "/agent/inbox/task.md", "2026-01-01T00:00:00Z"
        )
        self.service.submit_queued()
        time.sleep(0.1)
        return approval.id

    def _task_log(self, approval_id: str) -> list[dict]:
        path = Path(self.service._task_logger(approval_id).path)
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_mailbox_task_reuses_issue_180_and_creates_no_duplicate(self):
        approval_id = self._run_task(_ISSUE_180_TASK)

        created = [
            payload
            for method, path, payload in self.fake.calls
            if method == "POST" and path == "/repos/tire0011/agent-lab/issues"
        ]
        self.assertEqual(created, [], "Nexus opened a duplicate tracking issue")
        linked = [
            entry
            for entry in self._task_log(approval_id)
            if entry["event"] == "github_work_tracking_linked"
        ]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["issue_number"], 180)
        self.assertEqual(linked[0]["branch_name"], "fix/180-outbox-message-object")

    def test_task_result_reports_the_reused_issue_number(self):
        approval_id = self._run_task(_ISSUE_180_TASK)
        self.experiment.status = "succeeded"
        self.experiment.test_exit_code = 0

        self.service.poll_running()

        result = self.service.get(approval_id)
        self.assertEqual(result.status, "succeeded")
        self.assertIn("issue=#180", result.result_summary)
        self.assertIn("branch=fix/180-outbox-message-object", result.result_summary)

    def test_missing_reused_branch_stops_the_task_for_review(self):
        del self.fake.refs["fix/180-outbox-message-object"]

        approval_id = self._run_task(_ISSUE_180_TASK)

        result = self.service.get(approval_id)
        self.assertEqual(result.status, "needs_review")
        self.assertIn("fix/180-outbox-message-object", result.result_summary)
        self.experiment_service.create_experiment.assert_not_called()

    def test_task_that_forbids_a_new_issue_without_one_stops_for_review(self):
        approval_id = self._run_task(
            "Refactor the mailbox poller. Do not create another issue."
        )

        result = self.service.get(approval_id)
        self.assertEqual(result.status, "needs_review")
        self.assertIn("forbids creating a new tracking issue", result.result_summary)
        self.experiment_service.create_experiment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
