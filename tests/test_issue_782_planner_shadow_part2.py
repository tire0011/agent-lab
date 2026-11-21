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
        deadline = time.monotonic() + 30
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

        def fake_shadow(prompt, response_format, deadline):
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
        def fake_shadow(prompt, response_format, deadline):
            return '{"issue": 99, "reason": "not on the list"}'

        self.planner(fake_shadow).choose(ISSUES, tick_id="tick-99")
        row = self.wait_for_row()
        self.assertEqual(row["local_issue"], 99)
        self.assertEqual(row["local_valid"], 0)
        self.assertEqual(row["error"], "issue is not allowed")
        self.assertEqual(row["prompt_variant"], SHADOW_PROMPT_VARIANT)
        self.assertEqual(SHADOW_PROMPT_VARIANT, "compact_v1")

    def test_row_stores_prompt_variant_compact_v1(self):
        self.planner(lambda _p, _rf, _t: '{"issue": 7, "reason": "local"}').choose(
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
        def fake_shadow(prompt, response_format, deadline):
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
            choice = self.planner(lambda p, _rf, _t: called.append(p) or "{}").choose(
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
                lambda p, _rf, _t: called.append((p, _rf)) or "{}").choose(ISSUES)
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
        deadline = time.monotonic() + 30
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

        import threading

        release = threading.Event()

        def slow_shadow(prompt, response_format, deadline):
            # Blocked until the shared deadline, then gives up like the
            # production call (which cancels its request at the deadline).
            release.wait(max(0.0, deadline - time.monotonic()) + 0.01)
            raise TimeoutError("deadline")

        rates = FakeRates(10.0, 20.0)
        planner = IssuePlanner(lambda _: PAID, shadow=slow_shadow, db_path=self.db,
                               rates=rates)
        try:
            with patch.object(nexus_planner, "shadow_timeout_seconds",
                              side_effect=fake_timeout):
                choice = planner.choose(ISSUES, tick_id="tick-timeout")
                # The tick never waits on the shadow: choose() returned while
                # the shadow call is still blocked on the daemon thread.
                self.assertFalse(release.is_set())
                self.assertEqual(choice.issue, 5)
                row = self.wait_for_row()
        finally:
            release.set()
        # The monitor joined with the computed timeout and recorded it.
        self.assertEqual(row["error"], "timeout after 0.05s")
        self.assertEqual(len(seen), 1)
        prompt_tokens, used_rates = seen[0]
        self.assertGreater(prompt_tokens, 0)
        self.assertIs(used_rates, rates)  # the planner's rates object was used
        self.assertEqual(row["prompt_variant"], "compact_v1")

    def test_constructor_cap_still_limits_the_computed_timeout(self):
        import threading

        release = threading.Event()

        def slow_shadow(prompt, response_format, deadline):
            release.wait(max(0.0, deadline - time.monotonic()) + 0.01)
            raise TimeoutError("deadline")

        # Huge rates give ~10 s; the constructor cap brings it to 0.05 s.
        planner = IssuePlanner(lambda _: PAID, shadow=slow_shadow, db_path=self.db,
                               shadow_timeout=0.05, rates=FakeRates(1e9, 1e9))
        try:
            planner.choose(ISSUES, tick_id="tick-cap")
            row = self.wait_for_row()
        finally:
            release.set()
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


LEGACY_TABLE = """CREATE TABLE planner_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id TEXT NOT NULL,
    paid_issue INTEGER NOT NULL,
    local_issue INTEGER,
    local_valid INTEGER NOT NULL CHECK (local_valid IN (0, 1)),
    prompt_tokens INTEGER NOT NULL,
    seconds REAL NOT NULL,
    error TEXT)"""


class _ShadowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")
        env = patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("NEXUS_PLANNER_SHADOW_PROMPT_TOKENS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def wait_for_row(self, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if list_shadow(self.db)["count"]:
                return list_shadow(self.db)["rows"][0]
            time.sleep(0.01)
        self.fail("shadow row was not written")


class ShadowDeadlineTests(_ShadowCase):
    """Blocker 1: the computed budget reaches the callable as a deadline."""

    def test_callable_receives_the_computed_budget(self):
        seen = []

        def fake_shadow(prompt, response_format, deadline):
            seen.append(deadline - time.monotonic())
            return '{"issue": 3, "reason": "ok"}'

        IssuePlanner(lambda _: PAID, shadow=fake_shadow, db_path=self.db,
                     shadow_timeout=None, rates=FakeRates(1e9, 20.0)).choose(
            ISSUES, tick_id="tick-budget")
        self.wait_for_row()
        # prompt part ~0, 120 / 20 = 6, slack 10.
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], 16.0, delta=1.0)

    def test_callable_budget_is_the_capped_value(self):
        seen = []
        IssuePlanner(lambda _: PAID,
                     shadow=lambda p, rf, d: (seen.append(d - time.monotonic())
                                              or '{"issue": 3, "reason": "x"}'),
                     db_path=self.db, shadow_timeout=None,
                     rates=FakeRates(0.001, 0.001)).choose(ISSUES, tick_id="tick-capped")
        self.wait_for_row()
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], 240.0, delta=1.0)

    def _patched_client(self, handler):
        import httpx
        from modules import llama_cpp_client

        created = []

        class Client(llama_cpp_client.LlamaCppClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)
                created.append(self)

        return patch.object(llama_cpp_client, "LlamaCppClient", Client), created

    def test_production_shadow_cancels_the_request_at_the_deadline(self):
        import asyncio
        import nexus_tick

        state = {"cancelled": False, "finished": False}

        async def handler(request):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            state["finished"] = True
            raise AssertionError("request outlived the deadline")

        patcher, created = self._patched_client(handler)
        with patcher, self.assertRaises(TimeoutError):
            nexus_tick._local_shadow("prompt", shadow_response_format(ALLOWED),
                                     time.monotonic() + 0.2)
        # The request was cancelled (connection released), not left running;
        # without the deadline the handler would have finished its 30 s sleep.
        self.assertTrue(state["cancelled"])
        self.assertFalse(state["finished"])
        self.assertGreater(created[0].timeout, 0.0)
        self.assertLessEqual(created[0].timeout, 0.2)

    def test_production_shadow_returns_content_and_cached_tokens(self):
        import json as json_module
        import httpx
        import nexus_tick

        bodies = []

        def handler(request):
            bodies.append(json_module.loads(request.content))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": '{"issue": 3, "reason": "r"}'}}],
                "timings": {"prompt_n": 10, "prompt_ms": 100.0, "predicted_n": 5,
                            "predicted_ms": 50.0, "tokens_cached": 42},
            })

        patcher, created = self._patched_client(handler)
        response_format = shadow_response_format(ALLOWED)
        with patcher:
            result = nexus_tick._local_shadow("prompt", response_format, time.monotonic() + 30.0)
        self.assertEqual(result["content"], '{"issue": 3, "reason": "r"}')
        self.assertEqual(bodies[0]["response_format"], response_format)
        self.assertEqual(bodies[0]["max_tokens"], 120)
        self.assertAlmostEqual(created[0].timeout, 30.0, delta=1.0)
        if result["cached_prompt_tokens"] is not None:
            self.assertEqual(result["cached_prompt_tokens"], 42)

    def test_production_shadow_with_a_broken_budget_uses_the_fallback(self):
        import httpx
        import nexus_tick

        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

        for broken in (float("nan"), float("inf"), float("-inf"), None, "x"):
            patcher, created = self._patched_client(handler)
            with patcher:
                nexus_tick._local_shadow("prompt", None, broken)
            self.assertAlmostEqual(created[0].timeout, SHADOW_TIMEOUT_FALLBACK_SECONDS,
                                   delta=1.0, msg=broken)

    def test_planner_wiring_uses_the_deadline_shadow(self):
        import nexus_tick

        planner = nexus_tick._planner(self.tmp.name, self.db)
        self.assertIs(planner._shadow, nexus_tick._local_shadow)


