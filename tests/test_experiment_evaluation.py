"""Regression tests for the deterministic (non-LLM) quality evaluation gate
in ``modules.experiment_evaluation`` and its wiring into
``modules.experiment_service.ExperimentService``.

The concrete regression case that motivated this module: experiment
``eb57900b7fda4d19812fee7b0219baac`` was asked to extract the Logs-tab
behaviour out of ``static/app.js`` into a new ``static/logs.js`` module, but
the implementation phase only ever touched ``tests/test_api.py`` — the
tests happened to pass against the unmodified ``app.js`` — and the
experiment was still marked ``succeeded``. That must now always resolve to
``needs_review`` instead, and never silently to ``succeeded``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock

from modules.experiment_evaluation import (
    ChangedFiles,
    EvaluationResult,
    PlanLike,
    evaluate_implementation,
    extract_explicit_goal_artifacts,
    goal_is_explicit_tests_only,
    parse_git_status_short,
    split_explicit_goal_artifacts,
    validate_plan,
)
from modules.experiment_service import (
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_SUCCEEDED,
    ExperimentPlan,
    ExperimentService,
)

from tests.test_experiment_service import (
    _init_git_repo,
    _make_result,
    _write_affected_files_side_effect,
)


# ---------------------------------------------------------------------------
# The exact eb57900b7fda4d19812fee7b0219baac regression, at the unit level.
# ---------------------------------------------------------------------------


EB57900_GOAL = (
    "Extract the existing Logs-tab behaviour out of static/app.js into a "
    "new static/logs.js module so the Logs tab no longer depends on the "
    "monolithic app.js file."
)

# This mirrors the real, stored ``plan_json`` for the regression experiment:
# the model's policy-replan silently swapped in a tests-only-looking plan
# that only names ``tests/test_api.py`` in ``affected_files`` and predates
# the ``change_kind``/``required_artifacts`` fields entirely.
EB57900_STORED_PLAN = {
    "title": "Add regression tests for the logs endpoint",
    "motivation": "Ensure coverage for existing API behaviour.",
    "affected_files": ["tests/test_api.py"],
    "expected_changes": "Add tests to tests/test_api.py.",
    "success_criteria": "New tests pass.",
    "planned_tests": "tests/test_api.py",
    "risks": "None significant.",
    "estimated_scope": "10 minutes",
    # No change_kind / required_artifacts / acceptance_criteria keys at all
    # — this is exactly what the historical stored plan looks like.
}


class Eb57900RegressionTestCase(unittest.TestCase):
    """Exact reproduction: only tests/test_api.py changed, tests green,
    static/logs.js never created -> must be needs_review, never succeeded."""

    def test_only_test_file_changed_is_needs_review_not_succeeded(self):
        changed = ChangedFiles(tracked_modified=["tests/test_api.py"], untracked_new=[])
        result = evaluate_implementation(
            EB57900_STORED_PLAN, EB57900_GOAL, changed, worktree_path=None
        )
        self.assertEqual(result.outcome, "needs_review")
        self.assertNotEqual(result.outcome, "pass")
        self.assertIn("nur Tests geändert", result.reason)
        self.assertIn("erforderliches Artefakt static/logs.js fehlt", result.reason)
        self.assertIn("static/logs.js", result.missing_artifacts)

    def test_end_to_end_pipeline_never_reports_succeeded_for_eb57900_case(self):
        """Full pipeline wiring: implementation worker behaves exactly like
        the historical run (only edits tests/test_api.py), tests pass, and
        the final experiment status must be needs_review."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo_root = Path(tmp.name) / "repo"
        repo_root.mkdir()
        _init_git_repo(repo_root)
        (repo_root / "static").mkdir()
        (repo_root / "static" / "app.js").write_text("// logs tab lives here today\n")
        subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo_root), "commit", "-q", "-m", "add app.js"], check=True
        )

        selection_worker = MagicMock()
        selection_worker.run.return_value = _make_result(
            final_message="Research summary: looked at static/app.js and tests."
        )
        plan_worker = MagicMock()
        # This mirrors the plan that was actually *approved* by the
        # pre-implementation completeness gate for the real historical
        # experiment (it names the promised static/logs.js artifact), but
        # the implementation phase below reproduces exactly what actually
        # happened in production: only tests/test_api.py was ever touched,
        # static/logs.js was never created. The deterministic
        # post-implementation quality gate — not the test suite — is what
        # must catch this.
        approved_plan = dict(EB57900_STORED_PLAN)
        approved_plan["affected_files"] = ["static/logs.js", "tests/test_api.py"]
        approved_plan["change_kind"] = "implementation"
        plan_worker.run.return_value = _make_result(
            final_message=json.dumps(approved_plan)
        )

        implementation_worker = MagicMock()

        def _only_touch_test_api(*args, **kwargs):
            working_dir = kwargs.get("working_dir")
            test_file = Path(working_dir) / "tests" / "test_api.py"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text(
                "def test_logs_endpoint_smoke():\n    assert True\n"
            )
            return _make_result(final_message="implemented", sandbox="workspace-write")

        implementation_worker.run.side_effect = _only_touch_test_api

        service = ExperimentService(
            selection_worker=selection_worker,
            plan_worker_factory=lambda scratch_dir: plan_worker,
            worker_factory=lambda worktree_path: implementation_worker,
            db_path=str(Path(tmp.name) / "agent.db"),
            repo_root=str(repo_root),
            worktrees_root=str(Path(tmp.name) / "worktrees"),
            test_python_executable=sys.executable,
        )
        self.addCleanup(service.close)

        experiment = service.create_experiment(goal=EB57900_GOAL)
        result = service.run_experiment(experiment.id)

        self.assertNotEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertIsNotNone(result.quality_evaluation)
        self.assertEqual(result.quality_evaluation["outcome"], "needs_review")
        self.assertIn("nur Tests geändert", result.quality_evaluation["reason"])
        # The worktree must still exist for manual review.
        self.assertTrue(Path(result.worktree_path).exists())


