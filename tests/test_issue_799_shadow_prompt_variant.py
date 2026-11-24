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
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import nexus_planner
from modules.nexus_planner import IssuePlanner, SHADOW_PROMPT_VARIANT
from modules.planner_shadow import list_shadow, record

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


if __name__ == "__main__":
    unittest.main()
