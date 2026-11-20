"""Bounded background re-verification of parked branches."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules.experiment_service import ExperimentService, reverify_worker
from modules.experiment_service.reverify import list_reverifications
from modules.nexus_initiative import NexusInitiativeService
from modules.nexus_planner import _history_line
from modules.timing_check import run_timing_check
from modules.verification_runner import HungTest, WatchdogConfig
from nexus_tick import _record_tick_start


class ReverifyWatchdogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = str(self.root / "runs.db")
        self.service = ExperimentService(db_path=self.db, repo_root=str(self.root))
        self.addCleanup(self.service.close)
        with self.service._conn:
            self.service._conn.execute(
                """INSERT INTO experiments (id, goal, status, created_at, ended_at, branch_name, error)
                   VALUES ('exp', 'GitHub Issue #766', 'needs_review', '2020-01-01',
                           '2020-01-01', 'parked', 'timing check failed closed: original evidence')"""
            )

    def test_fake_clock_deadline_kills_process_and_records_timeout(self):
        # Include a child in a separate session, as the normal test runner does.
        process = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, sys; p = subprocess.Popen("
             "[sys.executable, '-c', 'import time; time.sleep(60)'], "
             "start_new_session=True); print(p.pid, flush=True); p.wait()"],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait, 5)
        self.addCleanup(lambda: reverify_worker._kill_process_tree(process.pid)
                        if process.poll() is None else None)
        self.assertGreater(int(process.stdout.readline()), 0)
        attempt_root = self.root / "attempt"
        attempt_root.mkdir()
        (attempt_root / "outcome.json").write_text('{"verdict": "passes_now"}', encoding="utf-8")
        fake_clock = SimpleNamespace(monotonic=iter((10.0, 2710.0)).__next__)
        with (
            patch.object(reverify_worker, "time", fake_clock),
            patch.object(reverify_worker.subprocess, "Popen", return_value=process),
            patch.object(reverify_worker.tempfile, "mkdtemp", return_value=str(attempt_root)),
            patch.object(reverify_worker, "_run_git"),
        ):
            verdict = reverify_worker._supervise_attempt(
                self.service, self.db, "exp", "main-sha", timeout=2700,
            )
        self.assertEqual(verdict, "reverify_timeout")
        self.assertEqual(process.returncode, -9)
        self.assertFalse(attempt_root.exists())
        self.assertEqual(process.stdout.read(), "")  # the separate session closed its inherited pipe
        self.assertEqual(self.service.get_experiment("exp").status, "needs_review")
        self.assertIn("2700s", list_reverifications(self.service)[0]["reason"])

    def test_supervisor_starts_a_real_attempt_process(self):
        with self.service._conn:
            self.service._conn.execute("UPDATE experiments SET goal='No linked issue' WHERE id='exp'")
        with patch.object(reverify_worker, "_run_git"):
            verdict = reverify_worker._supervise_attempt(
                self.service, self.db, "exp", "main-sha", timeout=10,
            )
        self.assertEqual(verdict, "skipped:no_issue")

    def test_stalled_suite_records_id_and_keeps_parked_evidence(self):
        attempt_root = self.root / "attempt"
        tests = attempt_root / "worktree" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_stalled.py").write_text(
            "import time, unittest\n"
            "class Example(unittest.TestCase):\n"
            "    def test_stuck(self):\n"
            "        time.sleep(30)\n",
            encoding="utf-8",
        )
        completed = subprocess.CompletedProcess([], 0, stdout="branch-sha", stderr="")
        original = self.service.get_experiment("exp")
        with (
            patch.dict(os.environ, {"NEXUS_REVERIFY_STALL_SECONDS": "0.5",
                                    "NEXUS_TEST_LOG_DIR": str(self.root / "test-logs")}),
            patch("modules.experiment_service.reverify._branch_sha", return_value="branch-sha"),
            patch("modules.experiment_service.reverify._run_git", return_value=completed),
            patch("modules.experiment_service.service_parts.verification._run_git", return_value=completed),
            patch.object(reverify_worker, "_issue_is_open", return_value=True),
            patch.object(reverify_worker, "ExperimentService", wraps=lambda **kwargs: ExperimentService(
                **kwargs, test_python_executable=sys.executable, experiment_log_dir=str(self.root / "logs"),
            )),
        ):
            reverify_worker._run_attempt(self.db, str(self.root), "exp", "main-sha", attempt_root)
        self.assertEqual(self.service.get_experiment("exp"), original)
        self.assertEqual(list_reverifications(self.service), [])
        self.assertEqual(reverify_worker._publish_outcome(self.service, attempt_root), "hung_test")
        row = list_reverifications(self.service)[0]
        self.assertEqual((row["verdict"], row["reason"]), ("hung_test", "test_stalled.Example.test_stuck"))
        evidence = self.service._conn.execute("SELECT status, error FROM verification_runs").fetchone()
        self.assertEqual(tuple(evidence), ("hung_test", row["reason"]))
        initiative = NexusInitiativeService(db_path=self.db, experiment_service=self.service)
        self.addCleanup(initiative.close)
        with patch.object(initiative, "_drift_reader", return_value={}):
            self.assertIn(row["reason"], _history_line(initiative._recent_experiments()[0]))

    def test_pass_publishes_verified_worktree_only_after_worker_exits(self):
        attempt_root = self.root / "attempt"
        attempt_root.mkdir()
        completed = subprocess.CompletedProcess([], 0, stdout="branch-sha", stderr="")
        original = self.service.get_experiment("exp")

        def verify(service, experiment_id, _worktree, **_options):
            service._force_status(
                experiment_id, "succeeded", error=None, ended_at="2026-11-20T00:00:00+00:00",
            )
            return service.get_experiment(experiment_id)

        with (
            patch("modules.experiment_service.reverify._branch_sha", return_value="branch-sha"),
            patch("modules.experiment_service.reverify._run_git", return_value=completed),
            patch.object(reverify_worker, "_issue_is_open", return_value=True),
            patch.object(ExperimentService, "_verify", verify),
            patch.object(ExperimentService, "get_experiment_logger"),
        ):
            reverify_worker._run_attempt(self.db, str(self.root), "exp", "main-sha", attempt_root)
        self.assertEqual(self.service.get_experiment("exp"), original)
        self.assertEqual(reverify_worker._publish_outcome(self.service, attempt_root), "passes_now")
        published = self.service.get_experiment("exp")
        self.assertEqual(published.status, "succeeded")
        self.assertEqual(published.worktree_path, str(attempt_root / "worktree"))

    def test_timing_pytest_reports_active_node(self):
        (self.root / "test_timing.py").write_text(
            "import time\ndef test_stuck():\n    time.sleep(30)\n", encoding="utf-8",
        )
        with (
            patch.dict(os.environ, {"NEXUS_TEST_LOG_DIR": str(self.root / "test-logs"),
                                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}),
            self.assertRaisesRegex(HungTest, "test_timing.py::test_stuck"),
        ):
            run_timing_check(self.root, ["test_timing.py"], watchdog=WatchdogConfig(2, 0.05, 10))

    def test_two_ticks_return_while_background_work_is_running(self):
        with patch.dict(os.environ, {"AGENT_API_KEY": "test-key",
                                     "AGENT_MAILBOX_REPO_PATH": str(self.root)}):
            import api

        from fastapi import HTTPException
        running = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        both_entered = threading.Event()
        lock = threading.Lock()
        entered = exited = 0

        def maintenance(*_args):
            nonlocal entered, exited
            with lock:
                entered += 1
                running.set()
                if entered == 2:
                    both_entered.set()
            release.wait(5)
            with lock:
                exited += 1
                if exited == 2:
                    finished.set()

        def launch():
            self.assertEqual(api.launch_parked_reverification(x_api_key=api.API_KEY),
                             {"status": "scheduled"})

        initiative = NexusInitiativeService(
            db_path=self.db, experiment_service=self.service, reverification_launcher=launch,
            reflection_sweep=lambda **_kwargs: None,
        )
        self.addCleanup(initiative.close)
        with (
            patch.object(initiative, "_paused", return_value=False),
            patch.object(initiative, "_sweep_stalled_experiments"),
            patch.object(initiative, "enforce_issue_approvals"),
            patch.object(initiative, "_stale_service_revisions", return_value=None),
            patch.object(initiative, "candidates", return_value=[]),
            patch.object(reverify_worker, "run_parked_maintenance", side_effect=maintenance),
            patch.dict(os.environ, {"NEXUS_REVERIFY_PARKED": "1"}),
        ):
            try:
                with self.assertRaises(HTTPException) as denied:
                    api.launch_parked_reverification(x_api_key=None)
                self.assertEqual(denied.exception.status_code, 401)
                self.assertEqual(initiative.tick().decision, "idle")
                self.assertTrue(running.wait(2))
                self.assertEqual(initiative.tick().decision, "idle")
                self.assertTrue(both_entered.wait(2))
                self.assertEqual(exited, 0)
            finally:
                release.set()
                finished.wait(2)

    def test_tick_start_logs_rolling_maximum(self):
        start = datetime(2026, 11, 20, tzinfo=timezone.utc)
        _record_tick_start(self.db, start)
        with self.assertLogs("nexus_tick", level="WARNING") as logged:
            _record_tick_start(self.db, start + timedelta(minutes=21))
        self.assertIn("maximum over the last 72h 1260.0s", logged.output[0])
        for day in range(1, 5):
            _record_tick_start(self.db, start + timedelta(days=day))
        first_retained = self.service._conn.execute(
            "SELECT MIN(started_at) FROM initiative_tick_starts"
        ).fetchone()[0]
        self.assertEqual(first_retained, (start + timedelta(days=1)).timestamp())
