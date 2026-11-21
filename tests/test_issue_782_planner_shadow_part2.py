"""Planner shadow part 2: json_schema enum, rate-based timeout, prompt_variant (#782).

``_start_shadow`` sends the compact prompt (#790) together with a
``json_schema_response_format`` whose ``issue`` property is an enum of exactly
the allowed issue numbers and whose ``reason`` is a string of at most 160
characters, so a conforming server cannot answer with a disallowed number.
The call timeout is ``prompt_tokens / prompt_tps + 120 / gen_tps + 10 s`` from
the LocalModelRates medians, capped at 240 s, with a 120 s fallback when there
are no samples; the call still runs on the daemon thread. Every shadow row
stores ``prompt_variant='compact_v1'`` and, when the shadow callable reports
them, the cached prompt tokens in the new nullable ``cached_prompt_tokens``
column (migrated like ``created_at``).

All tests use a fake shadow callable and a temporary SQLite database.
"""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import nexus_planner
from modules.nexus_planner import (
    IssuePlanner,
    SHADOW_PROMPT_VARIANT,
    SHADOW_REASON_MAX_LENGTH,
    SHADOW_TIMEOUT_CAP_SECONDS,
    SHADOW_TIMEOUT_FALLBACK_SECONDS,
    shadow_response_format,
    shadow_timeout_seconds,
)
from modules.planner_shadow import list_shadow, record

# Deliberately not in sorted order: the enum must be the sorted allowed set.
ISSUES = [
    {"number": 7, "title": "seven", "body": "", "attempts_left": 2},
    {"number": 3, "title": "three", "body": "", "attempts_left": 1},
    {"number": 5, "title": "five", "body": "", "attempts_left": 3, "review_blocked": True},
]
ALLOWED = {3, 5, 7}
PAID = '{"issue": 5, "reason": "paid choice"}'


class FakeRates:
    """A LocalModelRates-like object with fixed medians."""

    def __init__(self, prompt_tps, gen_tps):
        self.values = (prompt_tps, gen_tps)

    def medians(self):
        return self.values