class ShadowRatesOffTickTests(_ShadowCase):
    """Blocker 2: rates are resolved on the daemon thread with a fallback."""

    def test_slow_rates_read_never_stalls_the_paid_choice(self):
        import threading

        entered = threading.Event()
        release = threading.Event()

        class SlowRates:
            # Blocks until the test releases it: if choose() read the rates
            # on the paid path, it could never return while this is held.
            def medians(self):
                entered.set()
                release.wait(30)
                return 10.0, 20.0

        try:
            choice = IssuePlanner(
                lambda _: PAID, shadow=lambda p, rf, t: '{"issue": 3, "reason": "x"}',
                db_path=self.db, rates=SlowRates()).choose(ISSUES, tick_id="tick-slow-rates")
            # The paid choice came back while the rates read is still blocked.
            self.assertFalse(release.is_set())
            self.assertEqual(choice.issue, 5)
            self.assertTrue(entered.wait(30), "rates were never read on the daemon")
            self.assertEqual(list_shadow(self.db)["count"], 0)
        finally:
            release.set()
        row = self.wait_for_row()
        self.assertEqual(row["local_valid"], 1)

    def test_rates_store_construction_failure_uses_the_fallback(self):
        seen = []
        with patch.object(nexus_planner, "_default_shadow_rates",
                          side_effect=RuntimeError("store broken")) as store:
            choice = IssuePlanner(
                lambda _: PAID,
                shadow=lambda p, rf, d: (seen.append(d - time.monotonic())
                                         or '{"issue": 7, "reason": "x"}'),
                db_path=self.db, shadow_timeout=None).choose(ISSUES, tick_id="tick-store")
            self.assertEqual(choice.issue, 5)
            row = self.wait_for_row()
        self.assertTrue(store.called)
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], SHADOW_TIMEOUT_FALLBACK_SECONDS, delta=1.0)
        self.assertEqual((row["local_issue"], row["local_valid"]), (7, 1))

    def test_default_store_is_built_on_the_daemon_thread(self):
        import threading

        threads = []

        def build():
            threads.append(threading.current_thread().name)
            return FakeRates(None, None)

        with patch.object(nexus_planner, "_default_shadow_rates", side_effect=build):
            IssuePlanner(lambda _: PAID, shadow=lambda p, rf, t: "{}",
                         db_path=self.db).choose(ISSUES, tick_id="tick-thread")
            self.wait_for_row()
        self.assertEqual(threads, ["planner-shadow"])

    def test_rates_returning_garbage_use_the_fallback(self):
        class Garbage:
            def medians(self):
                return "fast"

        seen = []
        IssuePlanner(lambda _: PAID,
                     shadow=lambda p, rf, d: seen.append(d - time.monotonic()) or "{}",
                     db_path=self.db, shadow_timeout=None, rates=Garbage()).choose(
            ISSUES, tick_id="tick-garbage")
        self.wait_for_row()
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], SHADOW_TIMEOUT_FALLBACK_SECONDS, delta=1.0)


