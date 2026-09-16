"""Issue #209: progress-aware verification instead of a fixed timeout.

The live failure: experiment ``df97d67b`` fell back to local verification,
and ``python -m unittest discover`` was reported ``timed_out`` with no exit
code purely because it passed a wall-clock limit - a verdict that cannot
tell a healthy slow suite from a stuck one.

The fake suite these tests drive emits exactly what unittest's verbose
runner emits (test id flushed before the test, outcome appended after), so
the parser and watchdog are exercised against the real wire format rather
than against a mock of themselves.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest.mock
import sys
import tempfile
import time
import unittest
from pathlib import Path

from modules import verification_progress
from modules.run_store import init_db
from modules.nexus_diagnostics import LifecycleState
from modules.verification_progress import (
    UnittestProgressParser,
    VerificationProgressTracker,
)
from modules.verification_runner import (
    STALL_NO_PROGRESS,
    STALL_PROCESS_DIED,
    STALL_SAFETY_CAP,
    VerificationWatchdogConfig,
    run_verification_test,
)

# Emits verbose-unittest output: `count` tests `delay` seconds apart, then
# either a clean summary or a test that never finishes.
_FAKE_SUITE = """
import os, sys, time
count = int(sys.argv[1]); delay = float(sys.argv[2]); ending = sys.argv[3]
for index in range(count):
    sys.stderr.write("test_%d (tests.test_fake.Fake.test_%d) ... " % (index, index))
    sys.stderr.flush()
    time.sleep(delay)
    sys.stderr.write("ok\\n")
    sys.stderr.flush()
if ending in ("hang", "spin"):
    sys.stderr.write("test_stuck (tests.test_fake.Fake.test_stuck) ... ")
    sys.stderr.flush()
    if ending == "spin":
        while True:
            pass
    time.sleep(3600)
if ending == "env":
    sys.stdout.write(json.dumps({
        "cwd": os.getcwd(), "api_key": os.environ.get("AGENT_API_KEY"),
        "azure": os.environ.get("AZURE_OPENAI_API_KEY"),
    }))
