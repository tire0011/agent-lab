"""Experiment-service tests plus issue #190 Git-policy regressions.

The original test module is retained as ``experiment_service_legacy`` so the
large existing suite remains unchanged. Tests that asserted the now-obsolete
Git keyword deny policy are explicitly skipped and replaced below with the new
branch-workflow expectations.
"""

import json
import unittest

from tests.experiment_service_legacy import *  # noqa: F401,F403
from modules.experiment_service import _check_goal_policy, parse_plan, plan_violates_policy


_OBSOLETE_GIT_DENY_TESTS = {
    "test_policy_violation_persists_matched_policy_rule",
    "test_goal_mentioning_commit_is_rejected",
    "test_rejected_experiment_never_creates_worktree",
    "test_push_and_merge_are_still_rejected",
    "test_git_merge_command_is_blocked",
    "test_commit_changes_is_blocked",
    "test_merge_pull_request_is_blocked",
    "test_commit_result_is_blocked",
    "test_git_action_rule_classified_as_disallowed_keyword",
}

for _candidate in tuple(globals().values()):
    if isinstance(_candidate, type) and issubclass(_candidate, unittest.TestCase):
        for _test_name in _OBSOLETE_GIT_DENY_TESTS:
            if hasattr(_candidate, _test_name):
                _original = getattr(_candidate, _test_name)
                setattr(
                    _candidate,
                    _test_name,
                    unittest.skip("obsolete Git keyword policy removed by issue #190")(
                        _original
                    ),
                )


class GitWorkflowPolicyRegressionTest(unittest.TestCase):
    def test_issue_186_reporting_phrase_is_allowed(self):
        goal = (
            "Implement GitHub issue #186 on a dedicated branch. Final result must "
            "report commit SHA(s), tests run/results, CI result, and explicitly "
            "confirm not merged and not deployed."
        )
        self.assertIsNone(_check_goal_policy(goal))

    def test_normal_branch_git_workflow_is_allowed_in_goal(self):
        self.assertIsNone(
            _check_goal_policy(
                "Commit the changes, push the experiment branch, and open a pull request."
            )
        )
        self.assertIsNone(_check_goal_policy("run git merge to integrate changes"))

    def test_normal_branch_git_workflow_is_allowed_in_plan(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = (
            "Implement the change, commit it, push the experiment branch, and prepare a PR."
        )
        raw = json.dumps(data)
        plan = parse_plan(raw)
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, raw))

    def test_non_git_safety_checks_remain_active(self):
        self.assertIsNotNone(_check_goal_policy("Run pip install requests."))
        self.assertIsNotNone(_check_goal_policy("Delete the production server."))
        self.assertIsNotNone(_check_goal_policy("Read the API key from the environment."))
        self.assertIsNotNone(_check_goal_policy("Run rm -rf on generated files."))
