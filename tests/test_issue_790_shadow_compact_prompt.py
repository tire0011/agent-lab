"""_start_shadow sends the compact prompt and skips over-budget prompts (#790).

Part 1b of #781: the shadow callable gets ``compact_planning_prompt(...)`` plus
the fixed shadow-instruction suffix, capped by ``NEXUS_PLANNER_SHADOW_PROMPT_TOKENS``
(default 1500 tokens, i.e. at most 6000 UTF-8 bytes including the suffix).
When even the trimmed compact prompt does not fit, no model call is made and
one ``planner_shadow`` row with ``error="skipped: prompt_over_budget"`` is
written synchronously. The paid planner prompt stays byte-for-byte what main
renders for the same input.
"""

import hashlib
import os
import sqlite3
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
PAID_MANY = '{"issue": 139, "reason": "paid choice"}'  # newest of many_issues()
PAID_SNAP = '{"issue": 12, "reason": "paid choice"}'  # one of the snapshot issues
HEADER = nexus_planner._COMPACT_HEADER
SHADOW_INSTRUCTIONS = (
    '\n\nLOCAL SHADOW ANSWER: Pick only from OPEN ISSUES. Return exactly a short JSON '
    'object with only {"issue": <number>, "reason": "<short reason>"}.'
)

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


class ShadowCompactPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")
        env = patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("NEXUS_PLANNER_SHADOW_PROMPT_TOKENS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def wait_for_row(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if list_shadow(self.db)["count"]:
                return list_shadow(self.db)["rows"][0]
            time.sleep(0.01)
        self.fail("shadow row was not written")

    def planner(self, shadow, paid=PAID, timeout=1):
        return IssuePlanner(lambda _: paid, shadow=shadow, db_path=self.db,
                            shadow_timeout=timeout)

    def test_shadow_callable_gets_the_compact_prompt_within_the_byte_cap(self):
        suffix_bytes = len(SHADOW_INSTRUCTIONS.encode("utf-8"))
        suffix_tokens = estimate_tokens(SHADOW_INSTRUCTIONS)
        received = []
        issues = many_issues(80)
        for issue in issues:
            issue["title"] = "界" * 14
        outcomes = nine_outcomes()
        cap = 4 * DEFAULT_SHADOW_PROMPT_TOKENS
        unreserved = compact_planning_prompt(issues, outcomes, [], 3)
        self.assertGreater(len(unreserved.encode("utf-8")) + suffix_bytes, cap)

        def fake_shadow(prompt):
            received.append(prompt)
            return '{"issue": 179, "reason": "local"}'

        # A fresh temp db has no health rows, so stuck_measurements is empty.
        choice = self.planner(fake_shadow, paid=PAID_MANY).choose(
            issues, outcomes, budget_left=3, tick_id="tick-bytes")
        self.assertEqual(choice.issue, 139)
        row = self.wait_for_row()  # the shadow runs on a background thread
        self.assertEqual(len(received), 1)
        prompt = received[0]
        # The whole prompt the local model reads stays within 4*1500 bytes.
        self.assertLessEqual(len(prompt.encode("utf-8")), cap)
        # Compact prompt first (fixed header), then the shadow instructions.
        self.assertTrue(prompt.startswith(HEADER))
        self.assertTrue(prompt.endswith(SHADOW_INSTRUCTIONS))
        compact = prompt[: -len(SHADOW_INSTRUCTIONS)]
        self.assertEqual(compact, compact_planning_prompt(
            issues, outcomes, [], 3, DEFAULT_SHADOW_PROMPT_TOKENS - suffix_tokens))
        # No paid-prompt content leaks into the shadow prompt.
        self.assertNotIn("body text", compact)  # no issue bodies
        self.assertNotIn("You are Nexus, an independent agent", prompt)  # no DEFAULT_MISSION
        self.assertNotIn("boom " * 100, prompt)  # outcome errors are cut
        self.assertEqual(row["tick_id"], "tick-bytes")
        self.assertEqual(row["paid_issue"], 139)
        self.assertEqual(row["local_issue"], 179)
        self.assertEqual(row["local_valid"], 1)
        self.assertIsNone(row["error"])
        # prompt_tokens reflects the compact shadow prompt that was sent.
        self.assertEqual(row["prompt_tokens"], estimate_tokens(prompt))
        self.assertLessEqual(row["prompt_tokens"], DEFAULT_SHADOW_PROMPT_TOKENS)

    def test_env_token_budget_shrinks_the_shadow_prompt(self):
        received = []
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "250"}):
            choice = self.planner(
                lambda p: received.append(p) or '{"issue": 139, "reason": "x"}',
                paid=PAID_MANY,
            ).choose(many_issues(), nine_outcomes(), budget_left=3)
        self.assertEqual(choice.issue, 139)
        row = self.wait_for_row()  # the shadow runs on a background thread
        self.assertEqual(len(received), 1)
        self.assertLessEqual(len(received[0].encode("utf-8")), 4 * 250)
        self.assertEqual(row["prompt_tokens"], estimate_tokens(received[0]))
        self.assertIsNone(row["error"])

    def test_tiny_env_budget_skips_the_call_and_writes_the_skip_row_synchronously(self):
        called = []
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "1"}):
            choice = self.planner(lambda p: called.append(p) or "{}").choose(
                ISSUES, tick_id="tick-skip")
        # The paid choice is unaffected and no shadow model call was made.
        self.assertEqual(choice.issue, 2)
        self.assertEqual(called, [])
        # The skip row is written synchronously: it is already there when
        # choose() returns, without waiting for any thread.
        rows = list_shadow(self.db)["rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["tick_id"], "tick-skip")
        self.assertEqual(row["paid_issue"], 2)
        self.assertIsNone(row["local_issue"])
        self.assertEqual(row["local_valid"], 0)
        self.assertEqual(row["error"], "skipped: prompt_over_budget")
        self.assertEqual(row["prompt_tokens"], 0)
        self.assertEqual(row["seconds"], 0.0)

    def test_interrupted_insert_rolls_back_and_replaying_tick_keeps_one_skip(self):
        called = []
        list_shadow(self.db)  # Create the table before adding a failure trigger.
        with sqlite3.connect(self.db) as conn:
            conn.execute("""
                CREATE TRIGGER interrupt_shadow AFTER INSERT ON planner_shadow
                BEGIN SELECT RAISE(ABORT, 'interrupted write'); END
            """)
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "1"}):
            planner = self.planner(lambda p: called.append(p) or "{}")
            with self.assertLogs(nexus_planner.logger, level="WARNING") as logs:
                self.assertEqual(planner.choose(ISSUES, tick_id="replay").issue, 2)
            self.assertIn("interrupted write", logs.output[0])
            self.assertEqual(list_shadow(self.db)["rows"], [])
            with sqlite3.connect(self.db) as conn:
                conn.execute("DROP TRIGGER interrupt_shadow")
            self.assertEqual(planner.choose(ISSUES, tick_id="replay").issue, 2)
            saved = list_shadow(self.db)["rows"]
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["error"], "skipped: prompt_over_budget")
            self.assertEqual(planner.choose(ISSUES, tick_id="replay").issue, 2)
        self.assertEqual(list_shadow(self.db)["rows"], saved)
        self.assertEqual(called, [])

    def test_shadow_worker_exit_records_a_complete_failure_row(self):
        def exiting_shadow(_):
            raise SystemExit("worker interrupted")

        self.assertEqual(self.planner(exiting_shadow).choose(ISSUES, tick_id="exit").issue, 2)
        row = self.wait_for_row()
        self.assertEqual(row["tick_id"], "exit")
        self.assertEqual(row["error"], "SystemExit: worker interrupted")
        self.assertIsNone(row["local_issue"])
        self.assertEqual(row["local_valid"], 0)
        self.assertEqual(list_shadow(self.db)["count"], 1)

    def test_default_budget_never_writes_a_skip_row(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEXUS_PLANNER_SHADOW_PROMPT_TOKENS", None)
            self.planner(lambda _: '{"issue": 1, "reason": "local"}').choose(ISSUES)
        row = self.wait_for_row()
        self.assertNotIn("skipped", row["error"] or "")
        self.assertGreater(row["prompt_tokens"], 0)


class PaidPromptUnchangedTests(unittest.TestCase):
    """The paid planner prompt is byte-for-byte the one main renders (#790)."""

    ISSUES = [
        {"number": 11, "title": "Issue 11 title", "body": "Body of 11 " * 5},
        {"number": 12, "title": "Issue 12 title", "body": "Body of 12 " * 5},
        {"number": 13, "title": "Issue 13 title", "body": "Body of 13 " * 5},
    ]
    RECENT = [
        {"status": "failed", "title": "exp a", "error": "boom"},
        {"status": "succeeded", "title": "exp b", "adoption_status": "adopted",
         "adoption_reason": ""},
    ]
    CONTEXT = dict(
        exhausted=[{"number": 9, "title": "old", "last_reason": "tests failed"}],
        proposals=["p1"], decisions=[{"title": "d", "reason": "no"}], mission=None,
        budget_left=3, reflections=None,
    )

    def test_paid_prompt_snapshot_equals_main(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "agent.db")
        paid_prompts = []
        planner = IssuePlanner(
            lambda p: paid_prompts.append(p) or PAID_SNAP,
            shadow=lambda _: self.fail("over-budget shadow should be skipped"),
            db_path=db,
        )
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW": "1",
                                     "NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "1"}):
            choice = planner.choose(self.ISSUES, self.RECENT, tick_id="tick-snap", **self.CONTEXT)
        self.assertEqual(choice.issue, 12)
        self.assertEqual(len(paid_prompts), 1)
        # A fresh db has no stuck measurements, exactly like the snapshot input.
        expected = planning_prompt(self.ISSUES, self.RECENT,
                                   stuck_measurements=[], **self.CONTEXT)
        self.assertEqual(paid_prompts[0], expected)
        # Digest of the same fixed input rendered by main (verified against
        # git show main:modules/nexus_planner.py).
        self.assertEqual(hashlib.sha256(paid_prompts[0].encode()).hexdigest(),
                         "858b00b3fb3fc2f3472de423c3143dd6da1eab88052ad9e2d69891268866e385")


if __name__ == "__main__":
    unittest.main()