# ---------------------------------------------------------------------------
# Unit-level checks for modules.experiment_evaluation
# ---------------------------------------------------------------------------


class ValidatePlanTestCase(unittest.TestCase):
    def test_implementation_plan_without_any_non_test_file_is_rejected(self):
        plan = {
            "title": "x",
            "affected_files": ["tests/test_foo.py"],
            "change_kind": "implementation",
        }
        reason = validate_plan(plan, goal="Refactor the widget module.")
        self.assertIsNotNone(reason)

    def test_tests_only_plan_naming_a_non_test_file_is_rejected(self):
        plan = {
            "title": "x",
            "affected_files": ["modules/foo.py", "tests/test_foo.py"],
            "change_kind": "tests_only",
        }
        reason = validate_plan(plan, goal="Only add tests for module foo.")
        self.assertIsNotNone(reason)

    def test_refactor_task_declared_tests_only_is_rejected(self):
        """A refactor request must not be relabelled tests_only by the model."""
        plan = {
            "title": "x",
            "affected_files": ["tests/test_logs.py"],
            "change_kind": "tests_only",
        }
        goal = "Refactor static/app.js to move the logs tab into static/logs.js."
        reason = validate_plan(plan, goal=goal)
        self.assertIsNotNone(reason)
        self.assertIn("tests_only", reason)

    def test_explicit_tests_only_task_is_accepted(self):
        plan = {
            "title": "x",
            "affected_files": ["tests/test_foo.py"],
            "change_kind": "tests_only",
        }
        goal = "Only add tests for the existing foo() helper; do not change production code."
        reason = validate_plan(plan, goal=goal)
        self.assertIsNone(reason)

    def test_analysis_plan_needs_no_files(self):
        plan = {"title": "x", "affected_files": [], "change_kind": "analysis"}
        reason = validate_plan(plan, goal="Investigate why tests are flaky.")
        self.assertIsNone(reason)

    def test_unknown_change_kind_is_rejected(self):
        plan = {"title": "x", "affected_files": ["modules/foo.py"], "change_kind": "bogus"}
        reason = validate_plan(plan, goal="Do something.")
        self.assertIsNotNone(reason)

    def test_legacy_plan_dict_without_change_kind_is_still_validated(self):
        """Old stored plans have no change_kind/required_artifacts/
        acceptance_criteria keys at all; validate_plan must not crash and
        must default to the strict 'implementation' behaviour."""
        legacy_plan = {
            "title": "Legacy plan",
            "affected_files": ["modules/foo.py", "tests/test_foo.py"],
        }
        reason = validate_plan(legacy_plan, goal="Implement foo.")
        self.assertIsNone(reason)

        legacy_plan_tests_only_shaped = {
            "title": "Legacy tests-only-looking plan",
            "affected_files": ["tests/test_foo.py"],
        }
        # Defaults to "implementation", which requires a non-test file or
        # required artifact — this legacy plan names none, so it is now
        # correctly flagged rather than silently accepted.
        reason2 = validate_plan(legacy_plan_tests_only_shaped, goal="Implement foo.")
        self.assertIsNotNone(reason2)


