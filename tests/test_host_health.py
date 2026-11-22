"""Host health checks at the tick start (2026-11-22: disk 93 % full, unnoticed)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import nexus_tick
from modules import host_health

Usage = namedtuple("Usage", "total used free")
GB = host_health.GB


class HostHealthTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.db = str(self.root / "agent.db")
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE nexus_initiative_log (tick_id TEXT PRIMARY KEY, created_at TEXT, "
                         "decision TEXT, source_kind TEXT, source_ref TEXT, reason TEXT, "
                         "considered_json TEXT, detail TEXT)")
        mem = self.root / "meminfo"
        mem.write_text("MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n")
        patcher = mock.patch.object(host_health, "MEMINFO", str(mem))
        patcher.start()
        self.addCleanup(patcher.stop)

    def disk(self, free_gb, total_gb=29):
        return mock.patch("modules.host_health.shutil.disk_usage",
                          return_value=Usage(total_gb * GB, (total_gb - free_gb) * GB, free_gb * GB))

    def run_check(self, **kwargs):
        return host_health.run(self.db, tmp=str(self.tmp), **kwargs)

    def test_healthy_host_has_no_problems_or_messages(self):
        with self.disk(15):
            result = self.run_check()
        self.assertEqual(result.problems, [])
        self.assertEqual(result.messages, [])

    def test_low_disk_cleans_up_first_and_blocks_only_if_still_low(self):
        calls = []
        with self.disk(2):
            result = self.run_check(cleanups=[lambda: calls.append(1) or 7])
        self.assertEqual(calls, [1])
        self.assertEqual([p.name for p in result.blocking], ["disk_low"])
        self.assertIn("cleaned 7", result.messages[0])
        frees = iter([2, 12])
        with mock.patch("modules.host_health.shutil.disk_usage",
                        side_effect=lambda _p: Usage(29 * GB, 0, next(frees) * GB)):
            self.assertEqual(self.run_check(cleanups=[lambda: 0]).problems, [])

    def test_the_owner_hears_about_a_problem_once_a_day(self):
        now = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
        with self.disk(4):
            self.assertEqual(len(self.run_check(cleanups=[], now=now).messages), 1)
            self.assertEqual(self.run_check(cleanups=[], now=now + timedelta(hours=1)).messages, [])
            self.assertEqual(len(self.run_check(cleanups=[], now=now + timedelta(hours=25)).messages), 1)

    def test_many_tmp_entries_and_low_memory_are_warnings(self):
        for i in range(host_health.TMP_WARN_ENTRIES + 1):
            (self.tmp / f"f{i}").touch()
        (self.root / "meminfo").write_text("MemTotal: 8000000 kB\nMemAvailable: 400000 kB\n")
        with self.disk(15):
            result = self.run_check(cleanups=[])
        self.assertEqual(sorted(p.name for p in result.problems), ["memory_low", "tmp_leftovers"])
        self.assertEqual(result.blocking, [])

    def test_clean_own_tmp_removes_only_old_entries(self):
        old, fresh = self.tmp / "old-dir", self.tmp / "fresh-file"
        old.mkdir()
        (old / "x").touch()
        fresh.touch()
        past = time.time() - 3 * 86400
        os.utime(old, (past, past))
        self.assertEqual(host_health.clean_own_tmp(str(self.tmp)), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())


class TickTests(unittest.TestCase):
    def test_a_blocking_problem_stops_the_tick_before_any_work(self):
        result = host_health.HealthResult([host_health.Problem("disk_low", "disk /: 1 GB", True)], ["Health: x"])
        with mock.patch.dict(os.environ, {"NEXUS_HEALTH_CHECK": "1"}), \
                mock.patch("modules.host_health.run", return_value=result), \
                mock.patch("modules.main_guard.guard_main", return_value=None), \
                mock.patch.object(nexus_tick, "_record_tick_start"), \
                mock.patch.object(nexus_tick, "_reconcile_stale_claims"), \
                mock.patch.object(nexus_tick, "_notify") as notify, \
                mock.patch.object(nexus_tick, "_build_service") as build:
            self.assertEqual(nexus_tick.main(["--db-path", ":memory:"]), 0)
        build.assert_not_called()
        self.assertIn("Health: x", notify.call_args[0][1])

    def test_the_check_is_off_in_test_runs(self):
        with mock.patch("modules.host_health.run") as run:
            self.assertIsNone(nexus_tick._health_check(":memory:"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
