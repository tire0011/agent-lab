"""Compact local planner shadow prompt with a json_schema enum (#770)."""

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from modules import nexus_planner
from modules.nexus_planner import (
    COMPACT_PROMPT_VARIANT,
    IssuePlanner,
    ShadowPromptBudgetError,
    compact_planning_prompt,
    compact_shortlist,
    estimate_tokens,
    planning_prompt,
    shadow_response_format,
    shadow_timeout,
    validate_shadow_answer,
    wait_for_shadow,
)
from modules.planner_shadow import (
    INTERRUPTED_ERROR,
    begin,
    finish,
    finish_stale,
    list_shadow,
    record,
)

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


def listed_numbers(prompt):
    return [int(line[1:].split()[0]) for line in prompt.splitlines()
            if line.startswith("#") and line[1:2].isdigit()]


class StrictBudgetTests(unittest.TestCase):
    """Review point 1: the bound holds for every accepted budget."""

    def test_every_budget_fits_or_is_rejected(self):
        issues = many_issues(40)
        recent = [{"status": "failed", "title": "r" * 300} for _ in range(9)]
        stuck = ["s" * 400 for _ in range(6)]
        header = estimate_tokens(nexus_planner._COMPACT_HEADER)
        rejected = fitted = 0
        for budget in range(1, 1600, 7):
            try:
                result = compact_shortlist(issues, recent, stuck, 3, max_tokens=budget)
            except ShadowPromptBudgetError as exc:
                rejected += 1
                self.assertGreater(exc.needed, budget)
                continue
            fitted += 1
            self.assertLessEqual(estimate_tokens(result.prompt), budget)
            self.assertEqual(result.tokens, estimate_tokens(result.prompt))
            self.assertTrue(result.selectable)
            self.assertIn(139, result.listed)  # the newest issue survives every trim
        self.assertGreater(rejected, 0)
        self.assertGreater(fitted, 0)
        with self.assertRaises(ShadowPromptBudgetError):
            compact_shortlist(issues, None, None, 1, max_tokens=header - 1)

    def test_context_is_trimmed_when_one_issue_is_left(self):
        recent = [{"status": "failed", "title": f"r{i} " + "y" * 150} for i in range(5)]
        stuck = ["planner shadow: " + "z" * 150 for _ in range(4)]
        loose = compact_shortlist(ISSUES, recent, stuck, 1, max_tokens=5000)
        tight = compact_shortlist(ISSUES, recent, stuck, 1, max_tokens=loose.tokens - 60)
        self.assertLessEqual(tight.tokens, loose.tokens - 60)
        self.assertEqual(listed_numbers(tight.prompt), [2])  # one issue, the newest
        self.assertIn("- failed: r0", tight.prompt)  # the newest outcome stays longest
        self.assertNotIn("- failed: r4", tight.prompt)

    def test_nothing_selectable_is_rejected(self):
        with self.assertRaises(ShadowPromptBudgetError):
            compact_shortlist([], None, None, 1, exhausted=[{"number": 5, "title": "x"}])

    def test_impossible_budget_records_a_row_and_never_calls_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "agent.db")
            called = []
            with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "5"}):
                choice = IssuePlanner(lambda _: PAID, db_path=db,
                                      shadow=lambda p: called.append(p) or "{}").choose(ISSUES)
            self.assertEqual(choice.issue, 2)
            self.assertEqual(wait_for_shadow(2), 0)
            row = list_shadow(db)["rows"][0]
            self.assertEqual(called, [])
            self.assertIn("prompt budget too small", row["error"])
            self.assertEqual((row["local_valid"], row["prompt_variant"]), (0, "compact_v1"))


