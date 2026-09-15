from pathlib import Path
import re

policy_legacy = Path('modules/experiment_service/policy_legacy.py')
policy_path = Path('modules/experiment_service/policy.py')
tests_legacy = Path('tests/experiment_service_legacy.py')
tests_path = Path('tests/test_experiment_service.py')

policy = policy_legacy.read_text()
policy = policy.replace('    "commit", "push", "merge",\n', '', 1)

# Goal policy: normal experiment-branch Git actions are not safety violations.
policy = policy.replace(
    '    # Plain substring check — skip git-action keywords; they are checked\n'
    '    # with word-boundary and context awareness below.\n'
    '    for keyword in _REJECT_KEYWORDS:\n'
    '        if keyword in _GIT_ACTION_KEYWORDS:\n'
    '            continue\n'
    '        if keyword in haystack:\n'
    '            return f"goal mentions disallowed keyword: {keyword!r}"\n'
    '    # Word-boundary + negation-aware git-action check.\n'
    '    for regex in _GIT_ACTION_REGEXES:\n'
    '        for match in regex.finditer(haystack):\n'
    '            preceding = haystack[max(0, match.start() - 200):match.start()]\n'
    '            if _infra_action_match_is_false_positive(match.group(0), preceding):\n'
    '                continue\n'
    '            return f"goal requests a disallowed git action: {match.group(0)!r}"\n',
    '    # Git commit/push/merge are normal experiment-branch workflow actions.\n'
    '    # Main/merge/deployment governance is enforced by the lifecycle layer.\n'
    '    for keyword in _REJECT_KEYWORDS:\n'
    '        if keyword in haystack:\n'
    '            return f"goal mentions disallowed keyword: {keyword!r}"\n',
    1,
)
policy = policy.replace(
    '    ("goal requests a disallowed git action", "disallowed_keyword"),\n',
    '',
    1,
)
policy_path.write_text(policy)

tests = tests_legacy.read_text()

# Old tests encoded the previous policy contract. Keep them visible but skipped,
# and add explicit regressions for the new branch-scoped Git workflow contract.
obsolete = [
    'test_policy_violation_persists_matched_policy_rule',
    'test_goal_mentioning_commit_is_rejected',
    'test_rejected_experiment_never_creates_worktree',
    'test_push_and_merge_are_still_rejected',
    'test_git_merge_command_is_blocked',
    'test_commit_changes_is_blocked',
    'test_merge_pull_request_is_blocked',
    'test_merge_pr_is_blocked',
    'test_commit_result_is_blocked',
    'test_git_action_rule_classified_as_disallowed_keyword',
]
for name in obsolete:
    marker = f'    def {name}(self):'
    if marker in tests:
        tests = tests.replace(
            marker,
            '    @unittest.skip("obsolete Git keyword policy removed by issue #190")\n' + marker,
            1,
        )

regression = r'''

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
'''
if 'class GitWorkflowPolicyRegressionTest' not in tests:
    tests += regression

tests_path.write_text(tests)

# Remove temporary wrapper source files after restoring the real files.
policy_legacy.unlink()
tests_legacy.unlink()
