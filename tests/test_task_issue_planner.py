"""Free-form tasks get an issue written by an external planner (#294)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.github_work_tracking import GitHubWorkTrackingService
from modules.nexus_controller_service import NexusControllerService
from modules.nexus_sources import issue_source, task_source
from modules.task_issue_planner import (
    ENV_TASK_PLANNER,
    WORK_MACHINE,
    IssueDraft,
    LazyIssueDraft,
    TaskIssuePlanner,
    command_changes_something,
    open_machine_issue,
    parse_issue_draft,
    planned_goal,
    planner_prompt,
    report_machine_result,
)
from modules.task_orchestrator import ACTION_COMMAND, ACTION_NEW_EXPERIMENT, TaskDecision
from tests.test_github_work_tracking import FakeGitHub


def _answer(**overrides) -> str:
    data = {
        "title": "Update Ubuntu packages on the VM",
        "goal": "All security updates are installed.",
        "current_state": "12 packages are upgradable.",
        "plan": ["List upgradable packages", "Run apt upgrade", "Check services"],
        "acceptance_criteria": ["apt list --upgradable is empty", "API health is ok"],
    }
    data.update(overrides)
    return "Here is the issue:\n```json\n" + json.dumps(data) + "\n```"


class ParseIssueDraftTests(unittest.TestCase):
    def test_answer_becomes_structured_issue_with_original_task(self):
        draft = parse_issue_draft(_answer(), "update ubuntu", planner="claude")

        self.assertEqual(draft.title, "Update Ubuntu packages on the VM")
        self.assertEqual(draft.planner, "claude")
        for heading in ("## Goal", "## Current state", "## Plan", "## Acceptance criteria", "## Original task"):
            self.assertIn(heading, draft.body)
        self.assertIn("1. List upgradable packages", draft.body)
        self.assertIn("- [ ] API health is ok", draft.body)
        self.assertTrue(draft.body.split("## Original task", 1)[1].strip().startswith("update ubuntu"))

    def test_incomplete_or_garbled_answers_are_rejected(self):
        self.assertIsNone(parse_issue_draft("no json here", "task"))
        self.assertIsNone(parse_issue_draft(_answer(plan=[]), "task"))
        self.assertIsNone(parse_issue_draft(_answer(acceptance_criteria=[]), "task"))
        self.assertIsNone(parse_issue_draft(_answer(title=""), "task"))

    def test_secrets_never_reach_the_issue(self):
        draft = parse_issue_draft(_answer(), "deploy with token=ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123456789", draft.body)

    def test_prompt_names_work_kind_and_facts(self):
        prompt = planner_prompt("update ubuntu", "Knowledge briefing: 12 upgradable",
                                work=WORK_MACHINE, command="sudo apt-get upgrade -y")
        self.assertIn("MACHINE WORK", prompt)
        self.assertIn("sudo apt-get upgrade -y", prompt)
        self.assertIn("12 upgradable", prompt)
        self.assertIn('"acceptance_criteria"', prompt)

    def test_planned_goal_starts_with_the_issue_header(self):
        goal = planned_goal(77, IssueDraft("Do it", "## Goal\n\nDone."))
        self.assertTrue(goal.startswith("GitHub Issue #77: Do it\n\n## Goal"))


class TaskIssuePlannerTests(unittest.TestCase):
    def test_next_provider_is_used_when_the_first_fails(self):
        calls = []

        def broken(prompt):
            calls.append("claude")
            raise RuntimeError("cli missing")

        def codex(prompt):
            calls.append("codex")
            return _answer()

        draft = TaskIssuePlanner({"claude": broken, "codex": codex}).draft("update ubuntu", "facts")

        self.assertEqual(calls, ["claude", "codex"])
        self.assertEqual(draft.planner, "codex")

    def test_stops_after_the_first_usable_issue(self):
        second = MagicMock()
        draft = TaskIssuePlanner({"claude": lambda p: _answer(), "codex": second}).draft("task")
        self.assertEqual(draft.planner, "claude")
        second.assert_not_called()

    def test_no_external_provider_means_placeholder(self):
        self.assertIsNone(TaskIssuePlanner({}).draft("update ubuntu"))
        self.assertIsNone(TaskIssuePlanner({"claude": lambda p: "nonsense"}).draft("update ubuntu"))

    def test_can_be_switched_off(self):
        runner = MagicMock(return_value=_answer())
        with patch.dict(os.environ, {ENV_TASK_PLANNER: "off"}):
            self.assertIsNone(TaskIssuePlanner({"claude": runner}).draft("update ubuntu"))
        runner.assert_not_called()

    def test_default_runners_are_only_external_providers(self):
        with patch("modules.coding_provider.available_providers", return_value=[]):
            self.assertIsNone(TaskIssuePlanner().draft("update ubuntu"))

    def test_lazy_draft_plans_once_and_swallows_errors(self):
        plan = MagicMock(side_effect=RuntimeError("boom"))
        lazy = LazyIssueDraft(plan)
        self.assertIsNone(lazy())
        self.assertIsNone(lazy())
        plan.assert_called_once()


class CommandClassificationTests(unittest.TestCase):
    def test_lookups_are_read_only(self):
        for command in (
            "free -m", "df -h /", "uptime && free -h", "systemctl status nexus-api",
            "journalctl -u nexus-api -n 50 2>&1 | tail -20", "git status", "apt list --upgradable",
            "cat /proc/meminfo | grep MemAvailable", "ls -la > /dev/null",
        ):
            self.assertFalse(command_changes_something(command), command)

    def test_changes_are_detected(self):
        for command in (
            "sudo apt-get upgrade -y", "apt upgrade", "systemctl restart nexus-api",
            "echo x > /etc/motd", "sed -i s/a/b/ file", "rm -rf /tmp/x", "find /tmp -delete",
            "git push origin main", "gh issue create --title x", "free -m; reboot",
            "pip install requests", "cat $(rm x)",
        ):
            self.assertTrue(command_changes_something(command), command)


class WorkTrackingPlannedIssueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeGitHub()
        self.service = GitHubWorkTrackingService(
            db_path=str(Path(self.tmp.name) / "nexus.db"),
            repo_root=self.tmp.name,
            repository="tire0011/agent-lab",
            transport=self.fake,
        )

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def _created_issue_payloads(self):
        return [p for m, path, p in self.fake.calls if m == "POST" and path == "/repos/tire0011/agent-lab/issues"]

    def test_planned_issue_replaces_the_placeholder(self):
        plan = MagicMock(return_value=IssueDraft("Update Ubuntu packages", "## Goal\n\nUpdated."))
        source = SimpleNamespace(
            source_type="task", source_id="task-1", title="Mailbox task task-1",
            content="update ubuntu", issue_draft=LazyIssueDraft(plan),
        )

        ref = self.service.ensure_for_source(source)
        again = self.service.ensure_for_source(source)

        created = self._created_issue_payloads()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["title"], "Update Ubuntu packages")
        self.assertIn("## Goal", created[0]["body"])
        self.assertEqual(ref.issue_title, "Update Ubuntu packages")
        self.assertTrue(ref.branch_name.startswith(f"nexus/{ref.issue_number}-update-ubuntu-packages"))
        self.assertEqual(again.issue_number, ref.issue_number)
        plan.assert_called_once()

    def test_no_draft_falls_back_to_placeholder(self):
        source = SimpleNamespace(
            source_type="task", source_id="task-2", title="Mailbox task task-2",
            content="update ubuntu", issue_draft=LazyIssueDraft(lambda: None),
        )
        self.service.ensure_for_source(source)
        created = self._created_issue_payloads()
        self.assertEqual(created[0]["title"], "Mailbox task task-2")
        self.assertIn("## Nexus task\n\nupdate ubuntu", created[0]["body"])

    def test_named_issue_never_calls_the_planner(self):
        plan = MagicMock()
        source = SimpleNamespace(
            source_type="task", source_id="task-3", title="Mailbox task task-3",
            content="solve github issue #42", issue_draft=plan,
        )
        ref = self.service.ensure_for_source(source)
        self.assertEqual(ref.issue_number, 42)
        plan.assert_not_called()
        self.assertEqual(self._created_issue_payloads(), [])

    def test_draft_in_source_metadata_is_used(self):
        source = task_source("update ubuntu", source_id="task-4", title="Nexus controller task")
        source.metadata["issue_draft"] = lambda: IssueDraft("Planned title", "body")
        ref = self.service.ensure_for_source(source)
        self.assertEqual(ref.issue_title, "Planned title")


class ControllerPlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orchestrator = MagicMock()
        self.orchestrator.experiment_service = MagicMock()
        self.orchestrator.experiment_service.db_path = str(Path(self.tmp.name) / "agent.db")
        self.orchestrator.experiment_service.create_experiment.return_value = SimpleNamespace(id="exp-294")
        self.orchestrator.operations_registry = SimpleNamespace(repo_root=Path(self.tmp.name))
        self.orchestrator.task_store = MagicMock()
        self.github = MagicMock()
        self.github.configured = True

        def ensure(source):
            plan = (source.metadata or {}).get("issue_draft")
            draft = plan() if plan else None
            return SimpleNamespace(issue_number=77, issue_title=draft.title if draft else "x",
                                   branch_name="nexus/77-x")

        self.github.ensure_for_source.side_effect = ensure
        self.github.bind_experiment.side_effect = lambda ref, experiment_id: ref
        self.github.open_planned_issue.return_value = {"number": 88}
        self.planner = MagicMock()
        self.planner.draft.return_value = IssueDraft("Update Ubuntu", "## Goal\n\nUpdated.", planner="claude")
        self.service = NexusControllerService(
            self.orchestrator, github_work_tracking=self.github, issue_planner=self.planner
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_code_task_experiment_works_off_the_planned_issue(self):
        self.orchestrator.run.return_value = TaskDecision(
            action=ACTION_NEW_EXPERIMENT, session_id="s1", experiment_handover="Knowledge briefing: x",
        )
        with patch.object(self.service, "_start_experiment_thread"):
            self.service.handle_source(task_source("update ubuntu", source_id="task-1"))

        goal = self.orchestrator.experiment_service.create_experiment.call_args.args[0]
        self.assertTrue(goal.startswith("GitHub Issue #77: Update Ubuntu\n\n## Goal"))
        self.assertIn("Knowledge briefing: x", goal)
        self.planner.draft.assert_called_once()
        self.assertEqual(self.planner.draft.call_args.args[:2], ("update ubuntu", "Knowledge briefing: x"))

    def test_issue_source_keeps_its_path(self):
        self.orchestrator.run.return_value = TaskDecision(action=ACTION_NEW_EXPERIMENT, session_id="s1")
        with patch.object(self.service, "_start_experiment_thread"):
            self.service.handle_source(issue_source(5, title="Existing", body="body"))
        self.planner.draft.assert_not_called()
        goal = self.orchestrator.experiment_service.create_experiment.call_args.args[0]
        self.assertTrue(goal.startswith("GitHub Issue #5: Existing"))

    @patch("modules.nexus_controller_service.process_command")
    def test_machine_work_opens_issue_and_comments_result(self, process_command):
        process_command.return_value = ("**Exit code:** 0\n\nupgraded", None)
        self.orchestrator.run.return_value = TaskDecision(
            action=ACTION_COMMAND, session_id="s1", command="sudo apt-get upgrade -y",
        )

        self.service.handle_source(task_source("update ubuntu", source_id="task-2"))

        self.assertEqual(self.planner.draft.call_args.kwargs["work"], WORK_MACHINE)
        self.github.open_planned_issue.assert_called_once()
        number, body = self.github.comment.call_args.args
        self.assertEqual(number, 88)
        self.assertIn("succeeded", body)
        self.assertIn("upgraded", body)
        self.github.close_issue.assert_called_once_with(88)

    @patch("modules.nexus_controller_service.process_command")
    def test_read_only_command_gets_no_issue(self, process_command):
        process_command.return_value = ("**Exit code:** 0\n\nMem: 1G", None)
        self.orchestrator.run.return_value = TaskDecision(
            action=ACTION_COMMAND, session_id="s1", command="free -m",
        )
        self.service.handle_source(task_source("how much RAM is free?", source_id="task-3"))
        self.planner.draft.assert_not_called()
        self.github.open_planned_issue.assert_not_called()
        self.github.comment.assert_not_called()


class MachineIssueHelperTests(unittest.TestCase):
    def test_placeholder_issue_when_planner_has_no_answer(self):
        github = MagicMock(configured=True)
        github.open_planned_issue.return_value = {"number": 9}
        planner = MagicMock()
        planner.draft.return_value = None

        number, draft = open_machine_issue(
            github, planner, task="update ubuntu", facts="", command="apt upgrade",
            placeholder_title="Mailbox task abc: machine work",
        )

        self.assertEqual((number, draft), (9, None))
        issued = github.open_planned_issue.call_args.args[0]
        self.assertEqual(issued.title, "Mailbox task abc: machine work")
        self.assertIn("apt upgrade", issued.body)

    def test_unconfigured_github_does_not_block_machine_work(self):
        github = MagicMock(configured=False)
        number, _ = open_machine_issue(
            github, MagicMock(), task="t", facts="", command="apt upgrade", placeholder_title="x",
        )
        self.assertIsNone(number)
        github.open_planned_issue.assert_not_called()

    def test_failed_machine_work_stays_open(self):
        github = MagicMock()
        self.assertTrue(report_machine_result(github, 9, "apt upgrade", "E: lock", succeeded=False))
        self.assertIn("failed", github.comment.call_args.args[1])
        github.close_issue.assert_not_called()


class MailboxPlannerTests(unittest.TestCase):
    def setUp(self):
        from tests.test_mailbox_task_service import _insert_mailbox_msg, _make_service

        self.tmp = Path(tempfile.mkdtemp())
        self.es = MagicMock()
        self.es.create_experiment.return_value = SimpleNamespace(id="exp-mbx")
        self.es.experiment_log_dir = str(self.tmp / "experiment-logs")
        self.github = MagicMock()
        self.github.configured = True
        self.svc = _make_service(self.tmp, exp_service=self.es, github_work_tracking=self.github)

        def ensure(source):
            draft = source.issue_draft() if getattr(source, "issue_draft", None) else None
            return SimpleNamespace(issue_number=55, issue_title=draft.title if draft else "x",
                                   branch_name="nexus/55-x")

        self.github.ensure_for_source.side_effect = ensure
        self.github.open_planned_issue.return_value = {"number": 66}
        self.planner = MagicMock()
        self.planner.draft.return_value = IssueDraft("Update Ubuntu", "## Goal\n\nUpdated.", planner="codex")
        self.svc.issue_planner = self.planner
        msg_id = _insert_mailbox_msg(self.svc)
        self.approval = self.svc.create_task(msg_id, "update ubuntu", "alice", "/f.md", "2026-01-01T00:00:00Z")

    def tearDown(self):
        self.svc.close()

    def test_new_experiment_uses_planned_issue(self):
        decision = TaskDecision(action=ACTION_NEW_EXPERIMENT, experiment_handover="facts here")
        with patch.object(self.svc, "_start_thread"):
            self.svc._start_new_experiment(self.approval, decision)

        goal = self.es.create_experiment.call_args.args[0]
        self.assertTrue(goal.startswith("GitHub Issue #55: Update Ubuntu"))
        self.assertIn("facts here", goal)
        self.assertEqual(self.planner.draft.call_args.args[:2], ("update ubuntu", "facts here"))

    @patch("modules.mailbox.task_service.process_command")
    def test_machine_command_result_is_commented(self, process_command):
        process_command.return_value = ("**Exit code:** 0\n\ndone", None)
        decision = TaskDecision(action=ACTION_COMMAND, command="sudo apt-get upgrade -y")

        self.svc._run_command(self.approval, decision)

        self.github.open_planned_issue.assert_called_once()
        self.assertEqual(self.github.comment.call_args.args[0], 66)
        self.github.close_issue.assert_called_once_with(66)
        self.assertIn("Reported on GitHub issue #66", self.svc.get(self.approval.id).result_summary)


if __name__ == "__main__":
    unittest.main()
