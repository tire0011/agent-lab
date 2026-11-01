"""The planner sees its own open proposals, so it does not suggest them again."""

import unittest

from modules.nexus_planner import planning_prompt
from tests.test_nexus_initiative import InitiativeFixture


class PlannerSeesOpenProposalsTest(unittest.TestCase):
    def test_prompt_lists_open_proposals(self):
        prompt = planning_prompt(
            [{"number": 1, "title": "Something", "body": ""}],
            proposals=["Retry merges current main into the issue branch"],
        )
        self.assertIn("YOUR OPEN PROPOSALS", prompt)
        self.assertIn("Retry merges current main into the issue branch", prompt)

    def test_no_section_without_proposals(self):
        self.assertNotIn("YOUR OPEN PROPOSALS", planning_prompt([{"number": 1, "title": "x", "body": ""}]))

    def test_initiative_passes_proposal_titles_to_the_planner(self):
        seen = {}

        class Planner:
            def choose(self, issues, recent=None, exhausted=None, proposals=None):
                seen["proposals"] = proposals
                return None

        fx = InitiativeFixture()
        issues = [
            {"number": 1, "title": "Work", "body": ""},
            {"number": 2, "title": "My idea", "body": "", "labels": [{"name": "proposal"}]},
        ]
        svc = fx.service(issue_lister=lambda: issues, planner=Planner())
        self.addCleanup(svc.close)
        svc.tick()
        self.assertEqual(seen["proposals"], ["My idea"])


if __name__ == "__main__":
    unittest.main()
