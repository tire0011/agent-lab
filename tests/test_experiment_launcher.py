import unittest
from types import SimpleNamespace

from modules.experiment_launcher import ExperimentLauncher, LaunchResult
from modules.experiment_service import ExperimentAlreadyActiveError
from modules.github_work_tracking import GitHubWorkTrackingError


class FakeExperiments:
    def __init__(self, calls, *, already_active=None):
        self.calls = calls
        self.already_active = already_active
        self.goals = []
        self.create_kwargs = []

    def create_experiment(self, goal, **kwargs):
        self.calls.append("create_experiment")
        self.goals.append(goal)
        self.create_kwargs.append(kwargs)
        if self.already_active:
            raise ExperimentAlreadyActiveError(*self.already_active)
        return SimpleNamespace(id="exp-1")

    def run_experiment(self, experiment_id, *, github_work_tracking=None):
        self.calls.append(("run_experiment", experiment_id, github_work_tracking))


class FakeGitHub:
    def __init__(self, calls, *, error=None):
        self.calls = calls
        self.error = error
        self.sources = []

    def ensure_for_source(self, source):
        self.calls.append("ensure_for_source")
        self.sources.append(source)
        if self.error:
            raise self.error
        return SimpleNamespace(issue_number=7, experiment_id=None)

    def bind_experiment(self, work_ref, experiment_id):
        self.calls.append("bind_experiment")
        return SimpleNamespace(issue_number=work_ref.issue_number, experiment_id=experiment_id)


class FakeTaskStore:
    def __init__(self, calls):
        self.calls = calls

    def wait_for_experiment(self, session_id, experiment_id):
        self.calls.append(("wait_for_experiment", session_id, experiment_id))
        return True


SOURCE = SimpleNamespace(source_type="chat", source_id="chat-1", title="Nexus chat")


class ExperimentLauncherTests(unittest.TestCase):
    def _launcher(self, *, already_active=None, github_error=None):
        calls = []
        experiments = FakeExperiments(calls, already_active=already_active)
        github = FakeGitHub(calls, error=github_error)
        targets = []

        def background_starter(name, target):
            calls.append(("background", name))
            targets.append(target)

        def resume_factory(experiment_id):
            calls.append(("resume", experiment_id))

        launcher = ExperimentLauncher(experiments, github, background_starter, resume_factory)
        return launcher, experiments, github, calls, targets

    def test_steps_run_in_order_and_session_is_parked_before_background_start(self):
        launcher, experiments, github, calls, targets = self._launcher()

        result = launcher.launch(
            "chat-1", "Do X", "HANDOVER", SOURCE, task_store=FakeTaskStore(calls)
        )

        self.assertEqual(result, LaunchResult(
            experiment_id="exp-1", status="submitted", already_active=False,
            work_ref=SimpleNamespace(issue_number=7, experiment_id="exp-1"),
        ))
        self.assertEqual(calls, [
            "ensure_for_source",
            "create_experiment",
            "bind_experiment",
            ("wait_for_experiment", "chat-1", "exp-1"),
            ("background", "exp-1"),
        ])
        # The background target runs the experiment, then resumes the session.
        targets[0]()
        self.assertEqual(calls[-2:], [
            ("run_experiment", "exp-1", github),
            ("resume", "exp-1"),
        ])

    def test_resume_runs_even_when_the_experiment_raises(self):
        launcher, experiments, _, calls, targets = self._launcher()

        def failing_run(experiment_id, *, github_work_tracking=None):
            raise RuntimeError("boom")

        experiments.run_experiment = failing_run
        launcher.launch("chat-1", "Do X", None, SOURCE, task_store=FakeTaskStore(calls))

        with self.assertRaises(RuntimeError):
            targets[0]()
        self.assertEqual(calls[-1], ("resume", "exp-1"))

    def test_handover_is_in_created_goal_and_tracked_content(self):
        launcher, experiments, github, _, _ = self._launcher()

        launcher.launch("chat-1", "Do X", "What Nexus found", SOURCE)

        self.assertEqual(experiments.goals, ["Do X\n\nWhat Nexus found"])
        self.assertEqual(experiments.create_kwargs, [{}])
        tracked = github.sources[0]
        self.assertEqual(
            (tracked.source_type, tracked.source_id, tracked.title, tracked.content),
            ("chat", "chat-1", "Nexus chat", "Do X\n\nWhat Nexus found"),
        )

    def test_explicit_source_content_and_create_kwargs_are_kept(self):
        launcher, experiments, github, _, _ = self._launcher()
        source = SimpleNamespace(source_type="task", source_id="t-1", title="T", content="raw")

        launcher.launch(
            "s-1", "Do X", None, source, create_kwargs={"complexity": "small"}
        )

        self.assertEqual(experiments.goals, ["Do X"])
        self.assertEqual(experiments.create_kwargs, [{"complexity": "small"}])
        self.assertEqual(github.sources[0].content, "raw")

    def test_already_active_becomes_a_result_and_nothing_starts(self):
        launcher, _, _, calls, targets = self._launcher(already_active=("exp-old", "running"))

        result = launcher.launch(
            "chat-1", "Do X", None, SOURCE, task_store=FakeTaskStore(calls)
        )

        self.assertTrue(result.already_active)
        self.assertEqual(result.experiment_id, "exp-old")
        self.assertEqual(result.status, "running")
        self.assertEqual(calls, ["ensure_for_source", "create_experiment"])
        self.assertEqual(targets, [])

    def test_github_error_propagates_before_experiment_creation(self):
        launcher, _, _, calls, _ = self._launcher(
            github_error=GitHubWorkTrackingError("no token")
        )

        with self.assertRaises(GitHubWorkTrackingError):
            launcher.launch("chat-1", "Do X", None, SOURCE)
        self.assertEqual(calls, ["ensure_for_source"])

    def test_without_session_nothing_is_parked(self):
        launcher, _, _, calls, _ = self._launcher()

        launcher.launch(None, "Do X", None, SOURCE, task_store=FakeTaskStore(calls))

        self.assertNotIn("wait_for_experiment", [c[0] for c in calls if isinstance(c, tuple)])
        self.assertIn(("background", "exp-1"), calls)


if __name__ == "__main__":
    unittest.main()
