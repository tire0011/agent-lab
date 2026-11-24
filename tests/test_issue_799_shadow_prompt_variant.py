"""Planner shadow part 2d (#799): prompt_variant and cached_prompt_tokens.

The nullable ``planner_shadow`` columns ``prompt_variant`` and
``cached_prompt_tokens`` are migrated onto older tables with ``ALTER TABLE``
in the same style as ``created_at``, existing rows are kept (NULL in the new
columns). ``record()`` accepts both values and ``list_shadow`` returns both.
``_start_shadow`` stores ``prompt_variant='compact_v1'`` with every row and
fills ``cached_prompt_tokens`` when the shadow callable reports the prompt
tokens llama-server reused from its cache (``timings.cache_n``), which
``nexus_tick.py`` ``shadow()`` reads from ``client.last_timings``.
"""

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import nexus_planner
from modules.nexus_planner import IssuePlanner, SHADOW_PROMPT_VARIANT
from modules import planner_shadow
from modules.planner_shadow import cached_token_count, list_shadow, record

PAID = '{"issue": 2, "reason": "paid choice"}'
ISSUES = [
    {"number": 1, "title": "one", "body": ""},
    {"number": 2, "title": "two", "body": ""},
]


class FakeRates:
    """A LocalModelRates stand-in: ``medians()`` returns the given pair."""

    def __init__(self, medians):
        self._medians = medians

    def medians(self):
        return self._medians


class PlannerShadowMigrationTests(unittest.TestCase):
    """An old planner_shadow table keeps its rows when the new columns are
    added lazily, exactly like the created_at migration (#799)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_legacy_table(self):
        # A table from before created_at, prompt_variant and
        # cached_prompt_tokens (#799).
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

    def test_record_migrates_an_old_table_and_keeps_its_rows(self):
        self._create_legacy_table()
        record(self.db, tick_id="new", paid_issue=2, local_issue=2, local_valid=1,
               prompt_tokens=100, seconds=1.0, error=None,
               prompt_variant="compact_v1", cached_prompt_tokens=700)
        with sqlite3.connect(self.db) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
        self.assertIn("created_at", columns)
        self.assertIn("prompt_variant", columns)
        self.assertIn("cached_prompt_tokens", columns)
        rows = {row["tick_id"]: row for row in list_shadow(self.db, limit=10)["rows"]}
        # The old row is still there, NULL in the migrated columns.
        self.assertEqual(rows["old"]["paid_issue"], 1)
        self.assertIsNone(rows["old"]["prompt_variant"])
        self.assertIsNone(rows["old"]["cached_prompt_tokens"])
        # The new row stores both values.
        self.assertEqual(rows["new"]["prompt_variant"], "compact_v1")
        self.assertEqual(rows["new"]["cached_prompt_tokens"], 700)

    def test_list_shadow_migrates_an_old_table_and_keeps_its_rows(self):
        self._create_legacy_table()
        rows = list_shadow(self.db)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tick_id"], "old")
        self.assertIsNone(rows[0]["prompt_variant"])
        self.assertIsNone(rows[0]["cached_prompt_tokens"])
        with sqlite3.connect(self.db) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
        self.assertIn("prompt_variant", columns)
        self.assertIn("cached_prompt_tokens", columns)

    def test_record_defaults_the_new_columns_to_null(self):
        record(self.db, tick_id="t", paid_issue=1, local_issue=None, local_valid=0,
               prompt_tokens=0, seconds=0.0, error="x")
        row = list_shadow(self.db)["rows"][0]
        self.assertIsNone(row["prompt_variant"])
        self.assertIsNone(row["cached_prompt_tokens"])


class StartShadowVariantTests(unittest.TestCase):
    """_start_shadow stores prompt_variant='compact_v1' and, when the shadow
    callable reports them, the cached prompt tokens (#799)."""

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

    def planner(self, shadow):
        return IssuePlanner(lambda _: PAID, shadow=shadow, db_path=self.db,
                            rates=FakeRates((50.0, 5.0)))

    def test_new_row_stores_compact_v1_and_null_cache_without_timings(self):
        choice = self.planner(
            lambda prompt, timeout, allowed: '{"issue": 1, "reason": "local"}'
        ).choose(ISSUES, tick_id="tick-plain")
        self.assertEqual(choice.issue, 2)
        row = self.wait_for_row()
        self.assertEqual(row["prompt_variant"], "compact_v1")
        self.assertEqual(row["prompt_variant"], SHADOW_PROMPT_VARIANT)
        self.assertIsNone(row["cached_prompt_tokens"])
        self.assertEqual(row["local_valid"], 1)

    def test_cached_prompt_tokens_from_the_shadow_answer_are_stored(self):
        def shadow(prompt, timeout, allowed):
            return {"content": '{"issue": 1, "reason": "local"}', "cached_prompt_tokens": 812}

        self.planner(shadow).choose(ISSUES, tick_id="tick-cached")
        row = self.wait_for_row()
        self.assertEqual(row["cached_prompt_tokens"], 812)
        self.assertEqual(row["prompt_variant"], "compact_v1")
        self.assertEqual(row["local_issue"], 1)
        self.assertEqual(row["local_valid"], 1)
        self.assertIsNone(row["error"])

    def test_invalid_cached_value_stays_null(self):
        def shadow(prompt, timeout, allowed):
            return {"content": '{"issue": 1, "reason": "local"}',
                    "cached_prompt_tokens": "not-a-number"}

        self.planner(shadow).choose(ISSUES, tick_id="tick-badcache")
        row = self.wait_for_row()
        self.assertIsNone(row["cached_prompt_tokens"])
        self.assertEqual(row["prompt_variant"], "compact_v1")

    def test_over_budget_skip_row_also_stores_the_variant(self):
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "1"}):
            choice = self.planner(lambda p, t, a: "{}").choose(ISSUES, tick_id="tick-skip")
        self.assertEqual(choice.issue, 2)
        rows = list_shadow(self.db)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error"], "skipped: prompt_over_budget")
        self.assertEqual(rows[0]["prompt_variant"], "compact_v1")
        self.assertIsNone(rows[0]["cached_prompt_tokens"])