class ShadowPart2Tests(unittest.TestCase):
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

    def planner(self, shadow, **kwargs):
        kwargs.setdefault("shadow_timeout", 1)
        return IssuePlanner(lambda _: PAID, shadow=shadow, db_path=self.db, **kwargs)

    def test_schema_enum_is_exactly_the_allowed_set(self):
        calls = []

        def fake_shadow(prompt, response_format):
            calls.append((prompt, response_format))
            return '{"issue": 3, "reason": "local pick"}'

        choice = self.planner(fake_shadow).choose(ISSUES, tick_id="tick-enum")
        self.assertEqual(choice.issue, 5)  # the paid choice is untouched
        row = self.wait_for_row()
        self.assertEqual(len(calls), 1)
        prompt, response_format = calls[0]
        self.assertEqual(response_format["type"], "json_schema")
        json_schema = response_format["json_schema"]
        self.assertEqual(json_schema["name"], "planner_shadow")
        self.assertTrue(json_schema["strict"])
        schema = json_schema["schema"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"]["issue"],
                         {"type": "integer", "enum": [3, 5, 7]})
        self.assertEqual(schema["properties"]["issue"]["enum"], sorted(ALLOWED))
        self.assertEqual(schema["properties"]["reason"],
                         {"type": "string", "maxLength": SHADOW_REASON_MAX_LENGTH})
        self.assertEqual(SHADOW_REASON_MAX_LENGTH, 160)
        self.assertEqual(schema["required"], ["issue", "reason"])
        self.assertFalse(schema["additionalProperties"])
        # The shadow got the compact prompt, not the paid one.
        self.assertTrue(prompt.startswith(nexus_planner._COMPACT_HEADER))
        self.assertNotIn("independent agent that develops itself", prompt)
        # The valid local answer was recorded.
        self.assertEqual((row["local_issue"], row["local_valid"], row["error"]),
                         (3, 1, None))

    def test_shadow_response_format_helper_builds_the_enum(self):
        response_format = shadow_response_format({10, 2, 33})
        schema = response_format["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["issue"]["enum"], [2, 10, 33])

    def test_disallowed_answer_from_the_fake_is_still_recorded_as_not_allowed(self):
        # The schema cannot be produced by a conforming server, but the fake
        # returns a disallowed number anyway: it must still be recorded as
        # invalid with the "issue is not allowed" error.
        def fake_shadow(prompt, response_format):
            return '{"issue": 99, "reason": "not on the list"}'

        self.planner(fake_shadow).choose(ISSUES, tick_id="tick-99")
        row = self.wait_for_row()
        self.assertEqual(row["local_issue"], 99)
        self.assertEqual(row["local_valid"], 0)
        self.assertEqual(row["error"], "issue is not allowed")
        self.assertEqual(row["prompt_variant"], SHADOW_PROMPT_VARIANT)
        self.assertEqual(SHADOW_PROMPT_VARIANT, "compact_v1")

    def test_row_stores_prompt_variant_compact_v1(self):
        self.planner(lambda _p, _rf: '{"issue": 7, "reason": "local"}').choose(
            ISSUES, tick_id="tick-variant")
        row = self.wait_for_row()
        self.assertEqual(row["prompt_variant"], "compact_v1")
        self.assertEqual(row["paid_issue"], 5)
        self.assertEqual(row["local_issue"], 7)
        self.assertEqual(row["local_valid"], 1)
        self.assertIsNone(row["error"])
        # A plain string answer carries no cache information.
        self.assertIsNone(row["cached_prompt_tokens"])

    def test_cached_prompt_tokens_from_the_shadow_answer_are_stored(self):
        def fake_shadow(prompt, response_format):
            return {"content": '{"issue": 3, "reason": "cached prefix reused"}',
                    "cached_prompt_tokens": 812}

        self.planner(fake_shadow).choose(ISSUES, tick_id="tick-cached")
        row = self.wait_for_row()
        self.assertEqual(row["cached_prompt_tokens"], 812)
        self.assertEqual(row["local_issue"], 3)
        self.assertEqual(row["local_valid"], 1)
        self.assertEqual(row["prompt_variant"], "compact_v1")

    def test_over_budget_skip_row_also_stores_the_variant(self):
        called = []
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "1"}):
            choice = self.planner(lambda p, _rf: called.append(p) or "{}").choose(
                ISSUES, tick_id="tick-skip")
        self.assertEqual(choice.issue, 5)
        self.assertEqual(called, [])
        rows = list_shadow(self.db)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error"], "skipped: prompt_over_budget")
        self.assertEqual(rows[0]["prompt_variant"], "compact_v1")
        self.assertIsNone(rows[0]["cached_prompt_tokens"])

    def test_zero_env_disables_the_shadow_call(self):
        called = []
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW": "0"}):
            choice = self.planner(
                lambda p, _rf: called.append((p, _rf)) or "{}").choose(ISSUES)
        self.assertEqual(choice.issue, 5)
        time.sleep(0.03)
        self.assertEqual(called, [])
        with sqlite3.connect(self.db) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='planner_shadow'"
            ).fetchone()
        self.assertIsNone(table)


