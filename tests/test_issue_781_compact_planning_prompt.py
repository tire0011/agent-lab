"""Compact planner shadow prompt with a hard byte cap (#789, part 1a of #781).

``estimate_tokens`` is ceil(utf-8 bytes / 4). ``compact_planning_prompt`` is a
pure function: its result never has more than ``4 * max_tokens`` utf-8 bytes,
and it returns ``None`` when the prompt cannot fit. It is not wired into the
planner yet.
"""

import hashlib
import unittest

from modules import nexus_planner
from modules.nexus_planner import (
    DEFAULT_SHADOW_PROMPT_TOKENS,
    compact_planning_prompt,
    estimate_tokens,
    planning_prompt,
)

HEADER = nexus_planner._COMPACT_HEADER
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


def multibyte_issues(count):
    """Issues with 2-byte (ä) and 4-byte (emoji) characters in long titles."""
    return [
        {"number": 500 + n, "title": f"Prüfe Änderung {n} 🚀 " + "äö🙂" * 40,
         "body": "Körper " * 50, "attempts_left": n % 3, "review_blocked": n % 2 == 0}
        for n in range(count)
    ]


def multibyte_outcomes():
    return [{"status": "failed", "title": f"Lauf {i} 💥 " + "ü" * 200} for i in range(7)]


class EstimateTokensTests(unittest.TestCase):
    def test_rounds_utf8_bytes_up(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("a"), 1)
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcde"), 2)
        self.assertEqual(estimate_tokens("ä"), 1)  # 2 bytes
        self.assertEqual(estimate_tokens("ää🙂"), 2)  # 2 + 2 + 4 = 8 bytes
        self.assertEqual(estimate_tokens("ää🙂a"), 3)  # 9 bytes


class ByteCapTests(unittest.TestCase):
    def test_result_is_none_or_within_the_byte_cap(self):
        for max_tokens in (1, 10, 50, 1500):
            for count in (0, 1, 40):
                with self.subTest(max_tokens=max_tokens, issues=count):
                    prompt = compact_planning_prompt(
                        multibyte_issues(count), multibyte_outcomes(),
                        ["stuck ✋ " + "ß" * 300], 3, max_tokens)
                    if prompt is not None:
                        self.assertLessEqual(len(prompt.encode("utf-8")), 4 * max_tokens)
                        self.assertLessEqual(estimate_tokens(prompt), max_tokens)
                        self.assertTrue(prompt.startswith(HEADER))
                    if max_tokens == 1500:
                        self.assertIsNotNone(prompt)
                    if 4 * max_tokens < len(HEADER.encode("utf-8")):
                        self.assertIsNone(prompt)  # even the header does not fit

    def _exact_size(self):
        """A prompt and the max_tokens for which it is exactly 4*max_tokens bytes."""
        issues = [{"number": 7, "title": "Größe", "attempts_left": 2}]
        base = compact_planning_prompt(issues, None, None, 1, 10_000)
        size = len(base.encode("utf-8"))
        # Pad the title so the prompt lands on a multiple of 4 bytes.
        pad = (-size) % 4
        issues = [{"number": 7, "title": "Größe" + "x" * pad, "attempts_left": 2}]
        prompt = compact_planning_prompt(issues, None, None, 1, 10_000)
        self.assertEqual(len(prompt.encode("utf-8")) % 4, 0)
        return issues, prompt, len(prompt.encode("utf-8")) // 4

    def test_exactly_four_times_max_tokens_bytes_is_accepted(self):
        issues, full, max_tokens = self._exact_size()
        self.assertEqual(compact_planning_prompt(issues, None, None, 1, max_tokens), full)

    def test_one_byte_over_the_cap_is_not_accepted(self):
        issues, full, max_tokens = self._exact_size()
        # One more ASCII byte in the title: 4*max_tokens + 1 bytes. Rounding the
        # token count down would have let this pass; the title cut to 40
        # characters is the last resort, so short titles leave only None.
        longer = [{**issues[0], "title": issues[0]["title"] + "y"}]
        self.assertLess(len(longer[0]["title"]), 40)
        untrimmed = compact_planning_prompt(longer, None, None, 1, 10_000)
        self.assertEqual(len(untrimmed.encode("utf-8")), 4 * max_tokens + 1)
        self.assertIsNone(compact_planning_prompt(longer, None, None, 1, max_tokens))
        # A multi-byte character that crosses the cap is not accepted either.
        crossing = [{**issues[0], "title": issues[0]["title"] + "ä"}]
        self.assertIsNone(compact_planning_prompt(crossing, None, None, 1, max_tokens))

    def test_forty_multibyte_issues_stay_under_1500_and_keep_the_newest(self):
        prompt = compact_planning_prompt(multibyte_issues(40), multibyte_outcomes(),
                                         ["stuck"], 3, 1500)
        self.assertIsNotNone(prompt)
        self.assertLessEqual(len(prompt.encode("utf-8")), 4 * 1500)
        self.assertIn("#539 ", prompt)  # the newest issue is still listed
        self.assertEqual(prompt.splitlines()[-1], "Paid starts left today: 3")

    def test_header_is_byte_stable_with_multibyte_issues(self):
        a = compact_planning_prompt(multibyte_issues(1), None, None, 0, 1500)
        b = compact_planning_prompt(multibyte_issues(40), multibyte_outcomes(), ["x"], 9, 1500)
        header = HEADER.encode("utf-8")
        self.assertEqual(a.encode("utf-8")[:len(header)], header)
        self.assertEqual(b.encode("utf-8")[:len(header)], header)


class NotWiredYetTests(unittest.TestCase):
    def test_shadow_call_still_uses_the_full_prompt(self):
        import inspect

        source = inspect.getsource(nexus_planner.IssuePlanner._start_shadow)
        self.assertNotIn("compact_planning_prompt", source)


if __name__ == "__main__":
    unittest.main()
