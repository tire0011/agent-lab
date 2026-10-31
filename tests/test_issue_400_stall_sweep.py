"""#400: an experiment that logs nothing past its phase limit is marked stalled.

Experiment deac59fb logged ``codex_call_start`` and then nothing for 11 hours;
it stayed ``implementing`` until a service restart, blocking its issue, the
daily budget and the retry logic.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from modules.central_logging import ExperimentLogger
from modules.coding_worker import progress_heartbeat, run_watched
from modules.experiment_service import (
    DEFAULT_STALL_GRACE_SECONDS,
    ExperimentService,
    stall_grace_seconds,
)
from modules.experiment_service import service as service_module
from modules.nexus_initiative import NexusInitiativeService

# Default limits: 3h agent cap + 15 min grace.
PAST_THE_LIMIT = timedelta(hours=4)
WITHIN_THE_LIMIT = timedelta(hours=1)


class _NoWorker:
    def run(self, **kwargs):  # pragma: no cover - never called here
        raise AssertionError("no coding agent may run in this test")


class StallSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in (
            "NEXUS_AGENT_MAX_SECONDS", "NEXUS_AGENT_STALL_SECONDS",
            "NEXUS_TEST_SAFETY_CAP_SECONDS", "NEXUS_EXPERIMENT_STALL_GRACE_SECONDS",
        ):
            os.environ.pop(key, None)
        self.db_path = str(self.tmp / "agent.db")
        self.service = ExperimentService(
            selection_worker=_NoWorker(),
            worker_factory=lambda path: _NoWorker(),
            plan_worker_factory=lambda path: _NoWorker(),
            db_path=self.db_path,
            repo_root=str(self.tmp / "repo"),
            worktrees_root=str(self.tmp / "worktrees"),
            experiment_log_dir=str(self.tmp / "logs"),
        )
        self.addCleanup(self.service.close)

    def _experiment(self, status: str = "implementing", *, pgid: int | None = None) -> str:
        experiment_id = self.service.create_experiment(goal="Add a small helper.").id
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE experiments SET status = ?, worker_pgid = ? WHERE id = ?",
                (status, pgid, experiment_id),
            )
        return experiment_id

    def _log_events(self, experiment_id: str) -> list[dict]:
        path = self.service.experiment_log_path(experiment_id)
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _sleeper(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        def stop() -> None:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

        self.addCleanup(stop)
        return proc

    def test_a_silent_implementing_experiment_is_marked_stalled(self):
        experiment_id = self._experiment()

        stalled = self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT)

        self.assertEqual(stalled, [experiment_id])
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, "failed")
        self.assertRegex(experiment.error, r"^stalled: no progress for \d+s in phase implementing$")
        self.assertIsNotNone(experiment.ended_at)
        detected = [e for e in self._log_events(experiment_id) if e["event"] == "stalled_detected"]
        self.assertEqual(len(detected), 1)
        # Names the last event seen and when it happened.
        self.assertTrue(detected[0]["last_event"])
        self.assertIn(detected[0]["last_event"], detected[0]["message"])
        self.assertIn("last_event_at", detected[0])

    def test_a_recent_event_and_a_terminal_experiment_are_left_alone(self):
        recent = self._experiment()
        finished = self._experiment("succeeded")

        self.assertEqual(
            self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + WITHIN_THE_LIMIT), []
        )
        self.assertEqual(self.service.get_experiment(recent).status, "implementing")

        # A terminal experiment stays as it is however old it gets.
        self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT)
        self.assertEqual(self.service.get_experiment(finished).status, "succeeded")

    def test_a_progress_entry_keeps_an_old_experiment_alive(self):
        experiment_id = self._experiment()
        now = datetime.now(timezone.utc) + PAST_THE_LIMIT
        log_path = self.service.experiment_log_path(experiment_id)
        with log_path.open("a") as fh:
            fh.write(json.dumps({
                "timestamp": (now - timedelta(minutes=5)).isoformat(),
                "level": "INFO", "logger": "nexus.experiment", "phase": "implementation",
                "event": "coding_agent_progress", "message": "still running",
            }) + "\n")

        self.assertEqual(self.service.sweep_stalled_experiments(now), [])
        self.assertEqual(self.service.get_experiment(experiment_id).status, "implementing")

    def test_verification_gets_the_larger_test_cap(self):
        experiment_id = self._experiment("verifying")
        self.assertEqual(
            self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + WITHIN_THE_LIMIT), []
        )
        self.assertEqual(
            self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT),
            [experiment_id],
        )

    def test_the_grace_comes_from_the_environment(self):
        experiment_id = self._experiment()
        os.environ["NEXUS_EXPERIMENT_STALL_GRACE_SECONDS"] = str(2 * 60 * 60)
        self.assertEqual(
            self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT), []
        )
        os.environ["NEXUS_EXPERIMENT_STALL_GRACE_SECONDS"] = "0"
        self.assertEqual(
            self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT),
            [experiment_id],
        )

    def test_a_short_configured_agent_cap_is_honored_after_grace(self):
        os.environ["NEXUS_AGENT_MAX_SECONDS"] = "3600"
        experiment_id = self._experiment()
        created_at = self.service.get_experiment(experiment_id).created_at
        self.assertIsNotNone(created_at)
        last_progress = datetime.fromisoformat(created_at)
        deadline = last_progress + timedelta(seconds=3600 + DEFAULT_STALL_GRACE_SECONDS)

        # The deadline includes the grace and is strict: logging exactly at
        # the boundary is still within the limit.
        self.assertEqual(self.service.sweep_stalled_experiments(deadline), [])
        self.assertEqual(self.service.get_experiment(experiment_id).status, "implementing")
        self.assertEqual(
            self.service.sweep_stalled_experiments(deadline + timedelta(seconds=1)),
            [experiment_id],
        )
        detected = [e for e in self._log_events(experiment_id) if e["event"] == "stalled_detected"]
        self.assertEqual(detected[0]["limit_seconds"], 3600)
        self.assertEqual(detected[0]["grace_seconds"], DEFAULT_STALL_GRACE_SECONDS)

    def test_default_and_oversized_agent_caps_keep_the_three_hour_maximum(self):
        self.assertEqual(
            self.service._phase_hard_limit_seconds("implementing"),
            3 * 60 * 60,
        )
        os.environ["NEXUS_AGENT_MAX_SECONDS"] = str(6 * 60 * 60)
        self.assertEqual(
            self.service._phase_hard_limit_seconds("implementing"),
            3 * 60 * 60,
        )

    def test_grace_parsing(self):
        self.assertEqual(stall_grace_seconds(""), DEFAULT_STALL_GRACE_SECONDS)
        self.assertEqual(stall_grace_seconds("600"), 600)
        self.assertEqual(stall_grace_seconds("0"), 0)
        # A typo or a negative value must not turn the sweep aggressive.
        self.assertEqual(stall_grace_seconds("15m"), DEFAULT_STALL_GRACE_SECONDS)
        self.assertEqual(stall_grace_seconds("-5"), DEFAULT_STALL_GRACE_SECONDS)

    def test_a_live_worker_recorded_by_another_process_is_terminated(self):
        proc = self._sleeper()
        experiment_id = self._experiment(pgid=proc.pid)

        with mock.patch.object(service_module, "_STALL_TERMINATE_GRACE_SECONDS", 0.5):
            self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT)

        self.assertEqual(proc.wait(timeout=10), -signal.SIGTERM)
        self.assertEqual(self.service.get_experiment(experiment_id).status, "failed")
        detected = [e for e in self._log_events(experiment_id) if e["event"] == "stalled_detected"]
        self.assertEqual(detected[0]["worker"], "terminated")
        with sqlite3.connect(self.db_path) as conn:
            pgid = conn.execute("SELECT worker_pgid FROM experiments WHERE id = ?", (experiment_id,)).fetchone()[0]
        self.assertIsNone(pgid)

    def test_a_worker_tracked_in_this_process_is_terminated(self):
        experiment_id = self._experiment()
        proc = self._sleeper()
        self.service._track_process(experiment_id, proc)
        with sqlite3.connect(self.db_path) as conn:
            recorded = conn.execute("SELECT worker_pgid FROM experiments WHERE id = ?", (experiment_id,)).fetchone()[0]
        self.assertEqual(recorded, proc.pid)

        self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT)

        self.assertIsNotNone(proc.poll())
        self.assertEqual(self.service._active_processes, {})

    def test_a_missing_worker_pid_does_not_raise(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        gone.wait()
        experiment_id = self._experiment(pgid=gone.pid)

        self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT)

        self.assertEqual(self.service.get_experiment(experiment_id).status, "failed")
        detected = [e for e in self._log_events(experiment_id) if e["event"] == "stalled_detected"]
        self.assertEqual(detected[0]["worker"], "worker no longer running")

    def test_the_own_process_group_is_never_signalled(self):
        experiment_id = self._experiment(pgid=os.getpgrp())
        self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT)
        detected = [e for e in self._log_events(experiment_id) if e["event"] == "stalled_detected"]
        self.assertEqual(detected[0]["worker"], "refused to signal own process group")

    def test_sweeping_twice_is_idempotent(self):
        experiment_id = self._experiment()
        now = datetime.now(timezone.utc) + PAST_THE_LIMIT

        self.assertEqual(self.service.sweep_stalled_experiments(now), [experiment_id])
        error = self.service.get_experiment(experiment_id).error
        self.assertEqual(self.service.sweep_stalled_experiments(now + timedelta(hours=5)), [])

        self.assertEqual(self.service.get_experiment(experiment_id).error, error)
        detected = [e for e in self._log_events(experiment_id) if e["event"] == "stalled_detected"]
        self.assertEqual(len(detected), 1)

    def test_the_pipeline_cannot_overwrite_the_stall_verdict(self):
        # The pipeline thread wakes up once its worker is stopped and would
        # otherwise record its own timeout over the stall.
        experiment_id = self._experiment()
        self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT)

        self.service._force_status(experiment_id, "timed_out", error="implementation phase timed out")

        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, "failed")
        self.assertTrue(experiment.error.startswith("stalled: "))
        self.assertTrue(self.service._ended_from_outside(experiment_id))

    def test_a_stalled_experiment_counts_as_a_failed_attempt(self):
        # #374: two failed attempts end an issue; a stall is one of them.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS github_experiment_links "
                "(experiment_id TEXT PRIMARY KEY, issue_number INTEGER)"
            )
        first = self._experiment("timed_out")
        second = self._experiment()
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)",
                [(first, 388), (second, 388)],
            )
        initiative = NexusInitiativeService(
            db_path=self.db_path, experiment_service=self.service, idea_lister=lambda: [],
        )
        self.addCleanup(initiative.close)
        self.assertIn(388, initiative._linked_issue_numbers())  # still running

        self.service.sweep_stalled_experiments(datetime.now(timezone.utc) + PAST_THE_LIMIT)

        # Two failed attempts now: blocked for good, and nothing counts as busy.
        self.assertIn(388, initiative._linked_issue_numbers())
        self.assertIsNone(initiative.busy_reason())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM github_experiment_links WHERE experiment_id = ?", (first,))
        # One stalled attempt alone leaves the issue open for a retry.
        self.assertNotIn(388, initiative._linked_issue_numbers())


class ProgressHeartbeatTests(unittest.TestCase):
    def test_a_live_worker_reports_progress_while_it_runs(self):
        beats: list[tuple[float, float]] = []
        run = run_watched(
            [sys.executable, "-c", "import time; time.sleep(1.5)"],
            cwd=tempfile.gettempdir(), env={},
            stall_seconds=10, max_seconds=20, poll_seconds=0.1,
            on_heartbeat=lambda runtime, silent: beats.append((runtime, silent)),
            heartbeat_seconds=0.4,
        )
        self.assertIsNone(run.stop_reason)
        self.assertGreaterEqual(len(beats), 2)
        # A silent worker stands out: its silence grows with every beat.
        self.assertGreater(beats[-1][1], beats[0][1])

    def test_a_failing_heartbeat_does_not_stop_the_worker(self):
        def boom(runtime, silent):
            raise RuntimeError("log disk full")

        run = run_watched(
            [sys.executable, "-c", "print('done')"],
            cwd=tempfile.gettempdir(), env={},
            stall_seconds=10, max_seconds=20, poll_seconds=0.05,
            on_heartbeat=boom, heartbeat_seconds=0,
        )
        self.assertEqual(run.exit_code, 0)

    def test_the_heartbeat_lands_in_the_experiment_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            elog = ExperimentLogger("e" * 32, log_dir=tmp)
            self.assertIsNone(progress_heartbeat(None, "implementation", "Codex"))
            progress_heartbeat(elog, "implementation", "Codex")(650.0, 12.0)
            entries = [json.loads(line) for line in (Path(tmp) / f"{'e' * 32}.jsonl").read_text().splitlines()]
        self.assertEqual(entries[-1]["event"], "coding_agent_progress")
        self.assertEqual(entries[-1]["phase"], "implementation")
        self.assertIn("650s", entries[-1]["message"])


if __name__ == "__main__":
    unittest.main()