class ShadowMigrationRaceTests(unittest.TestCase):
    """Blocker 3: list_shadow migrates under the writer lock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")
        with sqlite3.connect(self.db) as conn:
            conn.execute(LEGACY_TABLE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_waits_for_a_migrating_writer_instead_of_adding_columns_twice(self):
        import threading

        writer = sqlite3.connect(self.db, timeout=30, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        outcome = {}

        def reader():
            try:
                outcome["result"] = list_shadow(self.db)
            except BaseException as exc:  # noqa: BLE001
                outcome["error"] = exc

        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(0.2)
        # The reader saw the old schema but must wait for the lock ...
        self.assertTrue(thread.is_alive())
        # ... while the other connection migrates and commits.
        for name, declaration in (("created_at", "TEXT"), ("prompt_variant", "TEXT"),
                                  ("cached_prompt_tokens", "INTEGER")):
            writer.execute(f"ALTER TABLE planner_shadow ADD COLUMN {name} {declaration}")
        writer.execute("COMMIT")
        writer.close()
        thread.join(10)
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["count"], 0)

    def test_concurrent_lists_and_records_on_a_legacy_table(self):
        import threading

        barrier = threading.Barrier(8)
        errors = []

        def worker(index):
            try:
                barrier.wait()
                if index % 2:
                    list_shadow(self.db)
                else:
                    record(self.db, tick_id=f"t{index}", paid_issue=1, local_issue=1,
                           local_valid=1, prompt_tokens=1, seconds=0.1, error=None,
                           prompt_variant="compact_v1")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(list_shadow(self.db)["summary"]["total"], 4)

    def test_current_schema_list_takes_no_write_lock(self):
        list_shadow(self.db)  # migrates once
        from modules import planner_shadow

        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        real_connect = sqlite3.connect

        def no_wait_connect(*args, **kwargs):
            # busy timeout 0: any write-lock attempt fails at once with
            # "database is locked" instead of waiting - no wall clock needed.
            kwargs["timeout"] = 0
            return real_connect(*args, **kwargs)

        try:
            with patch.object(planner_shadow.sqlite3, "connect", side_effect=no_wait_connect):
                self.assertEqual(list_shadow(self.db)["count"], 0)
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    def test_failed_migration_rolls_back_and_a_retry_succeeds(self):
        from modules import planner_shadow

        def broken(conn):
            conn.execute("ALTER TABLE planner_shadow ADD COLUMN created_at TEXT")
            raise RuntimeError("crash mid-migration")

        with patch.object(planner_shadow, "ensure_table", side_effect=broken):
            with self.assertRaises(RuntimeError):
                list_shadow(self.db)
        with sqlite3.connect(self.db) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
        self.assertNotIn("created_at", columns)  # rolled back, lock released
        self.assertEqual(list_shadow(self.db)["count"], 0)
        list_shadow(self.db)  # running it twice is harmless


class ShadowAnswerValidationTests(_ShadowCase):
    """Blocker 4: answers are validated against the schema, nothing coerced."""

    def test_validate_shadow_answer(self):
        validate = nexus_planner.validate_shadow_answer
        long_reason = "x" * (SHADOW_REASON_MAX_LENGTH + 1)
        cases = [
            ('{"issue": 3, "reason": "ok"}', (3, True, None)),
            ('{"issue": 3, "reason": "%s"}' % ("y" * SHADOW_REASON_MAX_LENGTH), (3, True, None)),
            ('{"issue": 3.9, "reason": "ok"}', (None, False, "invalid issue")),
            ('{"issue": 3.0, "reason": "ok"}', (None, False, "invalid issue")),
            ('{"issue": true, "reason": "ok"}', (None, False, "invalid issue")),
            ('{"issue": "3", "reason": "ok"}', (None, False, "invalid issue")),
            ('{"issue": NaN, "reason": "ok"}', (None, False, "invalid issue")),
            ('{"issue": Infinity, "reason": "ok"}', (None, False, "invalid issue")),
            ('{"issue": -Infinity, "reason": "ok"}', (None, False, "invalid issue")),
            ('{"issue": 1e400, "reason": "ok"}', (None, False, "invalid issue")),
            ('{"reason": "ok"}', (None, False, "invalid issue")),
            ('{"issue": 99, "reason": "ok"}', (99, False, "issue is not allowed")),
            ('{"issue": -1}', (-1, False, "issue is not allowed")),
            ('{"issue": %d, "reason": "ok"}' % 10 ** 30, (None, False, "issue is not allowed")),
            ('{"issue": 3}', (3, False, "invalid reason")),
            ('{"issue": 3, "reason": 7}', (3, False, "invalid reason")),
            ('{"issue": 3, "reason": "%s"}' % long_reason, (3, False, "reason too long")),
            ('{"issue": 3, "reason": "ok", "split": {}}', (3, False, "unexpected keys: split")),
            ("not json", (None, False, "invalid JSON")),
            ("[3]", (None, False, "invalid JSON")),
            ("", (None, False, "invalid JSON")),
            ("{" * 100000 + "}" * 100000, (None, False, "invalid JSON")),
        ]
        for text, expected in cases:
            with self.subTest(text=text[:60]):
                self.assertEqual(validate(text, ALLOWED), expected)

    def _row_for(self, answer, tick):
        IssuePlanner(lambda _: PAID, shadow=lambda p, rf, t: answer,
                     db_path=self.db, shadow_timeout=30).choose(ISSUES, tick_id=tick)
        return self.wait_for_row()

    def test_float_issue_is_recorded_as_invalid(self):
        row = self._row_for('{"issue": 3.9, "reason": "ok"}', "tick-float")
        self.assertEqual((row["local_issue"], row["local_valid"], row["error"]),
                         (None, 0, "invalid issue"))

    def test_non_finite_issue_still_writes_the_row(self):
        row = self._row_for('{"issue": Infinity, "reason": "ok"}', "tick-inf")
        self.assertEqual((row["local_valid"], row["error"]), (0, "invalid issue"))

    def test_huge_integer_is_not_allowed_and_still_writes_the_row(self):
        row = self._row_for('{"issue": %d, "reason": "ok"}' % 10 ** 30, "tick-huge")
        self.assertEqual((row["local_issue"], row["local_valid"], row["error"]),
                         (None, 0, "issue is not allowed"))

    def test_missing_reason_is_invalid(self):
        row = self._row_for('{"issue": 3}', "tick-noreason")
        self.assertEqual((row["local_issue"], row["local_valid"], row["error"]),
                         (3, 0, "invalid reason"))

    def test_malformed_cached_tokens_are_dropped_not_fatal(self):
        for index, cached in enumerate((10 ** 30, True, float("inf"), -3, "12", 1.5)):
            db = str(Path(self.tmp.name) / f"c{index}.db")
            IssuePlanner(lambda _: PAID,
                         shadow=lambda p, rf, t: {"content": '{"issue": 3, "reason": "x"}',
                                                  "cached_prompt_tokens": cached},
                         db_path=db, shadow_timeout=30).choose(ISSUES, tick_id="c")
            deadline = time.monotonic() + 30
            while not list_shadow(db)["count"] and time.monotonic() < deadline:
                time.sleep(0.01)
            row = list_shadow(db)["rows"][0]
            self.assertIsNone(row["cached_prompt_tokens"], cached)
            self.assertEqual(row["local_valid"], 1)

    def test_non_string_answers_are_invalid_json(self):
        for index, answer in enumerate((None, 42, ["x"], {"content": None}, {"content": 7})):
            db = str(Path(self.tmp.name) / f"n{index}.db")
            IssuePlanner(lambda _: PAID, shadow=lambda p, rf, t: answer,
                         db_path=db, shadow_timeout=30).choose(ISSUES, tick_id="n")
            deadline = time.monotonic() + 30
            while not list_shadow(db)["count"] and time.monotonic() < deadline:
                time.sleep(0.01)
            row = list_shadow(db)["rows"][0]
            self.assertEqual((row["local_valid"], row["error"]), (0, "invalid JSON"), answer)


class ShadowFailureModeTests(_ShadowCase):
    """Interrupted, crashing, half-failing and repeated shadow runs."""

    def test_running_the_same_tick_twice_keeps_one_row(self):
        planner = IssuePlanner(lambda _: PAID,
                               shadow=lambda p, rf, t: '{"issue": 3, "reason": "x"}',
                               db_path=self.db, shadow_timeout=30)
        planner.choose(ISSUES, tick_id="tick-twice")
        self.wait_for_row()
        planner.choose(ISSUES, tick_id="tick-twice")
        time.sleep(0.2)
        self.assertEqual(list_shadow(self.db)["summary"]["total"], 1)

    def test_interrupted_shadow_call_records_the_failure(self):
        def interrupted(prompt, response_format, deadline):
            raise KeyboardInterrupt("stopped")

        choice = IssuePlanner(lambda _: PAID, shadow=interrupted, db_path=self.db,
                              shadow_timeout=30).choose(ISSUES, tick_id="tick-int")
        self.assertEqual(choice.issue, 5)
        row = self.wait_for_row()
        self.assertEqual(row["local_valid"], 0)
        self.assertTrue(row["error"].startswith("KeyboardInterrupt"))

    def test_deadline_error_from_the_callable_is_recorded(self):
        def deadline(prompt, response_format, deadline):
            # Fails well before the shared deadline: recorded as the error.
            raise TimeoutError("upstream gave up")

        IssuePlanner(lambda _: PAID, shadow=deadline, db_path=self.db,
                     shadow_timeout=None, rates=FakeRates(None, None)).choose(
            ISSUES, tick_id="tick-deadline")
        row = self.wait_for_row()
        self.assertEqual(row["error"], "TimeoutError: upstream gave up")

    def test_unwritable_database_never_breaks_the_paid_choice(self):
        bad_db = self.tmp.name  # a directory, sqlite cannot open it
        choice = IssuePlanner(lambda _: PAID,
                              shadow=lambda p, rf, t: '{"issue": 3, "reason": "x"}',
                              db_path=bad_db, shadow_timeout=30).choose(ISSUES, tick_id="tick-bad")
        self.assertEqual(choice.issue, 5)
        time.sleep(0.2)  # the daemon logs and drops the row; nothing propagates



class ShadowSharedDeadlineTests(_ShadowCase):
    """Review 2, blocker 1: one monotonic deadline across monitor, worker and HTTP."""

    def _delay_worker_start(self, seconds):
        """Make the shadow call thread start *seconds* late (a busy VM)."""
        import threading

        original = threading.Thread

        class LateThread(original):
            def __init__(self, *args, target=None, name=None, **kwargs):
                if name == "planner-shadow-call" and target is not None:
                    inner = target

                    def target():
                        time.sleep(seconds)
                        inner()
                super().__init__(*args, target=target, name=name, **kwargs)

        return patch.object(threading, "Thread", LateThread)

    def test_late_worker_start_never_sends_after_the_deadline(self):
        calls = []
        with self._delay_worker_start(0.4):
            IssuePlanner(lambda _: PAID,
                         shadow=lambda p, rf, d: calls.append(d) or '{"issue": 3, "reason": "x"}',
                         db_path=self.db, shadow_timeout=0.1).choose(ISSUES, tick_id="tick-late")
            row = self.wait_for_row()
        # The worker woke up after the shared deadline and refused to send.
        self.assertEqual(calls, [])
        self.assertEqual(row["error"], "timeout after 0.1s (not sent)")
        self.assertEqual(row["local_valid"], 0)
        # The row was written once the worker had finished, not before.
        self.assertGreaterEqual(row["seconds"], 0.4)

    def test_late_worker_start_gets_only_the_remaining_budget(self):
        seen = []

        def fake_shadow(prompt, response_format, deadline):
            seen.append(deadline - time.monotonic())
            return '{"issue": 3, "reason": "x"}'

        with self._delay_worker_start(0.3):
            IssuePlanner(lambda _: PAID, shadow=fake_shadow, db_path=self.db,
                         shadow_timeout=1.0).choose(ISSUES, tick_id="tick-remaining")
            row = self.wait_for_row()
        self.assertEqual(len(seen), 1)
        # Not a fresh 1 s budget: the 0.3 s start delay was already spent.
        self.assertLess(seen[0], 0.75)
        self.assertGreater(seen[0], 0.0)
        self.assertEqual((row["local_issue"], row["local_valid"], row["error"]), (3, 1, None))

    def test_timeout_row_is_written_after_the_call_released_its_connection(self):
        released = []

        def honouring(prompt, response_format, deadline):
            # Cancelling and closing the connection takes a moment after the
            # deadline; the monitor must wait for it.
            time.sleep(max(0.0, deadline - time.monotonic()) + 0.3)
            released.append(time.monotonic())
            raise TimeoutError("cancelled")

        IssuePlanner(lambda _: PAID, shadow=honouring, db_path=self.db,
                     shadow_timeout=0.1).choose(ISSUES, tick_id="tick-release")
        deadline = time.monotonic() + 30
        while not list_shadow(self.db)["count"]:
            self.assertLess(time.monotonic(), deadline, "row never written")
            time.sleep(0.005)
        # When the row became visible, the call had already released.
        self.assertEqual(len(released), 1)
        row = list_shadow(self.db)["rows"][0]
        self.assertEqual(row["error"], "timeout after 0.1s")
        self.assertGreaterEqual(row["seconds"], 0.35)

    def test_answer_arriving_after_the_deadline_counts_as_timeout(self):
        def late(prompt, response_format, deadline):
            time.sleep(max(0.0, deadline - time.monotonic()) + 0.05)
            return '{"issue": 3, "reason": "too late"}'

        IssuePlanner(lambda _: PAID, shadow=late, db_path=self.db,
                     shadow_timeout=0.1).choose(ISSUES, tick_id="tick-too-late")
        row = self.wait_for_row()
        self.assertEqual((row["local_valid"], row["error"]), (0, "timeout after 0.1s"))

    def test_callable_ignoring_the_deadline_is_recorded_after_the_grace(self):
        import threading

        release = threading.Event()
        self.addCleanup(release.set)
        with patch.object(nexus_planner, "SHADOW_CANCEL_GRACE_SECONDS", 0.1):
            IssuePlanner(lambda _: PAID, shadow=lambda p, rf, d: release.wait(30) and "{}",
                         db_path=self.db, shadow_timeout=0.05).choose(ISSUES, tick_id="tick-ignore")
            row = self.wait_for_row()
        self.assertEqual(row["error"], "timeout after 0.05s (call still running)")
        release.set()

    def test_production_shadow_refuses_to_send_after_the_deadline(self):
        import nexus_tick
        from modules.nexus_planner import ShadowDeadlineExpired

        sent = []

        def handler(request):
            sent.append(request)
            raise AssertionError("must not be sent")

        patcher, created = ShadowDeadlineTests._patched_client(self, handler)
        with patcher:
            for expired in (time.monotonic(), time.monotonic() - 5):
                with self.assertRaises(ShadowDeadlineExpired):
                    nexus_tick._local_shadow("prompt", None, expired)
        self.assertEqual(created, [])
        self.assertEqual(sent, [])
        self.assertTrue(issubclass(ShadowDeadlineExpired, TimeoutError))

    def test_production_shadow_cancels_at_the_shared_deadline_not_a_fresh_budget(self):
        import asyncio
        import nexus_tick

        state = {"cancelled": False}

        async def handler(request):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        deadline = time.monotonic() + 0.5
        time.sleep(0.3)  # the worker started late: only ~0.2 s are left
        patcher, created = ShadowDeadlineTests._patched_client(self, handler)
        started = time.monotonic()
        with patcher, self.assertRaises(TimeoutError):
            nexus_tick._local_shadow("prompt", None, deadline)
        finished = time.monotonic()
        self.assertTrue(state["cancelled"])
        self.assertLessEqual(created[0].timeout, 0.21)
        # Returned at the shared deadline (plus scheduling slack), not 0.5 s later.
        self.assertLess(finished - started, 0.45)
        self.assertGreaterEqual(finished, deadline - 0.01)

    def test_production_shadow_end_to_end_through_the_planner(self):
        import asyncio
        import nexus_tick

        state = {"cancelled": False}

        async def handler(request):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        patcher, _created = ShadowDeadlineTests._patched_client(self, handler)
        with patcher:
            IssuePlanner(lambda _: PAID, shadow=nexus_tick._local_shadow, db_path=self.db,
                         shadow_timeout=0.2).choose(ISSUES, tick_id="tick-e2e")
            row = self.wait_for_row()
        # The connection was released before the timeout row was written.
        self.assertTrue(state["cancelled"])
        self.assertEqual(row["error"], "timeout after 0.2s")


class ShadowStrictJsonTests(_ShadowCase):
    """Review 2, blocker 2: the whole answer must be one top-level JSON object."""

    def _row_for(self, answer, tick):
        IssuePlanner(lambda _: PAID, shadow=lambda p, rf, d: answer,
                     db_path=self.db, shadow_timeout=30).choose(ISSUES, tick_id=tick)
        return self.wait_for_row()

    def test_validate_rejects_anything_but_a_bare_object(self):
        validate = nexus_planner.validate_shadow_answer
        for text in ('[{"issue": 3, "reason": "ok"}]',
                     'Sure! {"issue": 3, "reason": "ok"}',
                     '{"issue": 3, "reason": "ok"} trailing',
                     '```json\n{"issue": 3, "reason": "ok"}\n```',
                     '{"issue": 3, "reason": "ok"}{"issue": 5, "reason": "ok"}',
                     '"{\\"issue\\": 3, \\"reason\\": \\"ok\\"}"',
                     '{"issue": %s, "reason": "ok"}' % ("9" * 5000)):
            with self.subTest(text=text[:60]):
                self.assertEqual(validate(text, ALLOWED), (None, False, "invalid JSON"))
        # Surrounding whitespace is still one JSON value.
        self.assertEqual(validate('\n {"issue": 3, "reason": "ok"} \n', ALLOWED), (3, True, None))

    def test_array_answer_is_persisted_as_invalid(self):
        row = self._row_for('[{"issue": 3, "reason": "ok"}]', "tick-array")
        self.assertEqual((row["local_issue"], row["local_valid"], row["error"]),
                         (None, 0, "invalid JSON"))

    def test_embedded_object_is_persisted_as_invalid(self):
        row = self._row_for('I pick {"issue": 3, "reason": "ok"} because', "tick-embedded")
        self.assertEqual((row["local_valid"], row["error"]), (0, "invalid JSON"))

    def test_lone_surrogate_key_is_persisted_with_an_encodable_error(self):
        answer = '{"issue": 3, "reason": "ok", "\\ud800": 1}'
        local_issue, valid, error = nexus_planner.validate_shadow_answer(answer, ALLOWED)
        self.assertFalse(valid)
        error.encode("utf-8")  # must not raise
        self.assertEqual(error, "unexpected keys: '\\ud800'")
        row = self._row_for(answer, "tick-surrogate")
        self.assertEqual((row["local_issue"], row["local_valid"]), (3, 0))
        self.assertEqual(row["error"], "unexpected keys: '\\ud800'")

    def test_surrogate_in_an_exception_message_still_writes_the_row(self):
        def broken(prompt, response_format, deadline):
            raise RuntimeError("bad \ud800 bytes")

        IssuePlanner(lambda _: PAID, shadow=broken, db_path=self.db,
                     shadow_timeout=30).choose(ISSUES, tick_id="tick-exc-surrogate")
        row = self.wait_for_row()
        self.assertEqual(row["error"], "RuntimeError: bad \\ud800 bytes")

    def test_encodable_text(self):
        self.assertIsNone(nexus_planner.encodable_text(None))
        self.assertEqual(nexus_planner.encodable_text("plain ä"), "plain ä")
        self.assertEqual(nexus_planner.encodable_text("x\udc80"), "x\\udc80")


if __name__ == "__main__":
    unittest.main()