class ShortlistEnumTests(unittest.TestCase):
    """Review points 2 and 3: one set for prompt, enum and validation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_shadow(self, issues, answer, **choose):
        calls = []

        def shadow(prompt, *, response_format, timeout):
            calls.append((prompt, response_format))
            return answer

        paid = json.dumps({"issue": int(issues[-1]["number"]), "reason": "paid"})
        IssuePlanner(lambda _: paid, shadow=shadow, db_path=self.db).choose(issues, **choose)
        self.assertEqual(wait_for_shadow(5), 0)
        return calls[0], list_shadow(self.db)["rows"][0]

    def test_enum_is_the_retained_shortlist(self):
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "400"}):
            (prompt, fmt), row = self.run_shadow(many_issues(40), '{"issue": 100, "reason": "old"}')
        enum = fmt["json_schema"]["schema"]["properties"]["issue"]["enum"]
        self.assertEqual(enum, listed_numbers(prompt))
        self.assertNotIn(100, enum)  # dropped from the prompt, so not choosable
        # 100 is eligible for the paid planner but was never shown: not allowed.
        self.assertEqual((row["local_issue"], row["local_valid"]), (100, 0))
        self.assertEqual(row["error"], "issue is not allowed")

    def test_enum_is_intersected_with_the_eligible_set(self):
        result = compact_shortlist(many_issues(5), None, None, 1, eligible={101, 103, 999})
        self.assertEqual(result.selectable, frozenset({101, 103}))
        self.assertEqual(result.listed, (100, 101, 102, 103, 104))

    def test_exhausted_issues_are_context_but_never_choosable(self):
        exhausted = [{"number": 5, "title": "stuck one", "last_reason": "tests failed"}]
        (prompt, fmt), row = self.run_shadow(ISSUES, '{"issue": 5, "reason": "x"}',
                                             exhausted=exhausted)
        self.assertIn("#5 stuck one | attempts left: 0 [out-of-attempts]", prompt)
        self.assertNotIn("tests failed", prompt)
        self.assertEqual(fmt["json_schema"]["schema"]["properties"]["issue"]["enum"], [1, 2])
        self.assertEqual((row["local_issue"], row["error"]), (5, "issue is not allowed"))

    def test_exhausted_context_is_dropped_before_the_last_choosable_issue(self):
        exhausted = [{"number": 900 + i, "title": "e" * 90} for i in range(30)]
        result = compact_shortlist([{"number": 1, "title": "only"}], None, None, 1,
                                   max_tokens=300, exhausted=exhausted)
        self.assertEqual(result.selectable, frozenset({1}))
        self.assertLessEqual(result.tokens, 300)
        self.assertIn(929, result.listed)  # the newest context line is kept longest

    def test_live_candidate_path_passes_exhausted_issues(self):
        from modules import nexus_initiative

        source = Path(nexus_initiative.__file__).read_text()
        planned = source[source.index("def _planned_issue"):]
        planned = planned[:planned.index("\n    def ")]
        self.assertIn("exhausted=self._exhausted_issues", planned)


class AnswerValidationTests(unittest.TestCase):
    """Review point 5: exact shape and types, safe metadata."""

    def check(self, answer, expected):
        self.assertEqual(validate_shadow_answer(answer, {1, 2}), expected)

    def test_shapes(self):
        self.check('{"issue": 1, "reason": "ok"}', (1, True, None))
        self.check('```json\n{"issue": 2, "reason": "ok"}\n```', (2, True, None))
        self.check('{"issue": 7, "reason": "ok"}', (7, False, "issue is not allowed"))
        self.check('{"issue": -1, "reason": "ok"}', (-1, False, "issue is not allowed"))
        self.check('{"issue": true, "reason": "ok"}', (None, False, "invalid issue"))
        self.check('{"issue": 1.0, "reason": "ok"}', (None, False, "invalid issue"))
        self.check('{"issue": 1.5, "reason": "ok"}', (None, False, "invalid issue"))
        self.check('{"issue": "1", "reason": "ok"}', (None, False, "invalid issue"))
        self.check('{"issue": NaN, "reason": "ok"}', (None, False, "invalid issue"))
        self.check('{"issue": 1e400, "reason": "ok"}', (None, False, "invalid issue"))
        self.check('{"issue": %d, "reason": "ok"}' % 10 ** 30, (None, False, "invalid issue"))
        self.check('{"issue": 1}', (1, False, "invalid reason"))
        self.check('{"issue": 1, "reason": "  "}', (1, False, "invalid reason"))
        self.check('{"issue": 1, "reason": 5}', (1, False, "invalid reason"))
        self.check(json.dumps({"issue": 1, "reason": "r" * 161}), (1, False, "reason too long"))
        self.check(json.dumps({"issue": 1, "reason": "r" * 160}), (1, True, None))
        self.check('{"issue": 1, "reason": "ok", "x": 1}', (1, False, "unexpected fields"))
        self.check("not json", (None, False, "invalid JSON"))
        self.check("[1, 2]", (None, False, "invalid JSON"))
        self.check(None, (None, False, "invalid JSON"))
        self.check(5, (None, False, "invalid JSON"))

    def test_malformed_answers_and_metadata_are_still_recorded(self):
        ok = '{"issue": 1, "reason": "ok"}'
        answers = [
            {"content": ok, "tokens_cached": float("inf")},
            {"content": ok, "tokens_cached": float("nan")},
            {"content": ok, "tokens_cached": 1e30},
            {"content": ok, "tokens_cached": True},
            {"content": ok, "tokens_cached": -3},
            {"content": None, "tokens_cached": "12"},
            {"content": '{"issue": 99999999999999999999999, "reason": "big"}'},
            {"content": ok, "tokens_cached": 12.0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "agent.db")
            for answer in answers:
                IssuePlanner(lambda _: PAID, db_path=db,
                             shadow=lambda p, a=answer: a).choose(ISSUES)
                self.assertEqual(wait_for_shadow(5), 0)
            rows = list(reversed(list_shadow(db)["rows"]))
        self.assertEqual(len(rows), len(answers))
        self.assertEqual([r["cached_prompt_tokens"] for r in rows],
                         [None, None, None, None, None, None, None, 12])
        self.assertEqual([r["local_valid"] for r in rows], [1, 1, 1, 1, 1, 0, 0, 1])
        self.assertEqual(rows[6]["error"], "invalid issue")

    def test_bad_rates_use_the_fallback(self):
        timeouts = []

        def shadow(prompt, *, response_format, timeout):
            timeouts.append(timeout)
            return '{"issue": 1, "reason": "ok"}'

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "agent.db")
            for rates in ((float("nan"), 3.0), (float("inf"), float("inf")), "broken", (True, 2)):
                IssuePlanner(lambda _: PAID, db_path=db, shadow=shadow, shadow_timeout=33,
                             shadow_rates=lambda r=rates: r).choose(ISSUES)
                self.assertEqual(wait_for_shadow(5), 0)
        self.assertEqual(timeouts, [33, 33, 33, 33])


class LifetimeTests(unittest.TestCase):
    """Review point 4 and interruption: every attempt ends with an outcome."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tick_exit_waits_for_the_shadow_but_the_choice_does_not(self):
        import nexus_tick

        release, calling = threading.Event(), threading.Event()

        def slow(prompt):
            calling.set()
            release.wait(2)
            return '{"issue": 1, "reason": "late"}'

        started = time.monotonic()
        choice = IssuePlanner(lambda _: PAID, shadow=slow, db_path=self.db).choose(ISSUES)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(choice.issue, 2)
        self.assertTrue(calling.wait(2))
        summary = list_shadow(self.db)["summary"]
        self.assertEqual((summary["pending"], summary["total"]), (1, 0))
        threading.Timer(0.2, release.set).start()
        nexus_tick._wait_for_planner_shadow()
        payload = list_shadow(self.db)
        self.assertEqual(payload["summary"]["pending"], 0)
        self.assertEqual((payload["rows"][0]["local_issue"], payload["rows"][0]["local_valid"]),
                         (1, 1))

    def test_wait_gives_up_after_its_bound(self):
        release = threading.Event()
        IssuePlanner(lambda _: PAID, db_path=self.db,
                     shadow=lambda p: release.wait(5) and "{}").choose(ISSUES)
        try:
            self.assertEqual(wait_for_shadow(0.05), 1)
        finally:
            release.set()
            wait_for_shadow(5)

    def test_tick_main_waits_for_the_shadow_in_finally(self):
        import nexus_tick

        source = Path(nexus_tick.__file__).read_text()
        main = source[source.index("def main("):]
        tail = main[main.index("    finally:"):]
        self.assertLess(tail.index("_wait_for_planner_shadow()"),
                        tail.index("_shadow_replay(args.repo_root"))

    def test_killed_process_leaves_a_pending_row_that_is_finished_as_interrupted(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        stale = begin(self.db, tick_id="dead", paid_issue=1, prompt_tokens=10,
                      prompt_variant="compact_v1", created_at=old)
        fresh = begin(self.db, tick_id="alive", paid_issue=1, prompt_tokens=10,
                      prompt_variant="compact_v1")
        with sqlite3.connect(self.db) as conn:
            rows = dict(conn.execute("SELECT id, error FROM planner_shadow").fetchall())
        self.assertEqual(rows[stale], INTERRUPTED_ERROR)  # the next begin finished it
        self.assertIsNone(rows[fresh])  # still within its timeout: left alone
        self.assertEqual(list_shadow(self.db)["summary"]["pending"], 1)
        # The dead process's late finish must not overwrite the interrupted outcome.
        self.assertFalse(finish(self.db, stale, local_issue=1, local_valid=1, seconds=1.0,
                                error=None))
        self.assertEqual(finish_stale(self.db, older_than=0), 1)
        self.assertEqual(finish_stale(self.db, older_than=0), 0)  # running it twice: no-op
        self.assertEqual(list_shadow(self.db)["summary"]["pending"], 0)

    def test_finish_runs_once(self):
        row_id = begin(self.db, tick_id="t", paid_issue=2, prompt_tokens=10,
                       prompt_variant="compact_v1")
        self.assertTrue(finish(self.db, row_id, local_issue=1, local_valid=1, seconds=1.0))
        self.assertFalse(finish(self.db, row_id, local_issue=2, local_valid=0, seconds=9.0,
                                error="second"))
        row = list_shadow(self.db)["rows"][0]
        self.assertEqual((row["local_issue"], row["error"]), (1, None))
        with self.assertRaises(ValueError):
            finish(self.db, row_id, tick_id="rewrite")

    def test_locked_database_does_not_delay_the_choice(self):
        release = threading.Event()

        def slow_begin(*args, **kwargs):
            release.wait(2)
            raise sqlite3.OperationalError("database is locked")

        with patch("modules.planner_shadow.begin", side_effect=slow_begin):
            started = time.monotonic()
            choice = IssuePlanner(lambda _: PAID, db_path=self.db,
                                  shadow=lambda p: '{"issue": 1, "reason": "ok"}').choose(ISSUES)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(choice.issue, 2)
            release.set()
            self.assertEqual(wait_for_shadow(5), 0)
        self.assertEqual(list_shadow(self.db)["rows"][0]["local_valid"], 1)

    def test_failed_pending_insert_still_records_the_outcome(self):
        with patch("modules.planner_shadow.begin", side_effect=sqlite3.OperationalError("locked")):
            IssuePlanner(lambda _: PAID, db_path=self.db,
                         shadow=lambda p: '{"issue": 1, "reason": "ok"}').choose(ISSUES)
            self.assertEqual(wait_for_shadow(5), 0)
        row = list_shadow(self.db)["rows"][0]
        self.assertEqual((row["local_valid"], row["prompt_variant"]), (1, "compact_v1"))

    def test_shadow_thread_that_cannot_start_finishes_its_row(self):
        real = threading.Thread

        class NoStart(real):
            def start(self):
                if self.name == nexus_planner.SHADOW_MONITOR_THREAD:
                    raise RuntimeError("can't start new thread")
                super().start()

        with patch.object(nexus_planner.threading, "Thread", NoStart):
            choice = IssuePlanner(lambda _: PAID, db_path=self.db,
                                  shadow=lambda p: "{}").choose(ISSUES)
        self.assertEqual(choice.issue, 2)
        payload = list_shadow(self.db)
        self.assertEqual(payload["summary"]["pending"], 0)
        self.assertIn("can't start new thread", payload["rows"][0]["error"])

    def test_two_ticks_write_two_rows(self):
        planner = IssuePlanner(lambda _: PAID, db_path=self.db,
                               shadow=lambda p: '{"issue": 2, "reason": "same"}')
        planner.choose(ISSUES, tick_id="a")
        planner.choose(ISSUES, tick_id="b")
        self.assertEqual(wait_for_shadow(5), 0)
        payload = list_shadow(self.db)
        self.assertEqual(sorted(r["tick_id"] for r in payload["rows"]), ["a", "b"])
        self.assertEqual(payload["summary"]["agreement"], 1.0)

    def test_pending_rows_are_not_counted_by_measurement_health(self):
        from modules.measurement_health import check_pipelines

        now = datetime.now(timezone.utc)
        for i in range(5):
            begin(self.db, tick_id=str(i), paid_issue=1, prompt_tokens=10,
                  prompt_variant="compact_v1")
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW": "1"}), \
                sqlite3.connect(self.db) as conn:
            result = check_pipelines(conn, now + timedelta(seconds=1))["planner_shadow"]
        self.assertTrue(result["enabled"])
        self.assertNotEqual(result["status"], "stuck")


class MigrationAndRolloutTests(unittest.TestCase):
    """Review points 6 and 7."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def legacy_table(self, db):
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE planner_shadow (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "tick_id TEXT NOT NULL, paid_issue INTEGER NOT NULL, local_issue INTEGER, "
                "local_valid INTEGER NOT NULL, prompt_tokens INTEGER NOT NULL, "
                "seconds REAL NOT NULL, error TEXT)")
            conn.execute("INSERT INTO planner_shadow (tick_id, paid_issue, local_valid, "
                         "prompt_tokens, seconds) VALUES ('old', 1, 0, 8000, 55.1)")

    def test_concurrent_upgrade_of_a_legacy_database(self):
        for attempt in range(5):
            db = str(Path(self.tmp.name) / f"legacy{attempt}.db")
            self.legacy_table(db)
            barrier = threading.Barrier(12)
            errors = []

            def worker(i):
                try:
                    barrier.wait()
                    if i % 2:
                        list_shadow(db)
                    else:
                        record(db, tick_id=f"w{i}", paid_issue=1, local_issue=1,
                               local_valid=1, prompt_tokens=1, seconds=1.0, error=None,
                               prompt_variant="compact_v1")
                except Exception as exc:  # noqa: BLE001 - collected for the assertion
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            with sqlite3.connect(db) as conn:
                columns = [row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")]
                count = conn.execute("SELECT COUNT(*) FROM planner_shadow").fetchone()[0]
            for name in ("created_at", "prompt_variant", "cached_prompt_tokens", "status"):
                self.assertEqual(columns.count(name), 1)
            self.assertEqual(count, 7)

    def test_rollout_covers_only_the_latest_twenty_compact_rows(self):
        for i in range(36):  # the old full-prompt timeouts
            record(self.db, tick_id=f"old{i}", paid_issue=1, local_issue=None, local_valid=0,
                   prompt_tokens=8000, seconds=55.0, error="timeout")
        for i in range(5):  # older compact rows outside the window: all invalid
            record(self.db, tick_id=f"c{i}", paid_issue=1, local_issue=None, local_valid=0,
                   prompt_tokens=900, seconds=200.0, error="invalid JSON",
                   prompt_variant="compact_v1")
        for i in range(20):
            record(self.db, tick_id=f"n{i}", paid_issue=1, local_issue=1 if i < 14 else 2,
                   local_valid=1 if i < 18 else 0, prompt_tokens=900, seconds=40.0 + i,
                   error=None, prompt_variant="compact_v1")
        begin(self.db, tick_id="pending", paid_issue=1, prompt_tokens=900,
              prompt_variant="compact_v1")
        payload = list_shadow(self.db, limit=5)
        rollout = payload["rollout"]
        self.assertEqual(payload["count"], 5)
        self.assertEqual(payload["summary"]["total"], 61)
        self.assertEqual((rollout["prompt_variant"], rollout["window"], rollout["total"]),
                         ("compact_v1", 20, 20))
        self.assertEqual(rollout["valid_share"], 0.9)
        self.assertEqual(rollout["median_seconds"], 49.5)
        self.assertAlmostEqual(rollout["agreement"], 14 / 18)
        self.assertTrue(rollout["window_complete"])
        self.assertTrue(rollout["meets_targets"])
        self.assertTrue(rollout["followup_ready"])

    def test_rollout_is_incomplete_before_twenty_rows(self):
        record(self.db, tick_id="n", paid_issue=1, local_issue=1, local_valid=1,
               prompt_tokens=900, seconds=10.0, error=None, prompt_variant="compact_v1")
        rollout = list_shadow(self.db)["rollout"]
        self.assertEqual(rollout["total"], 1)
        self.assertFalse(rollout["window_complete"])
        self.assertFalse(rollout["meets_targets"])
        self.assertFalse(rollout["followup_ready"])


if __name__ == "__main__":
    unittest.main()