class ShadowTimeoutTests(unittest.TestCase):
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

    def test_fallback_120s_without_any_samples(self):
        self.assertEqual(shadow_timeout_seconds(1500, FakeRates(None, None)),
                         SHADOW_TIMEOUT_FALLBACK_SECONDS)
        self.assertEqual(SHADOW_TIMEOUT_FALLBACK_SECONDS, 120.0)
        # Only one rate known: still no full estimate, still the fallback.
        self.assertEqual(shadow_timeout_seconds(1500, FakeRates(7.0, None)), 120.0)
        self.assertEqual(shadow_timeout_seconds(1500, FakeRates(None, 15.0)), 120.0)
        # Broken rates store: fallback, never an exception.
        class Broken:
            def medians(self):
                raise RuntimeError("corrupt")
        self.assertEqual(shadow_timeout_seconds(1500, Broken()), 120.0)

    def test_timeout_is_computed_from_the_medians(self):
        # 1000 / 10 + 120 / 20 + 10 = 116 s
        self.assertAlmostEqual(shadow_timeout_seconds(1000, FakeRates(10.0, 20.0)), 116.0)
        # 400 / 8 + 120 / 40 + 10 = 63 s
        self.assertAlmostEqual(shadow_timeout_seconds(400, FakeRates(8.0, 40.0)), 63.0)

    def test_timeout_is_capped_at_240s(self):
        self.assertEqual(SHADOW_TIMEOUT_CAP_SECONDS, 240.0)
        # 10**9 / 1 + 120 / 1 + 10 would be ~10**9 s: capped.
        self.assertEqual(shadow_timeout_seconds(10 ** 9, FakeRates(1.0, 1.0)), 240.0)
        # Just below the cap is kept as computed.
        self.assertAlmostEqual(shadow_timeout_seconds(2000, FakeRates(10.0, 100.0)),
                               200.0 + 1.2 + 10.0)

    def test_start_shadow_uses_the_rate_based_timeout_on_the_daemon_thread(self):
        seen = []

        def fake_timeout(prompt_tokens, rates):
            seen.append((prompt_tokens, rates))
            return 0.05  # tiny, so the test does not wait

        def slow_shadow(prompt, response_format):
            time.sleep(1)
            return "{}"

        rates = FakeRates(10.0, 20.0)
        planner = IssuePlanner(lambda _: PAID, shadow=slow_shadow, db_path=self.db,
                               rates=rates)
        with patch.object(nexus_planner, "shadow_timeout_seconds", side_effect=fake_timeout):
            started = time.monotonic()
            choice = planner.choose(ISSUES, tick_id="tick-timeout")
        # The tick never waits on the shadow: it stays on the daemon thread.
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(choice.issue, 5)
        row = self.wait_for_row()
        # The monitor joined with the computed timeout and recorded it.
        self.assertEqual(row["error"], "timeout after 0.05s")
        self.assertEqual(len(seen), 1)
        prompt_tokens, used_rates = seen[0]
        self.assertGreater(prompt_tokens, 0)
        self.assertIs(used_rates, rates)  # the planner's rates object was used
        self.assertEqual(row["prompt_variant"], "compact_v1")

    def test_constructor_cap_still_limits_the_computed_timeout(self):
        def slow_shadow(prompt, response_format):
            time.sleep(1)
            return "{}"

        # Huge rates give ~10 s; the constructor cap brings it to 0.05 s.
        planner = IssuePlanner(lambda _: PAID, shadow=slow_shadow, db_path=self.db,
                               shadow_timeout=0.05, rates=FakeRates(1e9, 1e9))
        planner.choose(ISSUES, tick_id="tick-cap")
        row = self.wait_for_row()
        self.assertEqual(row["error"], "timeout after 0.05s")


class PlannerShadowMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_table_is_migrated_like_created_at(self):
        # A table from before #782 (and before created_at): no created_at,
        # no prompt_variant, no cached_prompt_tokens.
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """CREATE TABLE planner_shadow (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       tick_id TEXT NOT NULL,
                       paid_issue INTEGER NOT NULL,
                       local_issue INTEGER,
                       local_valid INTEGER NOT NULL CHECK (local_valid IN (0, 1)),
                       prompt_tokens INTEGER NOT NULL,
                       seconds REAL NOT NULL,
                       error TEXT)"""
            )
            conn.execute(
                "INSERT INTO planner_shadow (tick_id, paid_issue, local_issue, local_valid,"
                " prompt_tokens, seconds, error) VALUES ('old', 1, 1, 1, 10, 0.5, NULL)"
            )
        record(self.db, tick_id="new", paid_issue=2, local_issue=2, local_valid=1,
               prompt_tokens=100, seconds=1.0, error=None,
               prompt_variant="compact_v1", cached_prompt_tokens=700)
        with sqlite3.connect(self.db) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
        self.assertIn("created_at", columns)
        self.assertIn("prompt_variant", columns)
        self.assertIn("cached_prompt_tokens", columns)
        rows = {row["tick_id"]: row for row in list_shadow(self.db, limit=10)["rows"]}
        self.assertEqual(rows["new"]["prompt_variant"], "compact_v1")
        self.assertEqual(rows["new"]["cached_prompt_tokens"], 700)
        # Old rows keep NULL in the migrated columns.
        self.assertIsNone(rows["old"]["prompt_variant"])
        self.assertIsNone(rows["old"]["cached_prompt_tokens"])

    def test_record_defaults_the_new_columns_to_null(self):
        record(self.db, tick_id="t", paid_issue=1, local_issue=None, local_valid=0,
               prompt_tokens=0, seconds=0.0, error="x")
        row = list_shadow(self.db)["rows"][0]
        self.assertIsNone(row["prompt_variant"])
        self.assertIsNone(row["cached_prompt_tokens"])


if __name__ == "__main__":
    unittest.main()