LEGACY_TABLE = """CREATE TABLE planner_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id TEXT NOT NULL,
    paid_issue INTEGER NOT NULL,
    local_issue INTEGER,
    local_valid INTEGER NOT NULL CHECK (local_valid IN (0, 1)),
    prompt_tokens INTEGER NOT NULL,
    seconds REAL NOT NULL,
    error TEXT)"""


def _columns(db):
    with sqlite3.connect(db) as conn:
        return [row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")]


def _row(tick_id, **extra):
    values = dict(tick_id=tick_id, paid_issue=2, local_issue=2, local_valid=1,
                  prompt_tokens=100, seconds=1.0, error=None, prompt_variant="compact_v1")
    values.update(extra)
    return values


class CachedTokenCountTests(unittest.TestCase):
    """Optional cache telemetry is validated before conversion and binding;
    anything malformed or out of SQLite's range becomes None (#799 review)."""

    def test_valid_counts_are_kept(self):
        self.assertEqual(cached_token_count(0), 0)
        self.assertEqual(cached_token_count(812), 812)
        self.assertEqual(cached_token_count(812.0), 812)
        self.assertEqual(cached_token_count(2**63 - 1), 2**63 - 1)

    def test_malformed_and_out_of_range_counts_become_none(self):
        for value in (None, True, False, "812", "nan", [812], {"n": 1},
                      float("nan"), float("inf"), float("-inf"), -1, -0.5, 812.5,
                      2**63, 2**64, float(2**63), 1e300):
            with self.subTest(value=value):
                self.assertIsNone(cached_token_count(value))


class MalformedCacheTelemetryTests(unittest.TestCase):
    """A malformed cache count never invalidates a planner answer or keeps
    its row from being stored (#799 review, blocker 2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")
        env = patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("NEXUS_PLANNER_SHADOW_PROMPT_TOKENS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_stores_null_for_values_sqlite_cannot_bind(self):
        for i, value in enumerate((2**63, 2**80, float("nan"), float("inf"), -3, "x")):
            with self.subTest(value=value):
                record(self.db, **_row(f"t{i}", cached_prompt_tokens=value))
        rows = list_shadow(self.db, limit=50)["rows"]
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["cached_prompt_tokens"] is None for row in rows))
        self.assertTrue(all(row["local_valid"] == 1 for row in rows))

    def _start(self, cached, tick_id):
        def shadow(prompt, timeout, allowed):
            return {"content": '{"issue": 1, "reason": "local"}', "cached_prompt_tokens": cached}

        planner = IssuePlanner(lambda _: PAID, shadow=shadow, db_path=self.db,
                               rates=FakeRates((50.0, 5.0)))
        self.assertEqual(planner.choose(ISSUES, tick_id=tick_id).issue, 2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = [r for r in list_shadow(self.db, limit=50)["rows"] if r["tick_id"] == tick_id]
            if rows:
                return rows[0]
            time.sleep(0.01)
        self.fail(f"shadow row for {tick_id} was not written")

    def test_monitor_keeps_valid_answer_with_malformed_cache_count(self):
        for i, value in enumerate((float("nan"), float("inf"), 2**63, 2**64, -1, 1.5)):
            with self.subTest(value=value):
                row = self._start(value, f"tick-{i}")
                self.assertEqual(row["local_valid"], 1)
                self.assertEqual(row["local_issue"], 1)
                self.assertIsNone(row["error"])
                self.assertIsNone(row["cached_prompt_tokens"])
                self.assertEqual(row["prompt_variant"], "compact_v1")

    def test_monitor_stores_the_largest_sqlite_integer(self):
        row = self._start(2**63 - 1, "tick-max")
        self.assertEqual(row["cached_prompt_tokens"], 2**63 - 1)


class SerializedMigrationTests(unittest.TestCase):
    """The column check and ALTER TABLE run under BEGIN IMMEDIATE in both
    record() and list_shadow(), so migrations never race (#799 review,
    blocker 1); interrupted or repeated migrations are safe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")
        with sqlite3.connect(self.db) as conn:
            conn.execute(LEGACY_TABLE)
            conn.execute(
                "INSERT INTO planner_shadow (tick_id, paid_issue, local_issue, local_valid,"
                " prompt_tokens, seconds, error) VALUES ('old', 1, 1, 1, 10, 0.5, NULL)")

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_shadow_waits_for_a_concurrent_migration_instead_of_failing(self):
        # Another connection migrates and still holds its write lock while
        # list_shadow starts: list_shadow must wait for it and then find the
        # columns already there, not fail with "duplicate column name".
        other = sqlite3.connect(self.db, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        planner_shadow.ensure_table(other)
        outcome = {}

        def read():
            try:
                outcome["rows"] = list_shadow(self.db)["rows"]
            except BaseException as exc:  # noqa: BLE001
                outcome["error"] = exc

        reader = threading.Thread(target=read)
        reader.start()
        time.sleep(0.3)
        self.assertTrue(reader.is_alive(), "list_shadow must wait for the write lock")
        other.execute("COMMIT")
        other.close()
        reader.join(10)
        self.assertNotIn("error", outcome)
        self.assertEqual([r["tick_id"] for r in outcome["rows"]], ["old"])
        self.assertEqual(_columns(self.db).count("prompt_variant"), 1)

    def test_concurrent_readers_and_writers_on_a_legacy_table(self):
        errors = []
        barrier = threading.Barrier(12)

        def work(i):
            try:
                barrier.wait()
                if i % 2:
                    list_shadow(self.db)
                else:
                    record(self.db, **_row(f"t{i}", cached_prompt_tokens=i))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        rows = {r["tick_id"]: r for r in list_shadow(self.db, limit=50)["rows"]}
        self.assertEqual(set(rows), {"old"} | {f"t{i}" for i in range(0, 12, 2)})
        columns = _columns(self.db)
        for name in ("created_at", "prompt_variant", "cached_prompt_tokens"):
            self.assertEqual(columns.count(name), 1)

    def test_migration_failing_halfway_rolls_back_and_reruns_cleanly(self):
        # The third ALTER fails (NOT NULL without default), after the first
        # two ran: the whole migration must roll back, the old row stays,
        # and the next normal call migrates completely.
        broken = (("created_at", "TEXT"), ("prompt_variant", "TEXT"),
                  ("cached_prompt_tokens", "INTEGER NOT NULL"))
        with patch.object(planner_shadow, "_MIGRATED_COLUMNS", broken):
            with self.assertRaises(sqlite3.OperationalError):
                list_shadow(self.db)
            with self.assertRaises(sqlite3.OperationalError):
                record(self.db, **_row("lost"))
        self.assertNotIn("created_at", _columns(self.db))
        self.assertNotIn("prompt_variant", _columns(self.db))
        # The database is not left locked by the failed attempts.
        record(self.db, **_row("new", cached_prompt_tokens=5))
        rows = {r["tick_id"]: r for r in list_shadow(self.db)["rows"]}
        self.assertEqual(set(rows), {"old", "new"})
        self.assertEqual(rows["new"]["cached_prompt_tokens"], 5)

    def test_crash_before_commit_leaves_the_legacy_table_intact(self):
        # A process that dies mid-migration never commits; SQLite DDL is
        # transactional, so the table is exactly as before.
        crashed = sqlite3.connect(self.db, isolation_level=None)
        crashed.execute("BEGIN IMMEDIATE")
        crashed.execute("ALTER TABLE planner_shadow ADD COLUMN created_at TEXT")
        crashed.close()  # closing without COMMIT rolls back
        self.assertNotIn("created_at", _columns(self.db))
        rows = list_shadow(self.db)["rows"]
        self.assertEqual([r["tick_id"] for r in rows], ["old"])
        self.assertIn("cached_prompt_tokens", _columns(self.db))

    def test_partially_migrated_table_gets_only_the_missing_columns(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("ALTER TABLE planner_shadow ADD COLUMN created_at TEXT")
        list_shadow(self.db)
        columns = _columns(self.db)
        for name in ("created_at", "prompt_variant", "cached_prompt_tokens"):
            self.assertEqual(columns.count(name), 1)

    def test_repeated_migration_and_record_are_idempotent(self):
        list_shadow(self.db)
        list_shadow(self.db)
        record(self.db, **_row("t", cached_prompt_tokens=1))
        record(self.db, **_row("t", cached_prompt_tokens=2))  # a retry of the same tick
        rows = list_shadow(self.db)["rows"]
        self.assertEqual(sorted(r["tick_id"] for r in rows), ["old", "t"])
        self.assertEqual([r["cached_prompt_tokens"] for r in rows if r["tick_id"] == "t"], [1])

    def test_list_shadow_on_a_current_table_takes_no_write_lock(self):
        list_shadow(self.db)  # migrate once
        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with patch("sqlite3.connect",
                       lambda path, timeout=30: _real_connect(path, timeout=0.2)):
                self.assertEqual(len(list_shadow(self.db)["rows"]), 1)
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    def test_list_shadow_creates_a_missing_table(self):
        db = str(Path(self.tmp.name) / "empty.db")
        self.assertEqual(list_shadow(db)["count"], 0)
        self.assertIn("cached_prompt_tokens", _columns(db))


_real_connect = sqlite3.connect


class NexusTickShadowCacheTests(unittest.TestCase):
    """nexus_tick.py shadow() fills cached_prompt_tokens from llama-server's
    timings.cache_n when it is present (#799)."""

    def _shadow_result(self, response):
        import nexus_tick
        from modules import llama_cpp_client

        captured = {}

        class FakeClient:
            model = "fake-model"

            def __init__(self, base_url=None, timeout=300.0, model=None, *, stream=None,
                         transport=None):
                captured["timeout"] = timeout
                self.last_timings = None

            async def chat_completion(self, messages, max_tokens=1024, **kwargs):
                # Mimic the real client: normalize the server's timings.
                self.last_timings = llama_cpp_client.normalize_timings(response.get("timings"))
                return response

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(llama_cpp_client, "LlamaCppClient", FakeClient):
            planner = nexus_tick._planner(tmp, db_path=str(Path(tmp) / "agent.db"))
            return planner._shadow("the compact prompt", 123.0, {1, 2})

    def test_cache_n_is_returned_as_cached_prompt_tokens(self):
        result = self._shadow_result({
            "choices": [{"message": {"content": '{"issue": 1, "reason": "x"}'}}],
            "timings": {"prompt_n": 900, "cache_n": 812, "predicted_n": 40},
        })
        self.assertEqual(result["content"], '{"issue": 1, "reason": "x"}')
        self.assertEqual(result["cached_prompt_tokens"], 812)

    def test_missing_cache_n_is_reported_as_null(self):
        result = self._shadow_result({
            "choices": [{"message": {"content": '{"issue": 2, "reason": "x"}'}}],
            "timings": {"prompt_n": 900, "predicted_n": 40},
        })
        self.assertEqual(result["content"], '{"issue": 2, "reason": "x"}')
        self.assertIsNone(result["cached_prompt_tokens"])


    def test_malformed_cache_n_keeps_the_answer_and_reports_null(self):
        for value in (float("nan"), float("inf"), float("-inf"), 2**63, -5, 3.5):
            with self.subTest(value=value):
                result = self._shadow_result({
                    "choices": [{"message": {"content": '{"issue": 1, "reason": "x"}'}}],
                    "timings": {"prompt_n": 900, "cache_n": value, "predicted_n": 40},
                })
                self.assertEqual(result["content"], '{"issue": 1, "reason": "x"}')
                self.assertIsNone(result["cached_prompt_tokens"])


if __name__ == "__main__":
    unittest.main()