sys.stderr.write("\\n----------\\nRan %d tests in 0.1s\\n\\nOK\\n" % count)
sys.exit(0)
"""


def _fast_watchdog(**overrides) -> VerificationWatchdogConfig:
    """Production-shaped config with test-sized windows.

    Direct construction is unclamped on purpose (only ``from_env`` enforces
    the floors), which is what lets a stall be provoked in under a second.
    """
    settings = {"stall_seconds": 1.0, "poll_seconds": 0.05, "safety_cap_seconds": 60.0}
    settings.update(overrides)
    return VerificationWatchdogConfig(**settings)


class WatchdogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.suite = self.root / "fake_suite.py"
        self.suite.write_text("import json\n" + _FAKE_SUITE, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def command(self, count: int, delay: float, ending: str = "clean") -> list[str]:
        return [sys.executable, str(self.suite), str(count), str(delay), ending]


class ProgressingSuiteTests(WatchdogTestCase):
    def test_long_suite_is_not_killed_while_it_keeps_making_progress(self):
        # Total runtime (~1.5s) deliberately exceeds the 0.4s stall window:
        # under the old fixed timeout this run died, and it is exactly the
        # healthy-but-slow case #209 exists to keep alive.
        watchdog = _fast_watchdog(stall_seconds=0.4, safety_cap_seconds=60.0)

        started = time.monotonic()
        result = run_verification_test(
            self.command(10, 0.15), cwd=str(self.root), watchdog=watchdog
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.stalled)
        self.assertGreater(elapsed, watchdog.stall_seconds)
        self.assertEqual(result.progress["tests_completed"], 10)

    def test_suite_total_is_learned_from_the_run_summary(self):
        result = run_verification_test(
            self.command(4, 0.0), cwd=str(self.root), watchdog=_fast_watchdog()
        )

        self.assertEqual(result.progress["tests_total"], 4)

    def test_watchdog_run_keeps_the_isolated_env_and_worktree_cwd(self):
        # The pre-#209 path asserted this on subprocess.run; the watchdog
        # drives the child through Popen, so the secret-isolation guarantee
        # is re-asserted on the path that actually runs now.
        with unittest.mock.patch.dict(
            os.environ,
            {
                "AGENT_API_KEY": "fake-production-secret-should-not-leak",
                "AZURE_OPENAI_API_KEY": "azure-secret-should-not-leak",
            },
        ):
            result = run_verification_test(
                self.command(1, 0.0, "env"), cwd=str(self.root), watchdog=_fast_watchdog()
            )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["cwd"], str(self.root))
        self.assertNotEqual(payload["api_key"], "fake-production-secret-should-not-leak")
        self.assertIsNone(payload["azure"])


class StalledSuiteTests(WatchdogTestCase):
    def test_stalled_run_is_stopped_and_names_the_test_that_hung(self):
        result = run_verification_test(
            self.command(3, 0.05, "hang"), cwd=str(self.root), watchdog=_fast_watchdog()
        )

        self.assertTrue(result.stalled)
        self.assertEqual(result.stall_reason, STALL_NO_PROGRESS)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)
        self.assertIn("current_test: tests.test_fake.Fake.test_stuck", result.diagnostics)
        self.assertIn("tests_completed: 3", result.diagnostics)
        self.assertIn("last_progress:", result.diagnostics)

    def test_diagnostics_survive_truncation_of_a_long_verbose_run(self):
        result = run_verification_test(
            self.command(40, 0.0, "hang"), cwd=str(self.root), watchdog=_fast_watchdog()
        )

        # Stored output is cut head-first, so the diagnostic has to lead the
        # stream it is attached to rather than trail 40 tests' worth of it.
        self.assertTrue(result.stderr.startswith("Test run stalled"))
        self.assertIn("tests.test_fake.Fake.test_9", result.stderr)

    def test_stall_diagnostics_report_the_process_as_alive(self):
        # Rendered before the kill on purpose: "stalled but alive" is a
        # deadlock, "stalled and dead" is something else entirely, and
        # killing the process first would erase the difference.
        result = run_verification_test(
            self.command(1, 0.05, "hang"), cwd=str(self.root), watchdog=_fast_watchdog()
        )

        self.assertIn("process_alive: true", result.diagnostics)

    def test_stalled_run_does_not_wait_for_the_safety_cap(self):
        watchdog = _fast_watchdog(stall_seconds=0.5, safety_cap_seconds=3600.0)

        started = time.monotonic()
        result = run_verification_test(
            self.command(1, 0.05, "hang"), cwd=str(self.root), watchdog=watchdog
        )

        self.assertTrue(result.stalled)
        self.assertLess(time.monotonic() - started, 30)

    def test_stalled_child_process_is_actually_killed(self):
        result = run_verification_test(
            self.command(1, 0.05, "hang"), cwd=str(self.root), watchdog=_fast_watchdog()
        )
        pid = result.progress["pid"]

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _process_exists(pid):
            time.sleep(0.05)
        self.assertFalse(_process_exists(pid), "stalled test process survived the watchdog")


class StallDiagnosticDepthTests(WatchdogTestCase):
    """A stalled process that is busy and one that is asleep look different.

    Issue #209 asks for this so failure analysis can separate a deadlock
    from a slow-but-working run without attaching a debugger to the VM.
    """

    def test_blocked_process_reports_no_cpu_burn(self):
        result = run_verification_test(
            self.command(1, 0.02, "hang"), cwd=str(self.root),
            watchdog=_fast_watchdog(stall_seconds=0.6, poll_seconds=0.2),
        )

        self.assertIn("cpu_time:", result.diagnostics)
        self.assertEqual(result.progress["cpu_seconds_delta"], 0.0)

    def test_spinning_process_reports_the_cpu_it_burned(self):
        result = run_verification_test(
            self.command(1, 0.02, "spin"), cwd=str(self.root),
            watchdog=_fast_watchdog(stall_seconds=0.6, poll_seconds=0.2),
        )

        self.assertTrue(result.stalled)
        self.assertGreater(result.progress["cpu_seconds_delta"], 0.0)
        self.assertIn("since last check", result.diagnostics)

    def test_cpu_burn_alone_never_counts_as_progress(self):
        # A spinning deadlock must still be stopped: if CPU were treated as
        # progress it would reset the stall window forever.
        result = run_verification_test(
            self.command(1, 0.02, "spin"), cwd=str(self.root),
            watchdog=_fast_watchdog(stall_seconds=0.5, poll_seconds=0.1, safety_cap_seconds=60.0),
        )

        self.assertEqual(result.stall_reason, STALL_NO_PROGRESS)


class SafetyCapTests(WatchdogTestCase):
    def test_safety_cap_stops_a_run_that_never_stops_progressing(self):
        # Progress never stalls here, so only the absolute cap can end it.
        watchdog = _fast_watchdog(stall_seconds=3600.0, safety_cap_seconds=0.5)

        result = run_verification_test(
            self.command(200, 0.05), cwd=str(self.root), watchdog=watchdog
        )

        self.assertTrue(result.stalled)
        self.assertEqual(result.stall_reason, STALL_SAFETY_CAP)
        self.assertTrue(result.timed_out)
        self.assertIn("safety cap", result.diagnostics)

    def test_environment_cannot_configure_a_dangerously_short_window(self):
        config = VerificationWatchdogConfig.from_env(
            {"NEXUS_TEST_STALL_SECONDS": "1", "NEXUS_TEST_SAFETY_CAP_SECONDS": "2"}
        )

        self.assertGreaterEqual(config.stall_seconds, 60)
        self.assertGreaterEqual(config.safety_cap_seconds, config.stall_seconds)

    def test_environment_configures_the_windows_when_values_are_sane(self):
        config = VerificationWatchdogConfig.from_env(
            {
                "NEXUS_TEST_STALL_SECONDS": "900",
                "NEXUS_TEST_SAFETY_CAP_SECONDS": "5400",
                "NEXUS_TEST_WATCHDOG_POLL_SECONDS": "30",
            }
        )

        self.assertEqual(config.stall_seconds, 900)
        self.assertEqual(config.safety_cap_seconds, 5400)
        self.assertEqual(config.poll_seconds, 30)

    def test_unparsable_environment_values_fall_back_to_defaults(self):
        config = VerificationWatchdogConfig.from_env({"NEXUS_TEST_STALL_SECONDS": "soon"})

        self.assertEqual(config.stall_seconds, VerificationWatchdogConfig().stall_seconds)


class DeadProcessTests(WatchdogTestCase):
    def test_killed_test_process_is_reported_without_waiting_for_a_timeout(self):
        watchdog = _fast_watchdog(stall_seconds=3600.0, safety_cap_seconds=3600.0)
        killer = _KillAfterFirstTest()

        started = time.monotonic()
        result = run_verification_test(
            self.command(50, 0.1, "hang"),
            cwd=str(self.root),
            watchdog=watchdog,
            on_progress=killer,
        )

        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(result.stall_reason, STALL_PROCESS_DIED)
        self.assertTrue(result.stalled)
        # A signalled exit *is* an exit status, so this is not a timeout -
        # calling it one would hide that something killed the process.
        self.assertFalse(result.timed_out)
        self.assertLess(result.exit_code, 0)
        self.assertIn("died before the suite finished", result.diagnostics)

    def test_unstartable_command_is_reported_as_an_exception_not_a_stall(self):
        result = run_verification_test(
            [str(self.root / "does-not-exist")], cwd=str(self.root), watchdog=_fast_watchdog()
        )

        self.assertIsNotNone(result.exception)
        self.assertFalse(result.stalled)
        self.assertFalse(result.timed_out)


class _KillAfterFirstTest:
    """Kills the test process once the run is demonstrably under way."""

    def __init__(self) -> None:
        self.killed = False

    def __call__(self, snapshot) -> None:
        if self.killed or not snapshot.tests_completed or not snapshot.pid:
            return
        self.killed = True
        try:
            os.kill(snapshot.pid, 9)
        except OSError:
            pass


def _process_exists(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PerTestLoggingTests(WatchdogTestCase):
    def setUp(self):
        super().setUp()
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()

    def _records(self, key: str) -> list[dict]:
        path = verification_progress.progress_log_path(self.log_dir, key)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_every_test_start_and_result_is_logged_with_timings(self):
        tracker = VerificationProgressTracker(log_dir=self.log_dir, key="exp209")

        run_verification_test(
            self.command(3, 0.02), cwd=str(self.root),
            watchdog=_fast_watchdog(), tracker=tracker,
        )

        records = self._records("exp209")
        starts = [r for r in records if r["event"] == "test_started"]
        completions = [r for r in records if r["event"] == "test_completed"]
        self.assertEqual(len(starts), 3)
        self.assertEqual(len(completions), 3)
        for record in completions:
            self.assertEqual(record["result"], "passed")
            self.assertTrue(record["test_id"].startswith("tests.test_fake.Fake."))
            self.assertIsNotNone(record["started_at"])
            self.assertIsNotNone(record["ended_at"])
            self.assertIsInstance(record["duration_ms"], int)

    def test_per_test_entries_stay_out_of_the_unified_nexus_feed(self):
        from modules import central_logging

        tracker = VerificationProgressTracker(log_dir=self.log_dir, key="exp209")
        experiment_log = central_logging.ExperimentLogger("exp209", log_dir=self.log_dir)
        experiment_log.info("verifying", "tests_started", "Running verification test command")

        run_verification_test(
            self.command(5, 0.0), cwd=str(self.root),
            watchdog=_fast_watchdog(), tracker=tracker,
        )

        self.assertEqual(len(self._records("exp209")), 10)
        unified = central_logging.read_all_log_entries(self.log_dir)
        self.assertEqual([entry["event"] for entry in unified], ["tests_started"])

    def test_results_other_than_pass_are_recorded_verbatim(self):
        tracker = VerificationProgressTracker(log_dir=self.log_dir, key="mixed")
        parser = UnittestProgressParser()
        stream = (
            "test_a (tests.test_x.C.test_a) ... ok\n"
            "test_b (tests.test_x.C.test_b) ... FAIL\n"
            "test_c (tests.test_x.C.test_c) ... ERROR\n"
            "test_d (tests.test_x.C.test_d) ... skipped 'not on this platform'\n"
            "test_e (tests.test_x.C.test_e) ... expected failure\n"
        )
        tracker.note_events(parser.feed(stream))

        results = [r["result"] for r in self._records("mixed") if r["event"] == "test_completed"]
        self.assertEqual(
            results, ["passed", "failed", "error", "skipped", "expected_failure"]
        )
        self.assertEqual(tracker.snapshot.results["failed"], 1)


class HeartbeatTests(WatchdogTestCase):
    def setUp(self):
        super().setUp()
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()

    def test_heartbeat_tracks_the_running_test_and_is_cleared_afterwards(self):
        tracker = VerificationProgressTracker(log_dir=self.log_dir, key="beat")
        seen: list[dict] = []

        def watch(_snapshot):
            beat = verification_progress.read_heartbeat(self.log_dir, "beat")
            if beat:
                seen.append(beat)

        run_verification_test(
            self.command(6, 0.1, "hang"), cwd=str(self.root),
            watchdog=_fast_watchdog(stall_seconds=0.5), tracker=tracker, on_progress=watch,
        )

        self.assertTrue(seen, "no heartbeat was published while tests were running")
        self.assertTrue(any(beat.get("current_test") for beat in seen))
        self.assertTrue(any(beat.get("process_alive") for beat in seen))
        self.assertTrue(max(beat.get("tests_completed", 0) for beat in seen) > 0)

        tracker.clear_heartbeat()
        self.assertIsNone(verification_progress.read_heartbeat(self.log_dir, "beat"))

    def test_missing_or_corrupt_heartbeat_reads_as_no_signal(self):
        self.assertIsNone(verification_progress.read_heartbeat(self.log_dir, "absent"))

        path = verification_progress.heartbeat_path(self.log_dir, "corrupt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(verification_progress.read_heartbeat(self.log_dir, "corrupt"))


class GlobalHeartbeatPropagationTests(unittest.TestCase):
    """The global progress verdict must see the test heartbeat (#209)."""

    def setUp(self):
        from modules.nexus_diagnostics import NexusDiagnostics

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = Path(self.tmp.name) / "logs"
        self.log_dir.mkdir()
        db_path = str(Path(self.tmp.name) / "nexus.db")
        init_db(db_path).close()
        self.diagnostics = NexusDiagnostics(db_path=db_path, log_dir=str(self.log_dir))
        self.addCleanup(self.diagnostics.close)

    def _state(self) -> LifecycleState:
        return LifecycleState(
            subject_type="experiment",
            subject_id="exp209",
            lifecycle="running",
            status="verifying",
            correlation_ids={"experiment_id": "exp209"},
            # Long enough ago to be judged interrupted on its own.
            updated_at="2020-01-01T00:00:00+00:00",
            created_at="2020-01-01T00:00:00+00:00",
        )

    def test_live_test_heartbeat_keeps_a_quiet_experiment_running(self):
        tracker = VerificationProgressTracker(log_dir=self.log_dir, key="exp209")
        tracker.snapshot.current_test = "tests.test_slow.Slow.test_big"
        tracker.snapshot.tests_completed = 842
        tracker.snapshot.tests_total = 1873
        tracker.publish()

        state = self._state()
        verdict = self.diagnostics._progress(state, None)

        self.assertEqual(verdict.state, "running")
        self.assertEqual(verdict.source, "heartbeat")
        self.assertEqual(
            state.related["test_progress"]["current_test"], "tests.test_slow.Slow.test_big"
        )
        self.assertEqual(state.related["test_progress"]["tests_completed"], 842)
        self.assertEqual(state.related["test_progress"]["tests_total"], 1873)

    def test_no_test_heartbeat_leaves_the_verdict_untouched(self):
        state = self._state()
        verdict = self.diagnostics._progress(state, None)

        self.assertNotIn("test_progress", state.related)
        self.assertEqual(verdict.state, "interrupted")


if __name__ == "__main__":
    unittest.main()
