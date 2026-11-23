import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from modules import nexus_planner
from modules.nexus_planner import IssuePlanner
from modules.planner_shadow import list_shadow, record


PAID = '{"issue": 2, "reason": "paid choice"}'
ISSUES = [
    {"number": 1, "title": "one", "body": ""},
    {"number": 2, "title": "two", "body": ""},
]


class PlannerShadowTests(unittest.TestCase):
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

    def planner(self, shadow):
        return IssuePlanner(lambda _: PAID, shadow=shadow, db_path=self.db)

    def test_shadow_never_changes_paid_choice_and_writes_row(self):
        choice = self.planner(lambda prompt, timeout: '{"issue": 1, "reason": "local"}').choose(
            ISSUES, tick_id="tick-a")
        self.assertEqual(choice.issue, 2)
        row = self.wait_for_row()
        self.assertEqual((row["tick_id"], row["paid_issue"], row["local_issue"]),
                         ("tick-a", 2, 1))
        self.assertEqual(row["local_valid"], 1)
        self.assertGreater(row["prompt_tokens"], 0)

    def test_outside_allowed_is_invalid(self):
        self.planner(lambda prompt, timeout: '{"issue": 99, "reason": "local"}').choose(ISSUES)
        row = self.wait_for_row()
        self.assertEqual(row["local_issue"], 99)
        self.assertEqual(row["local_valid"], 0)
        self.assertIn("not allowed", row["error"])

    def test_exception_and_timeout_are_recorded_without_blocking(self):
        def broken(prompt, timeout):
            raise RuntimeError("offline")

        started = time.monotonic()
        choice = self.planner(broken).choose(ISSUES)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(choice.issue, 2)
        self.assertIn("offline", self.wait_for_row()["error"])

        other_db = str(Path(self.tmp.name) / "timeout.db")
        t = 0.02
        # #796: the monitor joins on the rate-based T plus the grace; a fake that
        # sleeps past it records 'timeout after ...'.
        with patch.object(nexus_planner, "shadow_timeout_seconds", lambda pt, r: t), \
                patch.object(nexus_planner, "SHADOW_MONITOR_GRACE_SECONDS", 0.01):
            slow = IssuePlanner(lambda _: PAID,
                                shadow=lambda prompt, timeout: time.sleep(0.5) or "{}",
                                db_path=other_db)
            self.db = other_db
            self.assertEqual(slow.choose(ISSUES).issue, 2)
            row = self.wait_for_row()
        self.assertIn("timeout after", row["error"])

    def test_summary_rates(self):
        for tick, values in enumerate(((1, 1, 1, .1), (2, 1, 1, .3), (1, 99, 0, .2))):
            record(self.db, tick_id=f"tick-{tick}", paid_issue=values[0], local_issue=values[1],
                   local_valid=values[2], prompt_tokens=10, seconds=values[3], error=None)
        summary = list_shadow(self.db)["summary"]
        self.assertEqual(summary["agreement"], .5)
        self.assertAlmostEqual(summary["valid_share"], 2 / 3)
        self.assertEqual(summary["median_seconds"], .2)

    def test_concurrent_result_and_skip_for_one_tick_store_one_complete_row(self):
        ready = Barrier(2, timeout=2)
        result = dict(local_issue=1, local_valid=1, prompt_tokens=100, seconds=0.1, error=None)
        skip = dict(local_issue=None, local_valid=0, prompt_tokens=0, seconds=0.0,
                    error="skipped: prompt_over_budget")

        def write(values):
            ready.wait()
            record(self.db, tick_id="replayed-tick", paid_issue=2, **values)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write, (result, skip)))

        rows = list_shadow(self.db)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0], [
            dict(tick_id="replayed-tick", paid_issue=2, **values)
            for values in (result, skip)
        ])

    def test_zero_disables_shadow_call(self):
        called = []
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW": "0"}):
            choice = self.planner(lambda prompt, timeout: called.append(prompt) or "{}").choose(ISSUES)
        self.assertEqual(choice.issue, 2)
        time.sleep(.03)
        self.assertEqual(called, [])
        with sqlite3.connect(self.db) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='planner_shadow'"
            ).fetchone()
        self.assertIsNone(table)


if __name__ == "__main__":
    unittest.main()