class GoalHeuristicsTestCase(unittest.TestCase):
    def test_goal_mentioning_concrete_path_is_never_tests_only(self):
        self.assertFalse(
            goal_is_explicit_tests_only(
                "Only add tests for static/logs.js, nothing else."
            )
        )

    def test_goal_explicitly_tests_only_without_paths_is_accepted(self):
        self.assertTrue(
            goal_is_explicit_tests_only("Only add tests; do not change production code.")
        )

    def test_goal_extraction_finds_explicit_path(self):
        artifacts = extract_explicit_goal_artifacts(EB57900_GOAL)
        self.assertIn("static/logs.js", artifacts)


class ParseGitStatusShortTestCase(unittest.TestCase):
    def test_tracked_and_untracked_files_are_both_detected(self):
        status_text = "\n".join([
            " M modules/existing.py",
            "?? static/logs.js",
            "?? tests/__pycache__/test_foo.cpython-310.pyc",
            "?? .git/hooks/pre-commit.sample",
        ])
        changed = parse_git_status_short(status_text)
        self.assertEqual(changed.tracked_modified, ["modules/existing.py"])
        self.assertEqual(changed.untracked_new, ["static/logs.js"])
        # Noise (pycache, .git) must never leak into either list.
        self.assertNotIn("tests/__pycache__/test_foo.cpython-310.pyc", changed.all_paths())
        self.assertNotIn(".git/hooks/pre-commit.sample", changed.all_paths())

    def test_rename_line_uses_new_path_only(self):
        changed = parse_git_status_short("R  old/name.py -> new/name.py")
        self.assertEqual(changed.tracked_modified, ["new/name.py"])


