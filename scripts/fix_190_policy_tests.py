from pathlib import Path
import re

path = Path("tests/test_experiment_service.py")
text = path.read_text()


def replace_method(name: str, body: str) -> None:
    global text
    pattern = rf"    def {re.escape(name)}\(self\):.*?(?=\n    (?:@[^\n]+\n    )?def |\n\nclass |\n\nif __name__|\Z)"
    replacement = "    def " + name + "(self):\n" + "\n".join(
        "        " + line if line else "" for line in body.strip().splitlines()
    )
    text, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"could not replace {name}: {count}")


# End-to-end policy-gate tests: these goals must reach the implementation worker.
for name, goal in (
    ("test_goal_with_commit_keyword_rejected_without_llm_call", "Implement and then run git commit and push."),
    ("test_goal_with_pip_install_rejected_without_llm_call", "Run pip install requests then use it."),
    ("test_goal_with_infra_action_rejected_without_llm_call", "Create a new Azure VM for the worker pool."),
):
    replace_method(
        name,
        f'''experiment = self.service.create_experiment(goal={goal!r})
result = self.service.run_experiment(experiment.id)
self.assertNotEqual(result.status, STATUS_REJECTED)
self.implementation_worker.run.assert_called_once()''',
    )

# Keep policy-rule classification coverage, but use a category that remains blocked.
replace_method(
    "test_goal_policy_rule_classified_correctly",
    '''experiment = self.service.create_experiment(
    goal="Run rm -rf on generated files before continuing."
)
result = self.service.run_experiment(experiment.id)
self.assertEqual(result.status, STATUS_REJECTED)
self.assertEqual(result.policy_rule, "disallowed_keyword")
self.implementation_worker.run.assert_not_called()''',
)

# Direct goal-policy checks now document the allowed autonomy contract.
for name, goal in (
    ("test_check_goal_policy_blocks_commit", "implement and commit the change"),
    ("test_check_goal_policy_blocks_infra_action", "create a new Azure VM"),
    ("test_git_commit_command_is_blocked", "implement it and then run git commit"),
    ("test_git_push_command_is_blocked", "after tests pass, run git push"),
    ("test_push_branch_is_blocked", "push the branch to origin"),
    ("test_push_code_is_blocked", "push the code when done"),
):
    replace_method(name, f'''self.assertIsNone(_check_goal_policy({goal!r}))''')

replace_method(
    "test_non_git_safety_checks_remain_active",
    '''# Package management and infrastructure/deployment are intentionally allowed.
self.assertIsNone(_check_goal_policy("Run pip install requests."))
self.assertIsNone(_check_goal_policy("Delete the production server."))
# Credential access and explicitly destructive shell/database actions remain blocked.
self.assertIsNotNone(_check_goal_policy("Read the API key from the environment."))
self.assertIsNotNone(_check_goal_policy("Run rm -rf on generated files."))''',
)

# Rename stale method names so the suite describes the current contract.
renames = {
    "test_goal_with_commit_keyword_rejected_without_llm_call": "test_goal_with_commit_keyword_reaches_llm_call",
    "test_goal_with_pip_install_rejected_without_llm_call": "test_goal_with_pip_install_reaches_llm_call",
    "test_goal_with_infra_action_rejected_without_llm_call": "test_goal_with_infra_action_reaches_llm_call",
    "test_check_goal_policy_blocks_commit": "test_check_goal_policy_allows_commit",
    "test_check_goal_policy_blocks_infra_action": "test_check_goal_policy_allows_infra_action",
    "test_git_commit_command_is_blocked": "test_git_commit_command_is_allowed",
    "test_git_push_command_is_blocked": "test_git_push_command_is_allowed",
    "test_push_branch_is_blocked": "test_push_branch_is_allowed",
    "test_push_code_is_blocked": "test_push_code_is_allowed",
    "test_non_git_safety_checks_remain_active": "test_remaining_safety_checks_remain_active",
}
for old, new in renames.items():
    text = text.replace(f"def {old}(self):", f"def {new}(self):", 1)

path.write_text(text.rstrip() + "\n")
