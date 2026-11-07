"""#263 step 4: ``new_experiment`` is a tool Nexus executes itself.

The solve-issue-238 run on Nexus gathered the root cause and then changed
code in the live checkout; with the experiment as an end point, whoever called
Nexus started it, with the bare task text, and Nexus never saw the result.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.experiment_handover import HANDOVER_HEADING
from modules.experiment_wait import resume_after_experiment
from modules.github_work_tracking import GitHubWorkTrackingError
from modules.nexus_experiment_tool import (
    LAUNCH_BLOCKED,
    LAUNCH_EVENT_KIND,
    LAUNCH_LIMIT_REACHED,
    LAUNCH_NOT_LINKED,
    LAUNCH_SUBMITTED,
    NexusExperimentTool,
)
from modules.nexus_task_store import RESTING_STATUSES, NexusTaskStore
from modules.task_orchestrator import (
    ACTION_CLARIFICATION,
    ACTION_NEW_EXPERIMENT,
    NexusTaskOrchestrator,
    TaskDecision,
)
from tests.test_issue_111_controller_protocol import _Client, _Delegation, _Registry

_ROOT = Path(__file__).resolve().parent.parent


def _github(pr_number=None):
    github = MagicMock()
    github.ensure_for_source.return_value = SimpleNamespace(
        issue_number=238, branch_name="nexus/238-memory-filter"
    )
    github.bind_experiment.side_effect = lambda ref, experiment_id: ref
    github.for_experiment.return_value = SimpleNamespace(pr_number=pr_number)
    return github


class NexusRunsItsOwnExperimentTests(unittest.TestCase):
    """The whole loop with the real controller: handover, pause, result, resume."""

    def test_solve_issue_reaches_the_experiment_with_handover_and_resumes_with_the_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _Client([
                '{"action":"command","command":"echo root-cause","reason":"look"}',
                '{"action":"new_experiment","arguments":{"goal":"add a kind filter"},'
                '"reason":"implement"}',
                '{"action":"clarification","message":"The experiment failed: tests red '
                'in test_x. Retry with a narrower change?","reason":"report the result"}',
            ])
            experiments = MagicMock()
            experiments.create_experiment.return_value = SimpleNamespace(id="exp-238")
            experiments.get_experiment.return_value = SimpleNamespace(
                id="exp-238", status="failed", error=None, test_exit_code=1,
                test_output="FAILED tests/test_x.py::test_y",
            )
            github = _github(pr_number=77)
            pending: list = []
            orchestrator = NexusTaskOrchestrator(
                operations_registry=_Registry(str(Path(tmp) / "state.db"), tmp),
                experiment_service=experiments,
                llama_client=client,
                delegation_service=_Delegation(),
            )
            orchestrator.experiment_tool = NexusExperimentTool(
                experiments,
                github,
                background=lambda _name, target: pending.append(target),
                on_finished=lambda experiment_id: resumed.extend(
                    resume_after_experiment(orchestrator, experiments, github, experiment_id)
                ),
            )
            resumed: list = []
            try:
                first = orchestrator.run(
                    "solve github issue #238", source_type="task", source_id="issue-238-e2e"
                )
                parked = orchestrator.task_store.get(first.session_id)
                prompts_while_parked = len(client.prompts)
                launches = orchestrator.task_store.recent_events(
                    first.session_id, role="tool", kind=LAUNCH_EVENT_KIND
                )

                # The experiment ends; its thread continues the same session.
                self.assertEqual(len(pending), 1)
                pending.pop()()
                after = orchestrator.task_store.get(first.session_id)
            finally:
                orchestrator.close()

        # Nexus executed its own tool; the caller did nothing.
        self.assertEqual(first.action, ACTION_NEW_EXPERIMENT)
        self.assertEqual(first.experiment_launch.status, LAUNCH_SUBMITTED)
        self.assertEqual(first.experiment_id, "exp-238")
        experiments.create_experiment.assert_called_once()
        goal = experiments.create_experiment.call_args.args[0]
        kwargs = experiments.create_experiment.call_args.kwargs
        self.assertEqual((kwargs["source_type"], kwargs["source_id"]), ("task", "issue-238-e2e"))

        # Handover: the task, then what Nexus found - also in the launch record.
        self.assertTrue(goal.startswith("solve github issue #238\n\n" + HANDOVER_HEADING))
        self.assertIn("Nexus goal:\nadd a kind filter", goal)
        self.assertIn("- command echo root-cause", goal)
        self.assertEqual(len(launches), 1)
        self.assertEqual(json.loads(launches[0]["content"])["goal"], goal)
        github.ensure_for_source.assert_called_once()
        github.bind_experiment.assert_called_once()

        # Pause: no claim, no heartbeat, no model call while the experiment runs.
        self.assertEqual(parked.status, "waiting_for_experiment")
        self.assertEqual(parked.waiting_experiment_id, "exp-238")
        self.assertIn(parked.status, RESTING_STATUSES)
        self.assertIsNone(parked.last_heartbeat_at)
        self.assertEqual(prompts_while_parked, 2)
        experiments.run_experiment.assert_called_once_with("exp-238", github_work_tracking=github)

        # Resume: the same session continues with status, PR and reason.
        self.assertEqual([d.action for d in resumed], [ACTION_CLARIFICATION])
        self.assertEqual(resumed[0].session_id, first.session_id)
        observations = client.prompts[2].partition("OBSERVATIONS FROM TOOLS")[2]
        self.assertIn('"status":"failed"', observations)
        self.assertIn('"pr_number":77', observations)
        self.assertIn("tests failed (exit 1)", observations)
        self.assertIn("test_x.py::test_y", observations)
        self.assertNotEqual(after.status, "waiting_for_experiment")

    def test_without_a_tool_the_decision_is_returned_unexecuted(self):
        with tempfile.TemporaryDirectory() as tmp:
            experiments = MagicMock()
            orchestrator = NexusTaskOrchestrator(
                operations_registry=_Registry(str(Path(tmp) / "state.db"), tmp),
                experiment_service=experiments,
                llama_client=_Client([
                    '{"action":"new_experiment","arguments":{"goal":"x"},"reason":"implement"}',
                ]),
                delegation_service=_Delegation(),
            )
            try:
                decision = orchestrator.run("change it", source_type="task", source_id="dry")
            finally:
                orchestrator.close()
        self.assertEqual(decision.action, ACTION_NEW_EXPERIMENT)
        self.assertIsNone(decision.experiment_launch)
        experiments.create_experiment.assert_not_called()


class ExperimentToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = NexusTaskStore(db_path=str(Path(self.tmp.name) / "n.db"))
        self.addCleanup(self.store.close)
        self.session = self.store.get_or_create(
            source_type="issue", source_id="238", title="GitHub Issue #238"
        )
        self.experiments = MagicMock()
        self.experiments.create_experiment.return_value = SimpleNamespace(id="exp-1")
        self.github = _github()
        self.background = MagicMock()

    def _tool(self, **kwargs):
        return NexusExperimentTool(
            self.experiments, self.github, background=self.background, **kwargs
        )

    def _decision(self, handover=None):
        return TaskDecision(
            action=ACTION_NEW_EXPERIMENT,
            session_id=self.session.id,
            experiment_handover=handover,
            complexity="small",
        )

    def test_goal_is_the_full_task_followed_by_briefing_findings_and_attempts(self):
        issue_body = "b" * 4000 + "\nISSUE-END-MARKER"
        self.store.append_event(self.session.id, role="user", kind="input", content=issue_body)
        handover = (
            f"{HANDOVER_HEADING}\n\nKnowledge briefing:\n_search_task_events has no kind filter"
            "\n\nDelegate findings (unverified):\nAdd a kind filter."
            "\n\nEarlier attempts in this run:\n- command grep -n kind: ok"
        )

        launch = self._tool().start(self.store, self.session, self._decision(handover), "fallback")

        self.assertEqual(launch.status, LAUNCH_SUBMITTED)
        goal = self.experiments.create_experiment.call_args.args[0]
        self.assertEqual(goal.split(HANDOVER_HEADING, 1)[0].strip(), issue_body)
        for part in ("Knowledge briefing:", "Delegate findings", "Earlier attempts in this run:"):
            self.assertIn(part, goal)
        self.assertEqual(self.experiments.create_experiment.call_args.kwargs["complexity"], "small")
        self.assertEqual(self.store.get(self.session.id).waiting_experiment_id, "exp-1")
        self.background.assert_called_once()

    def test_github_lifecycle_failure_blocks_before_creation(self):
        self.github.ensure_for_source.side_effect = GitHubWorkTrackingError("Configure GITHUB_TOKEN")
        launch = self._tool().start(self.store, self.session, self._decision(), "Fix it")
        self.assertEqual(launch.status, LAUNCH_BLOCKED)
        self.assertIn("GITHUB_TOKEN", launch.reason)
        self.experiments.create_experiment.assert_not_called()
        self.assertNotEqual(self.store.get(self.session.id).status, "waiting_for_experiment")

    def test_a_caller_that_does_not_link_the_experiment_keeps_it_from_running(self):
        launch = self._tool(on_created=lambda *_: False).start(
            self.store, self.session, self._decision(), "Fix it"
        )
        self.assertEqual(launch.status, LAUNCH_NOT_LINKED)
        self.background.assert_not_called()
        self.assertIsNone(self.store.get(self.session.id).waiting_experiment_id)

    def test_the_per_task_limit_counts_ended_experiments(self):
        for n in range(2):
            self.store.append_event(
                self.session.id, role="tool", kind=ACTION_NEW_EXPERIMENT,
                content=json.dumps({"experiment_id": f"exp{n}", "status": "failed"}),
            )
        launch = self._tool(max_experiments=2).start(
            self.store, self.session, self._decision(), "Fix it"
        )
        self.assertEqual(launch.status, LAUNCH_LIMIT_REACHED)
        self.assertIn("limit of 2 experiments per task", launch.reason)
        self.experiments.create_experiment.assert_not_called()

    def test_the_run_is_followed_by_the_continuation_even_when_it_raises(self):
        finished = []
        self.experiments.run_experiment.side_effect = RuntimeError("pipeline crashed")
        self._tool(on_finished=finished.append).start(
            self.store, self.session, self._decision(), "Fix it"
        )
        target = self.background.call_args.args[1]
        with self.assertRaises(RuntimeError):
            target()
        self.assertEqual(finished, ["exp-1"])


class CallSitesDoNotRunExperimentsTests(unittest.TestCase):
    """Acceptance: the three call sites no longer create experiments themselves."""

    def test_no_call_site_creates_or_parks_an_experiment(self):
        for module in (
            "modules/mailbox/task_service.py",
            "modules/nexus_controller_service.py",
            "modules/nexus_chat_service.py",
        ):
            source = (_ROOT / module).read_text(encoding="utf-8")
            with self.subTest(module=module):
                self.assertNotIn("create_experiment(", source)
                self.assertNotIn("wait_for_experiment(", source)
                self.assertNotIn("with_handover(", source)
                self.assertNotIn("_start_new_experiment", source)
                self.assertIn("NexusExperimentTool(", source)


if __name__ == "__main__":
    unittest.main()