class EvaluateImplementationTestCase(unittest.TestCase):
    def _plan(self, **overrides) -> dict:
        base = {
            "title": "Implement foo",
            "affected_files": ["modules/foo.py", "tests/test_foo.py"],
            "change_kind": "implementation",
            "required_artifacts": [],
            "acceptance_criteria": [],
        }
        base.update(overrides)
        return base

    def test_valid_implementation_change_plus_tests_succeeds(self):
        with tempfile.TemporaryDirectory() as worktree:
            (Path(worktree) / "modules").mkdir()
            (Path(worktree) / "modules" / "foo.py").write_text('"""doc"""\n')
            changed = ChangedFiles(
                tracked_modified=["modules/foo.py"],
                untracked_new=["tests/test_foo.py"],
            )
            result = evaluate_implementation(
                self._plan(), "Implement foo.", changed, worktree_path=worktree
            )
            self.assertEqual(result.outcome, "pass")

    def test_valid_new_untracked_required_file_succeeds(self):
        with tempfile.TemporaryDirectory() as worktree:
            (Path(worktree) / "static").mkdir()
            (Path(worktree) / "static" / "logs.js").write_text("// logs\n")
            plan = self._plan(
                affected_files=["static/logs.js"],
                required_artifacts=["static/logs.js"],
            )
            changed = ChangedFiles(tracked_modified=[], untracked_new=["static/logs.js"])
            result = evaluate_implementation(plan, "Extract logs.", changed, worktree_path=worktree)
            self.assertEqual(result.outcome, "pass")

    def test_legitimate_explicit_tests_only_task_succeeds(self):
        plan = self._plan(
            affected_files=["tests/test_foo.py"],
            change_kind="tests_only",
        )
        changed = ChangedFiles(tracked_modified=[], untracked_new=["tests/test_foo.py"])
        result = evaluate_implementation(
            plan,
            "Only add tests for foo(); do not change production code.",
            changed,
            worktree_path=None,
        )
        self.assertEqual(result.outcome, "pass")

    def test_missing_required_artifact_is_needs_review(self):
        with tempfile.TemporaryDirectory() as worktree:
            plan = self._plan(required_artifacts=["static/logs.js"])
            changed = ChangedFiles(
                tracked_modified=["modules/foo.py"], untracked_new=[]
            )
            result = evaluate_implementation(
                plan, "Extract logs.", changed, worktree_path=worktree
            )
            self.assertEqual(result.outcome, "needs_review")
            self.assertIn("static/logs.js", result.missing_artifacts)

    def test_tests_only_plan_with_unexpected_non_test_change_is_needs_review(self):
        plan = self._plan(affected_files=["tests/test_foo.py"], change_kind="tests_only")
        changed = ChangedFiles(
            tracked_modified=["modules/unexpected.py"], untracked_new=["tests/test_foo.py"]
        )
        result = evaluate_implementation(
            plan, "Only add tests; do not change production code.", changed, worktree_path=None
        )
        self.assertEqual(result.outcome, "needs_review")

    def test_no_plan_explicit_tests_only_goal_with_only_test_files_passes(self):
        """Single-call pipeline: plan=None + explicit tests-only goal => pass."""
        changed = ChangedFiles(
            tracked_modified=[], untracked_new=["tests/test_foo.py"]
        )
        result = evaluate_implementation(
            None,
            "Only add tests for foo(); do not change production code.",
            changed,
            worktree_path=None,
        )
        self.assertEqual(result.outcome, "pass")

    def test_no_plan_ordinary_goal_with_only_test_files_is_needs_review(self):
        """Single-call pipeline: plan=None + ordinary goal => needs_review when only tests changed."""
        changed = ChangedFiles(
            tracked_modified=["tests/test_api.py"], untracked_new=[]
        )
        result = evaluate_implementation(
            None,
            "Extract the logs tab into static/logs.js.",
            changed,
            worktree_path=None,
        )
        self.assertEqual(result.outcome, "needs_review")

    def test_no_plan_goal_naming_non_test_artifact_is_not_tests_only(self):
        """Single-call pipeline: plan=None, goal names a non-test path => must not be tests_only."""
        changed = ChangedFiles(
            tracked_modified=[], untracked_new=["tests/test_foo.py"]
        )
        result = evaluate_implementation(
            None,
            "Refactor static/logs.js and add tests.",
            changed,
            worktree_path=None,
        )
        # goal names a non-test artifact so change_kind stays implementation,
        # and only test files changed -> needs_review
        self.assertEqual(result.outcome, "needs_review")


class PlanLikeCompatibilityTestCase(unittest.TestCase):
    def test_experiment_plan_dataclass_round_trips(self):
        plan = ExperimentPlan(
            title="t",
            motivation="m",
            affected_files=["modules/foo.py"],
            expected_changes="e",
            success_criteria="s",
            planned_tests="p",
            risks="r",
            estimated_scope="1h",
        )
        p = PlanLike.from_any(plan)
        self.assertEqual(p.change_kind, "implementation")
        self.assertEqual(p.required_artifacts, [])
        self.assertEqual(p.acceptance_criteria, [])

    def test_none_plan_does_not_raise(self):
        p = PlanLike.from_any(None)
        self.assertEqual(p.change_kind, "implementation")
        self.assertEqual(p.affected_files, [])


# ---------------------------------------------------------------------------
# Pipeline-level integration tests through ExperimentService
# ---------------------------------------------------------------------------


