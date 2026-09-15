from pathlib import Path
import re

policy_path = Path("modules/experiment_service/policy.py")
tests_path = Path("tests/test_experiment_service.py")

policy = policy_path.read_text()

# Package management is part of Nexus VM self-maintenance.
for keyword_line in (
    '    "pip install", "npm install", "apt install", "apt-get install",\n',
    '    "package install", "install package",\n',
):
    policy = policy.replace(keyword_line, "", 1)

# Infrastructure/deployment is governed by explicit task/lifecycle constraints,
# not by generic text-policy rejection. Remove both plan- and goal-level checks.
pattern = re.compile(
    r"\n    for regex in _INFRA_ACTION_REGEXES:\n"
    r"        for match in regex\.finditer\(haystack\):\n"
    r"            preceding = haystack\[max\(0, match\.start\(\) - 200\):match\.start\(\)\]\n"
    r"            if _infra_action_match_is_false_positive\(match\.group\(0\), preceding\):\n"
    r"                continue\n"
    r"            return \(\n"
    r"                \"(?:plan|goal) describes an actual infrastructure/deployment action \"\n"
    r"                f\"with external effect: \{match\.group\(0\)!r\}\"\n"
    r"            \)\n"
)
policy, removed = pattern.subn("\n", policy)
assert removed == 2, removed

for line in (
    '    ("plan describes an actual infrastructure/deployment action", "infra_action"),\n',
    '    ("goal describes an actual infrastructure/deployment action", "infra_action"),\n',
):
    policy = policy.replace(line, "", 1)

policy_path.write_text(policy.rstrip() + "\n")

tests = tests_path.read_text()

# These tests encoded the old broad deny-policy contract. Keep history visible,
# but do not assert behavior that Nexus intentionally no longer has.
obsolete = [
    "test_goal_mentioning_package_install_is_rejected",
    "test_goal_creating_vm_is_rejected",
    "test_goal_deleting_vm_is_rejected",
    "test_goal_deploying_to_production_is_rejected",
    "test_creating_vm_is_rejected",
    "test_deleting_vm_is_rejected",
    "test_deploying_to_production_is_rejected",
    "test_cloud_resource_change_is_rejected",
]
for name in obsolete:
    marker = f"    def {name}(self):"
    if marker in tests and f'@unittest.skip("obsolete broad action policy removed by issue #190")\n{marker}' not in tests:
        tests = tests.replace(
            marker,
            '    @unittest.skip("obsolete broad action policy removed by issue #190")\n' + marker,
            1,
        )

regression = r'''

class AutonomousVmMaintenancePolicyRegressionTest(unittest.TestCase):
    def test_package_management_is_allowed(self):
        for goal in (
            "Run apt update and apt upgrade on the Nexus Ubuntu VM.",
            "Run apt install powershell and verify pwsh --version.",
            "Install the required package with apt-get install.",
            "Use pip install for the task dependency.",
        ):
            with self.subTest(goal=goal):
                self.assertIsNone(_check_goal_policy(goal))

    def test_infrastructure_and_deployment_actions_are_allowed(self):
        for goal in (
            "Restart the nginx service after updating its configuration.",
            "Configure the Nexus VM for the new worker.",
            "Deploy the build to the test environment.",
            "Resize the Azure VM when the task explicitly requires it.",
        ):
            with self.subTest(goal=goal):
                self.assertIsNone(_check_goal_policy(goal))

    def test_package_and_infrastructure_actions_are_allowed_in_plan(self):
        data = json.loads(VALID_PLAN_JSON)
        data["expected_changes"] = (
            "Install PowerShell with apt, update Ubuntu packages, configure nginx, "
            "restart the service, and deploy the build to the test environment."
        )
        raw = json.dumps(data)
        plan = parse_plan(raw)
        self.assertIsNotNone(plan)
        self.assertIsNone(plan_violates_policy(plan, raw))

    def test_remaining_secret_and_destructive_checks_still_apply(self):
        self.assertIsNotNone(_check_goal_policy("Read the API key from the environment."))
        self.assertIsNotNone(_check_goal_policy("Run rm -rf on generated files."))
        self.assertIsNotNone(_check_goal_policy("Drop table users from the database."))
'''
if "class AutonomousVmMaintenancePolicyRegressionTest" not in tests:
    tests += regression

tests_path.write_text(tests.rstrip() + "\n")
