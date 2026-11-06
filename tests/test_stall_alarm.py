"""The owner hears about a Nexus that starts no work for hours (#521)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nexus_tick

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class StallAlarmTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "agent.db")
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "CREATE TABLE nexus_initiative_log (tick_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
                "decision TEXT NOT NULL, source_kind TEXT, source_ref TEXT, reason TEXT, "
                "considered_json TEXT, detail TEXT)"
            )
        self.n = 0

    def _log(self, hours_ago: float, decision: str, detail: str = "") -> None:
        self.n += 1
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO nexus_initiative_log (tick_id, created_at, decision, detail) VALUES (?, ?, ?, ?)",
                (f"t{self.n}", (NOW - timedelta(hours=hours_ago)).isoformat(), decision, detail),
            )

    def _alarm(self, now=NOW):
        return nexus_tick._stall_alarm(self.db_path, now=now)

    def test_the_506_standstill_raises_an_alarm_with_its_reason(self):
        self._log(20, "dispatched")
        self._log(17, "skipped_unapproved_issue", "issue #230 has no approval record (pending)")
        for hours in range(16, 0, -1):
            self._log(hours, "idle", "no actionable work found")
        message = self._alarm()
        self.assertIn("started no work for 20h", message)
        self.assertIn("skipped_unapproved_issue x1", message)
        self.assertIn("no approval record", message)

    def test_recent_work_is_no_alarm(self):
        self._log(2, "dispatched")
        self._log(1, "idle")
        self.assertIsNone(self._alarm())

    def test_a_long_running_experiment_or_the_budget_is_no_alarm(self):
        self._log(10, "dispatched")
        self._log(1, "busy")
        self.assertIsNone(self._alarm())
        self._log(0.5, "budget_exhausted")
        self.assertIsNone(self._alarm())

    def test_the_alarm_repeats_at_most_once_a_day(self):
        self._log(30, "dispatched")
        self._log(1, "idle")
        self.assertIsNotNone(self._alarm())
        self.assertIsNone(self._alarm(NOW + timedelta(hours=3)))
        self.assertIsNotNone(self._alarm(NOW + timedelta(hours=25)))

    def test_an_empty_log_is_no_alarm(self):
        self.assertIsNone(self._alarm())


if __name__ == "__main__":
    unittest.main()
