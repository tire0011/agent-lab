"""#792: a retry goal whose appended prior-failure reason lists only test ids
must not turn an implementation issue into a tests_only plan.

A retry appends the previous needs_review reason to the issue body (#374).
When that reason is a timing-check or failing-test list made only of
``tests/...::...`` ids, ``goal_names_only_test_paths`` on the *full* retry
goal returned True, so ``evaluate_implementation`` demoted the plan to
``tests_only`` and the correct production diff then failed
``tests_only_scope`` (experiments 8c21f98a / 39865bbb on #766). The tests_only
relaxation is now decided from the original issue text only.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modules.experiment_evaluation import (
    ChangedFiles,
    evaluate_implementation,
    goal_names_only_test_paths,
    original_task_text,
    validate_plan,
)
from modules.experiment_handover import (
    EARLIER_ATTEMPTS_HEADING,
    HANDOVER_HEADING,
    MODEL_WRITTEN_TESTS_HEADING,
)

# A production-change issue body (like #766): it asks for production changes
# (time limit, kill, run outside the tick) but, as in the real #766 body,
# names no ``x/y.ext`` path — which is exactly why the appended test-id reason
# used to flip goal_names_only_test_paths on the full retry goal.
PRODUCTION_ISSUE_BODY = (
    "GitHub Issue #766: Free re-verification of parked branches must never block "
    "the tick\n\n"
    "**Change**\n"
    "1. Give the whole re-verification run a wall-clock limit (default 45 min, "
    "configurable). When it is exceeded, kill the process group and record "
    "`reverify_timeout`.\n"
    "2. Run the re-verification outside the tick as its own background job.\n"
    "3. Add a hung-test guard that kills the run when the test progress counter "
    "does not advance for N minutes."
)

# What a retry appends when the previous attempt ended in a timing-check
# reason made only of tests/...::... ids.
APPENDED_TEST_ID_REASON = (
    f"\n\n{EARLIER_ATTEMPTS_HEADING}\n"
    "- experiment 4168a4d0, needs_review: timing check failed closed: error: these "
    "tests fail on this host without drift too, so there is no timing verdict: "
    "tests/test_public_logs_api.py::PublicUnifiedLogsApiTests::test_aggregates_entries_across_experiments_chronologically, "
    "tests/test_public_logs_api.py::PublicUnifiedLogsApiTests::test_filters_by_level, "
    "tests/test_public_logs_api.py::PublicUnifiedLogsApiTests::test_no_log_files_returns_empty_entries"
)

# What a handover appends when a command quoted test ids.
APPENDED_HANDOVER_TEST_IDS = (
    f"\n\n{HANDOVER_HEADING}\n\n"
    "Earlier attempts in this run:\n"
    "- command pytest tests/test_public_logs_api.py -q: 3 failed in 12.31s"
)

# What a retry appends when the previous attempt's own failure quoted a
# production path (must not block a legitimately tests-only issue either).
APPENDED_PRODUCTION_PATH_REASON = (
    f"\n\n{EARLIER_ATTEMPTS_HEADING}\n"
    "- experiment 8c21f98a, needs_review: quality evaluation: unerwartete "
    "Nicht-Test-Änderungen bei tests_only-Plan: ['modules/verification_env.py']"
)

RETRY_GOAL = PRODUCTION_ISSUE_BODY + APPENDED_TEST_ID_REASON


def _implementation_plan(**overrides) -> dict:
    base = {
        "title": "Add the re-verification time limit",
        "affected_files": ["modules/x.py", "tests/test_x.py"],
        "change_kind": "implementation",
        "required_artifacts": [],
        "acceptance_criteria": [],
    }
    base.update(overrides)
    return base


def _failed_check_names(result) -> list[str]:
    return [c.name for c in result.checks if not c.passed]


class RetryGoalEvaluateImplementationTestCase(unittest.TestCase):
    def test_raw_predicate_on_full_retry_goal_is_true_the_old_trap(self):
        """Documents the bug: on the full retry goal the raw predicate fired."""
        self.assertTrue(goal_names_only_test_paths(RETRY_GOAL))
        # ...while the original issue text names production files only:
        self.assertFalse(goal_names_only_test_paths(original_task_text(RETRY_GOAL)))

    def test_production_issue_plus_appended_test_id_reason_is_not_tests_only(self):
        """The #792 core case: diff touches modules/x.py and tests, the plan
        stays implementation and no tests_only_scope failure appears."""
        with tempfile.TemporaryDirectory() as worktree:
            (Path(worktree) / "modules").mkdir()
            (Path(worktree) / "modules" / "x.py").write_text('"""doc"""\n')
            changed = ChangedFiles(
                tracked_modified=["modules/x.py"],
                untracked_new=["tests/test_x.py"],
            )
            result = evaluate_implementation(
                _implementation_plan(), RETRY_GOAL, changed, worktree_path=worktree
            )
        self.assertEqual(result.outcome, "pass")
        self.assertNotIn("tests_only_scope", _failed_check_names(result))
        self.assertNotIn("tests_only", result.reason)

    def test_issue_body_naming_a_production_path_plus_appended_test_id_reason(self):
        """Same, but the issue body itself names a production file: the
        relaxation must not fire either way."""
        goal = (
            "Add the wall-clock limit in modules/x.py and run the re-verification "
            "outside the tick." + APPENDED_TEST_ID_REASON
        )
        with tempfile.TemporaryDirectory() as worktree:
            (Path(worktree) / "modules").mkdir()
            (Path(worktree) / "modules" / "x.py").write_text('"""doc"""\n')
            changed = ChangedFiles(
                tracked_modified=["modules/x.py"],
                untracked_new=["tests/test_x.py"],
            )
            result = evaluate_implementation(
                _implementation_plan(), goal, changed, worktree_path=worktree
            )
        self.assertEqual(result.outcome, "pass")
        self.assertNotIn("tests_only_scope", _failed_check_names(result))

    def test_retry_goal_does_not_demote_a_tests_only_shaped_plan(self):
        """Even when the planner listed only test files, the appended test-id
        reason must not demote the plan: the production diff stays valid."""
        with tempfile.TemporaryDirectory() as worktree:
            (Path(worktree) / "modules").mkdir()
            (Path(worktree) / "modules" / "x.py").write_text('"""doc"""\n')
            plan = _implementation_plan(affected_files=["tests/test_x.py"])
            changed = ChangedFiles(
                tracked_modified=["modules/x.py"],
                untracked_new=["tests/test_x.py"],
            )
            result = evaluate_implementation(
                plan, RETRY_GOAL, changed, worktree_path=worktree
            )
        self.assertEqual(result.outcome, "pass")
        self.assertNotIn("tests_only_scope", _failed_check_names(result))

    def test_handover_quoting_test_ids_does_not_trigger_tests_only(self):
        with tempfile.TemporaryDirectory() as worktree:
            (Path(worktree) / "modules").mkdir()
            (Path(worktree) / "modules" / "x.py").write_text('"""doc"""\n')
            goal = PRODUCTION_ISSUE_BODY + APPENDED_HANDOVER_TEST_IDS
            changed = ChangedFiles(
                tracked_modified=["modules/x.py"],
                untracked_new=["tests/test_x.py"],
            )
            result = evaluate_implementation(
                _implementation_plan(), goal, changed, worktree_path=worktree
            )
        self.assertEqual(result.outcome, "pass")
        self.assertNotIn("tests_only_scope", _failed_check_names(result))

    def test_only_add_tests_body_with_non_test_diff_still_fails_tests_only_scope(self):
        """Explicit wording in the issue itself still relaxes — and still
        enforces the scope when the diff touches production code."""
        goal = (
            "Only add tests for the foo() helper in tests/test_foo.py; "
            "do not change production code." + APPENDED_TEST_ID_REASON
        )
        plan = _implementation_plan(
            affected_files=["tests/test_foo.py"], change_kind="tests_only"
        )
        changed = ChangedFiles(
            tracked_modified=["modules/x.py"], untracked_new=["tests/test_foo.py"]
        )
        result = evaluate_implementation(plan, goal, changed, worktree_path=None)
        self.assertEqual(result.outcome, "needs_review")
        self.assertIn("tests_only_scope", _failed_check_names(result))
        self.assertIn("tests_only-Plan", result.reason)

    def test_explicit_tests_only_body_with_test_only_diff_still_passes(self):
        goal = (
            "Only add tests for the foo() helper; do not change production code."
            + APPENDED_TEST_ID_REASON
        )
        plan = _implementation_plan(
            affected_files=["tests/test_foo.py"], change_kind="tests_only"
        )
        changed = ChangedFiles(tracked_modified=[], untracked_new=["tests/test_foo.py"])
        result = evaluate_implementation(plan, goal, changed, worktree_path=None)
        self.assertEqual(result.outcome, "pass")

    def test_goal_naming_only_test_paths_in_own_body_still_relaxes(self):
        """A tests-only issue stays relaxed even when an appended retry reason
        quotes a production path: the decision uses the original text only."""
        goal = (
            "Fix tests/conftest.py so LOCAL_MODEL_URL is configured for the tests."
            + APPENDED_PRODUCTION_PATH_REASON
        )
        plan = _implementation_plan(affected_files=["tests/conftest.py"])
        changed = ChangedFiles(tracked_modified=["tests/conftest.py"], untracked_new=[])
        result = evaluate_implementation(plan, goal, changed, worktree_path=None)
        self.assertEqual(result.outcome, "pass")
        self.assertNotIn("nur Tests geändert", result.reason)

    def test_issue_text_parameter_overrides_the_goal(self):
        """Callers that know the original issue text can pass it separately."""
        with tempfile.TemporaryDirectory() as worktree:
            (Path(worktree) / "modules").mkdir()
            (Path(worktree) / "modules" / "x.py").write_text('"""doc"""\n')
            changed = ChangedFiles(
                tracked_modified=["modules/x.py"], untracked_new=["tests/test_x.py"]
            )
            result = evaluate_implementation(
                _implementation_plan(),
                RETRY_GOAL,
                changed,
                worktree_path=worktree,
                issue_text=PRODUCTION_ISSUE_BODY,
            )
        self.assertEqual(result.outcome, "pass")
        self.assertNotIn("tests_only_scope", _failed_check_names(result))


class RetryGoalValidatePlanTestCase(unittest.TestCase):
    def test_tests_only_plan_not_justified_by_appended_test_id_reason(self):
        plan = _implementation_plan(
            affected_files=["tests/test_x.py"], change_kind="tests_only"
        )
        reason = validate_plan(plan, goal=RETRY_GOAL)
        self.assertIsNotNone(reason)
        self.assertIn("tests_only", reason)

    def test_tests_only_plan_justified_by_explicit_wording_in_issue_body(self):
        plan = _implementation_plan(
            affected_files=["tests/test_x.py"], change_kind="tests_only"
        )
        goal = "Only add tests for the foo() helper; do not change production code."
        self.assertIsNone(validate_plan(plan, goal=goal + APPENDED_TEST_ID_REASON))

    def test_tests_only_plan_justified_by_test_paths_in_own_body(self):
        plan = _implementation_plan(
            affected_files=["tests/conftest.py"], change_kind="tests_only"
        )
        goal = "Fix tests/conftest.py so LOCAL_MODEL_URL is configured."
        # An appended reason quoting a production path must not revoke the
        # relaxation that the original issue text grants.
        self.assertIsNone(validate_plan(plan, goal=goal + APPENDED_PRODUCTION_PATH_REASON))

    def test_issue_text_parameter_overrides_the_goal(self):
        plan = _implementation_plan(
            affected_files=["tests/test_x.py"], change_kind="tests_only"
        )
        self.assertIsNone(
            validate_plan(
                plan, goal=RETRY_GOAL, issue_text="Only add tests for the foo helper."
            )
        )


class OriginalTaskTextTestCase(unittest.TestCase):
    def test_plain_goal_is_returned_unchanged(self):
        goal = "Refactor modules/foo.py and update tests/test_foo.py."
        self.assertEqual(original_task_text(goal), goal)

    def test_appended_sections_are_stripped(self):
        goal = (
            "Fix modules/x.py."
            + APPENDED_TEST_ID_REASON
            + APPENDED_HANDOVER_TEST_IDS
            + f"\n\n{MODEL_WRITTEN_TESTS_HEADING}\ntests/test_y.py"
        )
        self.assertEqual(original_task_text(goal).strip(), "Fix modules/x.py.")

    def test_empty_goal(self):
        self.assertEqual(original_task_text(""), "")


if __name__ == "__main__":
    unittest.main()
