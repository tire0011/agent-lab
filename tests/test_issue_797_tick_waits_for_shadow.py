import os
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import nexus_tick
from modules.nexus_planner import IssuePlanner
from modules.planner_shadow import list_shadow


ISSUES = [{"number": 1, "title": "one", "body": ""}]
ANSWER = '{"issue": 1, "reason": "test choice"}'


class ShadowWaitTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "agent.db")
        env = patch.dict(os.environ, {
            "NEXUS_PLANNER_SHADOW": "1",
            "NEXUS_PLANNER_SHADOW_PROMPT_TOKENS": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def planner(self, shadow):
        return IssuePlanner(lambda _: ANSWER, shadow=shadow,
                            db_path=self.db, rates=(None, None))

    def test_wait_returns_after_the_monitor_stores_the_result(self):
        def shadow(prompt, timeout):
            time.sleep(0.05)
            return ANSWER

        planner = self.planner(shadow)
        planner.choose(ISSUES, tick_id="finished")
        planner.wait_for_shadow(1)

        row, = list_shadow(self.db)["rows"]
        self.assertEqual(row["tick_id"], "finished")
        self.assertEqual(row["local_valid"], 1)
        self.assertIsNone(row["error"])

    @pytest.mark.usefixtures("real_clock")
    def test_hung_shadow_stores_one_exit_row_even_if_it_finishes_later(self):
        release = threading.Event()
        planner = self.planner(lambda prompt, timeout: release.wait(2) and ANSWER)
        planner.choose(ISSUES, tick_id="hung")
        try:
            started = time.monotonic()
            planner.wait_for_shadow(0.05)
            self.assertLess(time.monotonic() - started, 0.5)
            row, = list_shadow(self.db)["rows"]
            self.assertEqual(row["tick_id"], "hung")
            self.assertEqual(row["paid_issue"], 1)
            self.assertEqual(row["error"], "tick_exit_before_shadow")
            self.assertIsNone(row["local_issue"])
            self.assertEqual(row["local_valid"], 0)
            self.assertGreater(row["prompt_tokens"], 0)
            planner.wait_for_shadow(0)
        finally:
            release.set()
            planner.wait_for_shadow(1)
        self.assertEqual(list_shadow(self.db)["rows"], [row])

    def test_wait_without_a_shadow_returns_immediately(self):
        planner = self.planner(None)
        with patch.object(threading.Thread, "join") as join:
            planner.wait_for_shadow(1)
        join.assert_not_called()
        self.assertIsNone(planner.shadow_timeout)
        self.assertFalse(Path(self.db).exists())

    @pytest.mark.usefixtures("real_clock")
    def test_exit_write_does_not_wait_for_a_database_lock(self):
        release = threading.Event()
        planner = self.planner(lambda prompt, timeout: release.wait(2) and ANSWER)
        planner.choose(ISSUES, tick_id="locked")
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertLogs("modules.nexus_planner", level="WARNING") as logs:
                started = time.monotonic()
                planner.wait_for_shadow(0.05)
                self.assertLess(time.monotonic() - started, 0.5)
            self.assertIn("could not store tick exit", logs.output[0])
        finally:
            conn.close()
            release.set()
            planner.wait_for_shadow(1)
        row, = list_shadow(self.db)["rows"]
        self.assertIsNone(row["error"])

    def test_tick_waits_with_the_model_timeout_plus_ten(self):
        service = Mock(controller=None, planner=Mock(shadow_timeout=240))
        service.abandoned_work.return_value = []
        service.tick.return_value = SimpleNamespace(
            acted=False, decision="idle", detail="", candidate=None,
        )
        with patch("modules.main_guard.guard_main", return_value=None), patch.multiple(
            nexus_tick,
            _build_service=Mock(return_value=service),
            _record_tick_start=Mock(),
            _reconcile_stale_claims=Mock(),
            _health_check=Mock(return_value=None),
            _sweep_stalled_experiments=Mock(),
            _wait_for_experiments=Mock(),
            _cleanup_expired_worktrees=Mock(),
            _prune_test_tmp=Mock(),
            _update_clis=Mock(),
            _weekly_review=Mock(),
            _shadow_replay=Mock(),
            _stall_alarm=Mock(return_value=None),
            _finished_badly=Mock(return_value=[]),
            _notify=Mock(),
        ), patch("modules.nexus_status_issue.update_for"):
            result = nexus_tick.main(["--db-path", self.db, "--repo-root", "."])

        self.assertEqual(result, 0)
        service.planner.wait_for_shadow.assert_called_once_with(250)