VALID_IMPLEMENTATION_PLAN_JSON = json.dumps({
    "title": "Add a docstring test",
    "motivation": "Improve coverage of a small utility.",
    "affected_files": ["modules/foo.py", "tests/test_foo.py"],
    "expected_changes": "Add a docstring and a unit test.",
    "success_criteria": "New test passes.",
    "planned_tests": "tests/test_foo.py::test_docstring",
    "risks": "None significant.",
    "estimated_scope": "5 minutes",
    "change_kind": "implementation",
    "required_artifacts": [],
    "acceptance_criteria": [],
})

TESTS_ONLY_PLAN_JSON = json.dumps({
    "title": "Add regression test",
    "motivation": "Improve coverage.",
    "affected_files": ["tests/test_foo.py"],
    "expected_changes": "Add a unit test.",
    "success_criteria": "New test passes.",
    "planned_tests": "tests/test_foo.py::test_docstring",
    "risks": "None significant.",
    "estimated_scope": "5 minutes",
    "change_kind": "tests_only",
    "required_artifacts": [],
    "acceptance_criteria": [],
})


def _service(repo_root: Path, tmp_name: str, selection_worker, plan_worker, implementation_worker):
    return ExperimentService(
        selection_worker=selection_worker,
        plan_worker_factory=lambda scratch_dir: plan_worker,
        worker_factory=lambda worktree_path: implementation_worker,
        db_path=str(Path(tmp_name) / "agent.db"),
        repo_root=str(repo_root),
        worktrees_root=str(Path(tmp_name) / "worktrees"),
        test_python_executable=sys.executable,
    )


class PipelineIntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name) / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)

        self.selection_worker = MagicMock()
        self.selection_worker.run.return_value = _make_result(
            final_message="Research summary text."
        )

    def test_valid_implementation_change_plus_tests_succeeds(self):
        plan_worker = MagicMock()
        plan_worker.run.return_value = _make_result(final_message=VALID_IMPLEMENTATION_PLAN_JSON)
        implementation_worker = MagicMock()
        implementation_worker.run.side_effect = _write_affected_files_side_effect()

        service = _service(
            self.repo_root, self.tmp.name, self.selection_worker, plan_worker, implementation_worker
        )
        self.addCleanup(service.close)
        experiment = service.create_experiment(goal="Add a small docstring test.")
        result = service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(result.quality_evaluation["outcome"], "pass")

    def test_legitimate_tests_only_task_succeeds(self):
        # The single-call pipeline never invokes the plan worker, so
        # experiment.plan stays None.  The evaluator must derive the
        # tests_only classification solely from the goal text.
        implementation_worker = MagicMock()

        def _write_test_only(*args, **kwargs):
            working_dir = kwargs["working_dir"]
            test_file = Path(working_dir) / "tests" / "test_foo.py"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text("def test_docstring():\n    assert True\n")
            return _make_result(final_message="implemented", sandbox="workspace-write")

        implementation_worker.run.side_effect = _write_test_only

        service = _service(
            self.repo_root, self.tmp.name, self.selection_worker, MagicMock(), implementation_worker
        )
        self.addCleanup(service.close)
        experiment = service.create_experiment(
            goal="Only add tests for the existing foo() helper; do not change production code."
        )
        result = service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_SUCCEEDED)
        self.assertEqual(result.quality_evaluation["outcome"], "pass")

    def test_missing_required_artifact_yields_needs_review_and_preserves_worktree(self):
        plan_json = json.dumps({
            "title": "Extract logs into static/logs.js",
            "motivation": "Modularize logs tab.",
            "affected_files": ["static/logs.js"],
            "expected_changes": "Create static/logs.js.",
            "success_criteria": "Logs tab still works.",
            "planned_tests": "tests/test_api.py",
            "risks": "None significant.",
            "estimated_scope": "20 minutes",
            "change_kind": "implementation",
            "required_artifacts": ["static/logs.js"],
            "acceptance_criteria": ["static/logs.js exists"],
        })
        plan_worker = MagicMock()
        plan_worker.run.return_value = _make_result(final_message=plan_json)
        implementation_worker = MagicMock()

        def _forget_to_create_logs_js(*args, **kwargs):
            # Implementation worker claims success but never actually
            # creates the required artifact.
            return _make_result(final_message="implemented", sandbox="workspace-write")

        implementation_worker.run.side_effect = _forget_to_create_logs_js

        service = _service(
            self.repo_root, self.tmp.name, self.selection_worker, plan_worker, implementation_worker
        )
        self.addCleanup(service.close)
        experiment = service.create_experiment(goal="Extract logs into static/logs.js.")
        result = service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertIn("static/logs.js", result.quality_evaluation["missing_artifacts"])
        self.assertTrue(Path(result.worktree_path).exists())

    def test_failed_tests_remain_failed_regardless_of_quality_evaluation(self):
        plan_worker = MagicMock()
        plan_worker.run.return_value = _make_result(final_message=VALID_IMPLEMENTATION_PLAN_JSON)
        implementation_worker = MagicMock()

        def _write_failing_test(*args, **kwargs):
            working_dir = kwargs["working_dir"]
            (Path(working_dir) / "modules").mkdir(exist_ok=True)
            (Path(working_dir) / "modules" / "foo.py").write_text('"""doc"""\n')
            test_file = Path(working_dir) / "tests" / "test_foo.py"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text(
                "import unittest\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_docstring(self):\n"
                "        self.assertTrue(False)\n"
            )
            return _make_result(final_message="implemented", sandbox="workspace-write")

        implementation_worker.run.side_effect = _write_failing_test

        service = _service(
            self.repo_root, self.tmp.name, self.selection_worker, plan_worker, implementation_worker
        )
        self.addCleanup(service.close)
        experiment = service.create_experiment(goal="Add a small docstring test.")
        result = service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_FAILED)

    def test_evaluation_exception_never_yields_succeeded(self):
        plan_worker = MagicMock()
        plan_worker.run.return_value = _make_result(final_message=VALID_IMPLEMENTATION_PLAN_JSON)
        implementation_worker = MagicMock()
        implementation_worker.run.side_effect = _write_affected_files_side_effect()

        service = _service(
            self.repo_root, self.tmp.name, self.selection_worker, plan_worker, implementation_worker
        )
        self.addCleanup(service.close)

        with unittest.mock.patch(
            "modules.experiment_service.evaluate_implementation",
            side_effect=RuntimeError("boom: evaluator bug"),
        ):
            experiment = service.create_experiment(goal="Add a small docstring test.")
            result = service.run_experiment(experiment.id)

        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertNotEqual(result.status, STATUS_SUCCEEDED)

    def test_logging_exception_during_evaluation_never_yields_succeeded(self):
        plan_worker = MagicMock()
        plan_worker.run.return_value = _make_result(final_message=VALID_IMPLEMENTATION_PLAN_JSON)
        implementation_worker = MagicMock()
        implementation_worker.run.side_effect = _write_affected_files_side_effect()

        service = _service(
            self.repo_root, self.tmp.name, self.selection_worker, plan_worker, implementation_worker
        )
        self.addCleanup(service.close)

        from modules.central_logging import ExperimentLogger

        original_info = ExperimentLogger.info

        def _flaky_info(self, phase, event, message, **kwargs):
            if event in ("quality_evaluation_passed", "quality_evaluation_failed"):
                raise OSError("simulated disk failure")
            return original_info(self, phase, event, message, **kwargs)

        with unittest.mock.patch.object(ExperimentLogger, "info", _flaky_info):
            experiment = service.create_experiment(goal="Add a small docstring test.")
            result = service.run_experiment(experiment.id)

        self.assertNotEqual(result.status, STATUS_SUCCEEDED)


# ---------------------------------------------------------------------------
# #416: with sections, only paths in the Change section are required.
# ---------------------------------------------------------------------------

