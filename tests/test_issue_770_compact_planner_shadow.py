"""Compact local planner shadow prompt with a json_schema enum (#770)."""

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import nexus_planner
from modules.nexus_planner import (
    COMPACT_PROMPT_VARIANT,
    IssuePlanner,
    compact_planning_prompt,
    estimate_tokens,
    planning_prompt,
    shadow_response_format,
    shadow_timeout,
)
from modules.planner_shadow import list_shadow, record

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


class CompactPromptTests(unittest.TestCase):
    def test_forty_issues_fit_and_keep_the_newest(self):
        issues = many_issues()
        recent = [{"status": "failed", "title": f"exp {i}", "error": "boom " * 100} for i in range(9)]
        prompt = compact_planning_prompt(issues, recent, ["planner shadow: 36x invalid"], 3)
        self.assertLessEqual(estimate_tokens(prompt), 1500)
        self.assertIn("#139 ", prompt)  # the newest issue is still listed
        self.assertNotIn("body text", prompt)
        self.assertNotIn("x" * 91, prompt)  # titles cut to 90 characters
        self.assertEqual(prompt.count("- failed: exp"), 5)
        self.assertIn("planner shadow: 36x invalid", prompt)
        self.assertIn("Paid starts left today: 3", prompt)

    def test_oldest_issues_are_dropped_first(self):
        prompt = compact_planning_prompt(many_issues(80), None, None, 1)
        self.assertLessEqual(estimate_tokens(prompt), 1500)
        self.assertIn("#179 ", prompt)
        self.assertNotIn("#100 ", prompt)
        listed = [int(line[1:4]) for line in prompt.splitlines() if line.startswith("#1")]
        self.assertEqual(listed, list(range(listed[0], 180)))  # a newest-first contiguous cut

    def test_small_budget_and_tags(self):
        prompt = compact_planning_prompt(
            [*ISSUES, {"number": 3, "title": "three", "attempts_left": 0}], None, None, None)
        self.assertIn("#2 two | attempts left: 1 [blocked-by-review]", prompt)
        self.assertIn("#3 three | attempts left: 0 [out-of-attempts]", prompt)
        self.assertNotIn("a long body", prompt)
        small = compact_planning_prompt(many_issues(), None, None, None, max_tokens=300)
        self.assertLessEqual(estimate_tokens(small), 300)
        self.assertIn("#139 ", small)

    def test_prefix_is_stable_across_ticks(self):
        a = compact_planning_prompt(ISSUES, [{"status": "failed", "title": "a"}], [], 4)
        b = compact_planning_prompt(ISSUES, [{"status": "succeeded", "title": "b"}], ["x"], 2)
        prefix = a[:a.index("RECENT EXPERIMENTS:")]
        self.assertTrue(b.startswith(prefix))
        self.assertTrue(a.startswith(nexus_planner._COMPACT_HEADER))

    def test_schema_enum_is_allowed_set(self):
        fmt = shadow_response_format({5, 2, 9})
        schema = fmt["json_schema"]["schema"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(schema["properties"]["issue"]["enum"], [2, 5, 9])
        self.assertEqual(schema["properties"]["reason"]["maxLength"], 160)
        self.assertFalse(schema["additionalProperties"])

    def test_timeout_from_rates(self):
        self.assertAlmostEqual(shadow_timeout(700, 7.0, 3.0, 60), 100 + 40 + 10)
        self.assertEqual(shadow_timeout(1500, 7.0, 3.0, 60), 240)
        self.assertEqual(shadow_timeout(1500, None, 3.0, 60), 60)
        self.assertEqual(shadow_timeout(1500, None, None, 999), 240)

    def test_full_paid_prompt_unchanged(self):
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
        # Digest of the same input rendered by the code before #770.
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(),
                         "6f09d47c989545f8fb60b1113d5cc942e80a4551ef9aecbd0b63e21b39363e3c")
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

    def test_compact_prompt_schema_and_row(self):
        calls = []

        def shadow(prompt, *, response_format, timeout):
            calls.append((prompt, response_format, timeout))
            return {"content": '{"issue": 1, "reason": "local"}', "tokens_cached": 321}

        paid_prompts = []
        planner = IssuePlanner(lambda p: paid_prompts.append(p) or PAID, shadow=shadow,
                               db_path=self.db, shadow_rates=lambda: (1000.0, 100.0))
        self.assertEqual(planner.choose(ISSUES, tick_id="t1", budget_left=2).issue, 2)
        row = self.wait_for_row()
        prompt, fmt, timeout = calls[0]
        self.assertTrue(prompt.startswith(nexus_planner._COMPACT_HEADER))
        self.assertNotIn("a long body", prompt)
        self.assertIn("a long body", paid_prompts[0])
        self.assertEqual(fmt["json_schema"]["schema"]["properties"]["issue"]["enum"], [1, 2])
        tokens = estimate_tokens(prompt)
        self.assertAlmostEqual(timeout, tokens / 1000 + 120 / 100 + 10)
        self.assertEqual(row["prompt_tokens"], tokens)
        self.assertEqual((row["local_issue"], row["local_valid"]), (1, 1))
        self.assertEqual(row["prompt_variant"], COMPACT_PROMPT_VARIANT)
        self.assertEqual(row["prompt_variant"], "compact_v1")
        self.assertEqual(row["cached_prompt_tokens"], 321)

    def test_disallowed_answer_is_blocked_by_schema_and_still_recorded(self):
        formats = []

        def shadow(prompt, *, response_format, timeout):
            formats.append(response_format)
            return json.dumps({"issue": 99, "reason": "not in the list"})

        IssuePlanner(lambda _: PAID, shadow=shadow, db_path=self.db).choose(ISSUES)
        row = self.wait_for_row()
        self.assertNotIn(99, formats[0]["json_schema"]["schema"]["properties"]["issue"]["enum"])
        self.assertEqual((row["local_issue"], row["local_valid"]), (99, 0))
        self.assertEqual(row["error"], "issue is not allowed")
        self.assertEqual(row["prompt_variant"], "compact_v1")
        self.assertIsNone(row["cached_prompt_tokens"])

    def test_disabled_shadow_makes_no_call(self):
        called = []
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW": "0"}):
            planner = IssuePlanner(
                lambda _: PAID, db_path=self.db,
                shadow=lambda p, **kw: called.append(p) or "{}")
            self.assertEqual(planner.choose(ISSUES).issue, 2)
        time.sleep(.03)
        self.assertEqual(called, [])

    def test_env_prompt_budget(self):
        prompts = []
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "300"}):
            IssuePlanner(lambda _: '{"issue": 139, "reason": "paid"}', db_path=self.db,
                         shadow=lambda p: prompts.append(p) or '{"issue": 139, "reason": "x"}',
                         ).choose(many_issues())
            row = self.wait_for_row()
        self.assertLessEqual(estimate_tokens(prompts[0]), 300)
        self.assertEqual(row["local_valid"], 1)

    def test_old_table_gets_new_nullable_columns(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "CREATE TABLE planner_shadow (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "tick_id TEXT NOT NULL, paid_issue INTEGER NOT NULL, local_issue INTEGER, "
                "local_valid INTEGER NOT NULL, prompt_tokens INTEGER NOT NULL, "
                "seconds REAL NOT NULL, error TEXT)")
            conn.execute("INSERT INTO planner_shadow (tick_id, paid_issue, local_valid, "
                         "prompt_tokens, seconds) VALUES ('old', 1, 0, 8000, 55.1)")
        self.assertIsNone(list_shadow(self.db)["rows"][0]["prompt_variant"])
        record(self.db, tick_id="new", paid_issue=1, local_issue=1, local_valid=1,
               prompt_tokens=900, seconds=30.0, error=None,
               prompt_variant="compact_v1", cached_prompt_tokens=800)
        row = list_shadow(self.db)["rows"][0]
        self.assertEqual((row["prompt_variant"], row["cached_prompt_tokens"]), ("compact_v1", 800))


if __name__ == "__main__":
    unittest.main()
