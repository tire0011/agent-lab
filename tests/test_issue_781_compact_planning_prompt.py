"""Compact planner shadow prompt with a hard token budget (#781, part 1 of #770).

``compact_planning_prompt`` never exceeds ``max_tokens`` (bytes/4) and returns
``None`` when even the fixed header with one issue does not fit; ``_start_shadow``
then records ``skipped: prompt_over_budget`` without calling the model.
"""

import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import nexus_planner
from modules.nexus_planner import (
    DEFAULT_SHADOW_PROMPT_TOKENS,
    IssuePlanner,
    compact_planning_prompt,
    estimate_tokens,
    planning_prompt,
)
from modules.planner_shadow import list_shadow

PAID = '{"issue": 2, "reason": "paid choice"}'
ISSUES = [
    {"number": 1, "title": "one", "body": "a long body " * 50, "attempts_left": 2},
    {"number": 2, "title": "two", "body": "", "attempts_left": 1, "review_blocked": True},
]


def many_issues(count=40):
    return [
        {"number": 100 + n, "title": f"Issue {100 + n}: " + "x" * 200,
         "body": "body text " * 300, "attempts_left": 2}
        for n in range(count)
    ]


def nine_outcomes():
    return [{"status": "failed", "title": f"exp {i}", "error": "boom " * 100}
            for i in range(9)]


class CompactPlanningPromptTests(unittest.TestCase):
    def test_forty_issues_stay_under_1500_and_keep_the_newest(self):
        prompt = compact_planning_prompt(many_issues(), nine_outcomes(),
                                         ["planner shadow: 36x invalid"], 3)
        self.assertIsNotNone(prompt)
        self.assertLessEqual(estimate_tokens(prompt), DEFAULT_SHADOW_PROMPT_TOKENS)
        self.assertLessEqual(estimate_tokens(prompt), 1500)
        self.assertIn("#139 ", prompt)  # the newest issue is still listed
        self.assertIn("#100 ", prompt)  # nothing had to be dropped at this size
        self.assertNotIn("body text", prompt)  # no bodies in the compact prompt
        self.assertNotIn("x" * 91, prompt)  # titles cut to 90 characters
        self.assertEqual(prompt.count("- failed: exp"), 5)  # the last five outcomes
        self.assertIn("planner shadow: 36x invalid", prompt)
        self.assertEqual(prompt.splitlines()[-1], "Paid starts left today: 3")  # budget last

    def test_tiny_budgets_never_return_an_over_budget_prompt(self):
        for budget in (1, 10, 50):
            with self.subTest(budget=budget):
                prompt = compact_planning_prompt(many_issues(), nine_outcomes(),
                                                 ["stuck line"], 3, budget)
                # The fixed header alone is over these budgets: None, never over.
                self.assertIsNone(prompt)
                self.assertGreater(estimate_tokens(nexus_planner._COMPACT_HEADER), budget)

    def test_header_is_byte_stable_across_calls_with_different_issues(self):
        a = compact_planning_prompt(ISSUES, [{"status": "failed", "title": "a"}], [], 4)
        b = compact_planning_prompt(many_issues(40), nine_outcomes(), ["x"], 2)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        header = nexus_planner._COMPACT_HEADER
        self.assertEqual(a[:len(header)], header)
        self.assertEqual(b[:len(header)], header)
        self.assertEqual(a[:len(header)], b[:len(header)])  # byte-stable
        self.assertEqual(a.split("OPEN ISSUES:")[0], header)
        self.assertEqual(b.split("OPEN ISSUES:")[0], header)

    def test_trimming_order_outcomes_then_oldest_issues_then_short_titles(self):
        stuck = ["planner shadow: 36x invalid"]
        # Outcomes go first: all 40 issues are kept while outcomes are dropped.
        prompt = compact_planning_prompt(many_issues(), nine_outcomes(), stuck, 3, 1320)
        self.assertLessEqual(estimate_tokens(prompt), 1320)
        issue_lines = [l for l in prompt.splitlines() if l.startswith("#")]
        self.assertEqual(len(issue_lines), 40)
        self.assertLess(prompt.count("- failed: exp"), 5)  # oldest outcomes dropped
        # Then the oldest issues: outcomes are already gone when issues go.
        prompt = compact_planning_prompt(many_issues(), nine_outcomes(), stuck, 3, 1280)
        self.assertLessEqual(estimate_tokens(prompt), 1280)
        self.assertEqual(prompt.count("- failed: exp"), 0)
        self.assertNotIn("#100 ", prompt)  # oldest issue dropped ...
        self.assertIn("#139 ", prompt)  # ... the newest always stays
        # Then the titles cut to 40 characters.
        prompt = compact_planning_prompt(many_issues(), nine_outcomes(), stuck, 3, 175)
        self.assertIsNotNone(prompt)
        self.assertLessEqual(estimate_tokens(prompt), 175)
        self.assertNotIn("x" * 41, prompt)
        self.assertIn("#139 ", prompt)

    def test_issue_line_has_number_attempts_and_tags(self):
        prompt = compact_planning_prompt(
            [*ISSUES, {"number": 3, "title": "three", "attempts_left": 0}], None, None, None)
        self.assertIn("#1 one | attempts left: 2", prompt)
        self.assertIn("#2 two | attempts left: 1 [blocked-by-review]", prompt)
        self.assertIn("#3 three | attempts left: 0 [out-of-attempts]", prompt)
        self.assertNotIn("a long body", prompt)
        self.assertEqual(prompt.splitlines()[-1], "Paid starts left today: unknown")

    def test_full_paid_prompt_is_unchanged_byte_for_byte(self):
        issues = [{"number": n, "title": f"Issue {n} title", "body": f"Body of {n} " * 5}
                  for n in (11, 12, 13)]
        recent = [{"status": "failed", "title": "exp a", "error": "boom"},
                  {"status": "succeeded", "title": "exp b", "adoption_status": "adopted",
                   "adoption_reason": ""}]
        context = dict(
            exhausted=[{"number": 9, "title": "old", "last_reason": "tests failed"}],
            proposals=["p1"], decisions=[{"title": "d", "reason": "no"}], mission=None,
            budget_left=3, reflections=None, stuck_measurements=["planner shadow: 36x invalid"])
        prompt = planning_prompt(issues, recent, **context)
        # Digest of the same input rendered by the code before #781 (main).
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(),
                         "6f09d47c989545f8fb60b1113d5cc942e80a4551ef9aecbd0b63e21b39363e3c")
        explore = planning_prompt(issues, recent, explore=True, mission="Custom mission.",
                                  **{k: v for k, v in context.items() if k != "mission"})
        self.assertEqual(hashlib.sha256(explore.encode()).hexdigest(),
                         "4a68fd52b79a96d89f3bf2a77c5b5700dd23d2694c1e916f5caf9673894cd791")
        # The compact-only keys never leak into the paid prompt.
        tagged = [{**i, "attempts_left": 1, "review_blocked": True} for i in issues]
        self.assertEqual(planning_prompt(tagged, recent, **context), prompt)


class CompactShadowCallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def wait_for_row(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if list_shadow(self.db)["count"]:
                return list_shadow(self.db)["rows"][0]
            time.sleep(0.01)
        self.fail("shadow row was not written")

    def test_shadow_gets_the_compact_prompt_within_the_env_budget(self):
        calls = []
        paid_prompts = []

        def shadow(prompt):
            calls.append(prompt)
            return '{"issue": 1, "reason": "local"}'

        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "300"}):
            planner = IssuePlanner(lambda p: paid_prompts.append(p) or PAID,
                                   shadow=shadow, db_path=self.db)
            self.assertEqual(planner.choose(ISSUES, recent=nine_outcomes(),
                                            tick_id="t-300", budget_left=2).issue, 2)
            row = self.wait_for_row()
        self.assertEqual(len(calls), 1)
        prompt = calls[0]
        self.assertTrue(prompt.startswith(nexus_planner._COMPACT_HEADER))
        self.assertLessEqual(estimate_tokens(prompt), 300)
        self.assertNotIn("body text", prompt)  # compact, not the paid prompt
        self.assertIn("a long body", paid_prompts[0])  # the paid prompt is untouched
        self.assertEqual(row["prompt_tokens"], estimate_tokens(prompt))
        self.assertEqual((row["local_issue"], row["local_valid"]), (1, 1))
        self.assertIsNone(row["error"])

    def test_over_budget_prompt_is_skipped_and_recorded_without_a_model_call(self):
        calls = []
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "50"}):
            planner = IssuePlanner(lambda _: PAID,
                                   shadow=lambda p: calls.append(p) or "{}",
                                   db_path=self.db)
            # The skip is recorded synchronously, before choose returns.
            self.assertEqual(planner.choose(ISSUES, tick_id="t-skip").issue, 2)
            row = list_shadow(self.db)["rows"][0]
        self.assertEqual(calls, [])  # the model was never called
        self.assertEqual(row["tick_id"], "t-skip")
        self.assertEqual(row["paid_issue"], 2)
        self.assertIsNone(row["local_issue"])
        self.assertEqual(row["local_valid"], 0)
        self.assertEqual(row["error"], "skipped: prompt_over_budget")


if __name__ == "__main__":
    unittest.main()