# Goal text of #403 as experiment 664ee34c saw it: Problem names paths inside
# the git directory, Test names made-up fixture paths.
ISSUE_403_GOAL = (
    "GitHub Issue #403: Quality gate: drop paths inside the git directory from required artifacts\n\n"
    "**Problem**\n"
    "#349 and #378 ended needs_review for missing git/hooks/pre-commit.sample and "
    "git/outbox_publisher.lock. `modules/experiment_evaluation.py` only drops git-ignored paths.\n\n"
    "**Change**\n"
    "- In `modules/experiment_evaluation.py`, add a `_is_git_internal(path, worktree_path)` filter.\n"
    "- Each dropped path gets one line in the `required_artifacts_present` detail.\n\n"
    "**Test** (`tests/test_experiment_evaluation.py`)\n"
    "- A plan naming modules/x.py and modules/y.py plus the two git paths reports only real misses.\n\n"
    "---\n"
    "Suggested by the Nexus planning step."
)


class ChangeSectionArtifactsTestCase(unittest.TestCase):
    def _worktree(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "modules").mkdir()
        (Path(tmp.name) / "modules" / "experiment_evaluation.py").write_text('"""doc"""\n')
        return tmp.name

    def _detail(self, result) -> str:
        return next(c.detail for c in result.checks if c.name == "required_artifacts_present")

    def test_issue_403_goal_passes_without_plan(self):
        changed = ChangedFiles(
            tracked_modified=["modules/experiment_evaluation.py", "tests/test_experiment_evaluation.py"],
        )
        result = evaluate_implementation(None, ISSUE_403_GOAL, changed, worktree_path=self._worktree())
        self.assertEqual(result.outcome, "pass", result.reason)
        self.assertEqual(result.missing_artifacts, [])
        self.assertEqual(result.expected_files, ["modules/experiment_evaluation.py"])

    def test_detail_lists_skipped_problem_and_test_paths(self):
        changed = ChangedFiles(tracked_modified=["modules/experiment_evaluation.py"])
        result = evaluate_implementation(None, ISSUE_403_GOAL, changed, worktree_path=self._worktree())
        detail = self._detail(result)
        for path in ("git/hooks/pre-commit.sample", "git/outbox_publisher.lock", "modules/x.py", "modules/y.py"):
            self.assertIn(f"skipped (outside Change section): {path}", detail)
        self.assertEqual(detail.count("skipped (outside Change section)"), 4)
        self.assertIn("git/outbox_publisher.lock", result.to_dict()["checks"][1]["detail"])

    def test_missing_change_path_still_fails(self):
        goal = (
            "**Problem**\nSee modules/experiment_evaluation.py.\n\n"
            "**Change**\nAdd modules/new_thing.py.\n\n"
            "**Test**\nCover modules/x.py.\n"
        )
        changed = ChangedFiles(tracked_modified=["modules/experiment_evaluation.py"])
        result = evaluate_implementation(None, goal, changed, worktree_path=self._worktree())
        self.assertEqual(result.outcome, "needs_review")
        self.assertEqual(result.missing_artifacts, ["modules/new_thing.py"])
        self.assertIn("skipped (outside Change section): modules/x.py", self._detail(result))

    def test_heading_styles(self):
        for goal in (
            "Problem: see modules/a.py.\n\nChange: add modules/b.py.\nAlso modules/c.py.\n\nTest: modules/d.py",
            "## Problem\nmodules/a.py\n\n## Change\nmodules/b.py and modules/c.py\n\n## Test\nmodules/d.py",
            "**Problem:** modules/a.py\n\n**Change:** modules/b.py\nmodules/c.py\n\n**Test**\nmodules/d.py",
        ):
            with self.subTest(goal=goal.splitlines()[0]):
                self.assertEqual(
                    split_explicit_goal_artifacts(goal),
                    (["modules/b.py", "modules/c.py"], ["modules/a.py", "modules/d.py"]),
                )

    def test_goal_without_sections_keeps_whole_text(self):
        goal = "Problem in static/app.js; extract it into static/logs.js."
        self.assertEqual(split_explicit_goal_artifacts(goal), (["static/app.js", "static/logs.js"], []))
        self.assertEqual(extract_explicit_goal_artifacts(EB57900_GOAL), ["static/app.js", "static/logs.js"])


if __name__ == "__main__":
    unittest.main()
