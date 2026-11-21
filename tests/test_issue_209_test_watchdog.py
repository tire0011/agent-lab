"""Progress-aware verification watchdog and per-test logging (#209).

Covers the acceptance criteria of the issue:

* a long-running suite that keeps making progress is allowed to continue,
* a stalled run is detected from missing progress, with diagnostics naming
  the current test and how long nothing has happened,
* a dead test process is reported as such rather than as a test failure,
* the test heartbeat reaches the global Nexus log once per watchdog tick,
* per-test detail stays in its own log, isolated from the global feed,
* the absolute safety cap still exists as a final fail-safe.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.central_logging import ExperimentLogger, read_log_entries
from modules.nexus_diagnostics import (
    LIFECYCLE_RUNNING,
    PROGRESS_RUNNING,
    PROGRESS_SOURCE_LOG,
    assess_progress,
)
from modules.test_progress import (
    EVENT_START,
    RESULT_ERROR,
    RESULT_FAILED,
    RESULT_PASSED,
    RESULT_SKIPPED,
    ProgressTracker,
    UnittestStreamParser,
    format_duration,
    format_stall_diagnostics,
    parse_total_tests,
    canonical_test_id,
)
from modules.test_run_log import (
    VerificationTestLog,
    global_heartbeat_bridge,
    prune_test_logs,
    resolve_test_log_dir,
    safe_run_id,
)
from modules.verification_runner import (
    MIN_SAFETY_CAP_SECONDS,
    MIN_STALL_TIMEOUT_SECONDS,
    WatchdogConfig,
    run_verification_test_monitored,
)

# A watchdog that reacts in well under a second, so the suite stays fast.
def _record_all(tracker, events):
    for event in events:
        tracker.record(event)


def _read_entries(log):
    entries = []
    for line in log.path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def _read_heartbeat(log):
    try:
        return json.loads(log._heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

FAST = WatchdogConfig(
    stall_timeout_seconds=1.5,
    check_interval_seconds=0.05,
    safety_cap_seconds=60,
)


def _write_suite(body: str) -> Path:
    """Create a throwaway worktree-shaped directory holding one test module."""
    root = Path(tempfile.mkdtemp(prefix="nexus-watchdog-"))
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_subject.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return root


DISCOVER = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]


class UnittestStreamParserTests(unittest.TestCase):
    """The parser is the only in-band source of 'which test is running'."""

    def test_start_is_observed_before_the_result_arrives(self):
        parser = UnittestStreamParser()
        events = parser.feed("test_a (tests.m.C.test_a) ... ")
        self.assertEqual([e.kind for e in events], ["start"])
        self.assertEqual(parser.current_test, "tests.m.C.test_a")

        events = parser.feed("ok\n")
        self.assertEqual([(e.kind, e.result) for e in events], [("end", RESULT_PASSED)])
        self.assertIsNone(parser.current_test)

    def test_results_are_classified(self):
        stream = (
            "test_a (tests.m.C.test_a) ... ok\n"
            "test_b (tests.m.C.test_b) ... FAIL\n"
            "test_c (tests.m.C.test_c) ... ERROR\n"
            "test_d (tests.m.C.test_d) ... skipped 'no reason'\n"
        )
        parser = UnittestStreamParser()
        results = [e.result for e in parser.feed(stream) if e.kind == "end"]
        self.assertEqual(results, [RESULT_PASSED, RESULT_FAILED, RESULT_ERROR, RESULT_SKIPPED])

    def test_docstring_descriptions_are_handled(self):
        parser = UnittestStreamParser()
        events = parser.feed("test_a (tests.m.C.test_a)\nThe docstring line. ... ok\n")
        self.assertEqual([e.test_id for e in events], ["tests.m.C.test_a"] * 2)

    def test_chunk_boundaries_do_not_lose_events(self):
        stream = "test_a (tests.m.C.test_a) ... ok\ntest_b (tests.m.C.test_b) ... ok\n"
        parser = UnittestStreamParser()
        events = []
        for index in range(0, len(stream), 3):
            events.extend(parser.feed(stream[index:index + 3]))
        self.assertEqual(len([e for e in events if e.kind == "end"]), 2)

    def test_output_a_test_writes_itself_does_not_end_it(self):
        parser = UnittestStreamParser()
        parser.feed("test_a (tests.m.C.test_a) ... ")
        events = parser.feed("some noise the test printed\n")
        self.assertEqual([e.kind for e in events], ["output"])
        self.assertEqual(parser.current_test, "tests.m.C.test_a")
        self.assertIn("some noise the test printed", parser.filtered_output)

    def test_passing_lines_are_stripped_but_failures_and_summary_survive(self):
        """Verbose output must not push the failure summary past the output cap."""
        parser = UnittestStreamParser()
        parser.feed(
            "test_a (tests.m.C.test_a) ... ok\n"
            "test_b (tests.m.C.test_b) ... FAIL\n"
            "\n"
            "======================================================================\n"
            "FAIL: test_b (tests.m.C.test_b)\n"
            "AssertionError: 1 != 2\n"
            "\n"
            "Ran 2 tests in 0.1s\n"
            "\nFAILED (failures=1)\n"
        )
        parser.flush()
        output = parser.filtered_output
        self.assertNotIn("test_a (tests.m.C.test_a) ... ok", output)
        self.assertIn("test_b (tests.m.C.test_b) ... FAIL", output)
        self.assertIn("AssertionError: 1 != 2", output)
        self.assertIn("FAILED (failures=1)", output)

    def test_ids_and_totals_are_extracted(self):
        # Python <= 3.10 spells the description with the class, 3.11+ with
        # the full dotted path; both must produce one canonical id.
        self.assertEqual(canonical_test_id("test_a (tests.m.C)"), "tests.m.C.test_a")
        self.assertEqual(canonical_test_id("test_a (tests.m.C.test_a)"), "tests.m.C.test_a")
        self.assertEqual(parse_total_tests("Ran 1873 tests in 900.1s\n"), 1873)
        self.assertIsNone(parse_total_tests("nothing here"))


class TestProgressTrackerTests(unittest.TestCase):
    def test_progress_age_follows_test_events_once_any_are_seen(self):
        parser = UnittestStreamParser()
        tracker = ProgressTracker(expected_total=10)
        _record_all(tracker, parser.feed("test_a (tests.m.C.test_a) ... ok\n"))
        snapshot = tracker.snapshot(process_alive=True)
        self.assertEqual(snapshot["tests_completed"], 1)
        self.assertEqual(snapshot["tests_total"], 10)
        self.assertLess(snapshot["last_progress_age_seconds"], 1.0)

        # Raw output alone must not count as progress once tests are running:
        # a process printing while wedged between tests is still stalled.
        before = tracker.seconds_since_progress()
        time.sleep(0.05)
        tracker.note_activity()
        self.assertGreater(tracker.seconds_since_progress(), before)

    def test_diagnostics_name_the_test_and_the_gap(self):
        parser = UnittestStreamParser()
        tracker = ProgressTracker(expected_total=1873)
        _record_all(tracker, parser.feed("test_foo (tests.test_xyz.SomeTest.test_foo) ... "))
        report = format_stall_diagnostics(tracker.snapshot(process_alive=True, pid=4242))
        self.assertIn("Test run stalled", report)
        self.assertIn("current_test: tests.test_xyz.SomeTest.test_foo", report)
        self.assertIn("last_progress:", report)
        self.assertIn("process_alive: true", report)
        self.assertIn("/ 1873", report)
        self.assertIn("pid: 4242", report)

    def test_duration_formatting_matches_the_reported_shape(self):
        self.assertEqual(format_duration(432), "7m 12s")
        self.assertEqual(format_duration(5), "5s")
        self.assertEqual(format_duration(3700), "1h 1m 40s")


class TestRunLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="nexus-test-log-"))

    def test_entries_and_heartbeat_are_durable(self):
        log = VerificationTestLog("run-1", log_dir=self.dir)
        log.log_test_start("tests.m.C.test_a", at="2026-01-01T00:00:00+00:00")
        log.log_test_end(
            "tests.m.C.test_a", result=RESULT_PASSED,
            started_at="2026-01-01T00:00:00+00:00",
            at="2026-01-01T00:00:01+00:00", duration_ms=1000,
        )
        log.write_heartbeat({"current_test": "tests.m.C.test_b", "tests_completed": 1})

        entries = _read_entries(log)
        self.assertEqual([e["event"] for e in entries], ["test_started", "test_finished"])
        self.assertEqual(entries[1]["duration_ms"], 1000)
        self.assertEqual(entries[1]["result"], RESULT_PASSED)
        heartbeat = _read_heartbeat(log)
        self.assertEqual(heartbeat["current_test"], "tests.m.C.test_b")
        self.assertEqual(heartbeat["run_id"], "run-1")

    def test_log_dir_is_a_subdirectory_of_the_experiment_log_dir(self):
        """Isolation is structural: the unified feed globs ``*.jsonl`` in the
        log directory itself and does not recurse."""
        resolved = Path(resolve_test_log_dir({"AGENT_LOG_DIR": "/tmp/nexus-logs"}))
        self.assertEqual(resolved, Path("/tmp/nexus-logs/test-runs"))
        self.assertEqual(
            resolve_test_log_dir({"NEXUS_TEST_LOG_DIR": "/elsewhere"}), "/elsewhere"
        )

    def test_unsafe_run_ids_cannot_escape_the_log_directory(self):
        self.assertEqual(safe_run_id("../../etc/passwd"), "etc-passwd")
        self.assertEqual(safe_run_id(""), "unknown")
        log = VerificationTestLog("../escape", log_dir=self.dir)
        log.log_run_event("x", "y")
        self.assertEqual(log.path.parent, self.dir)

    def test_writing_failures_never_reach_the_caller(self):
        log = VerificationTestLog("run-2", log_dir=self.dir / "file-in-the-way")
        (self.dir / "file-in-the-way").write_text("not a directory", encoding="utf-8")
        log.log_test_start("tests.m.C.test_a")  # must not raise
        log.write_heartbeat({"current_test": None})
        self.assertIsNone(_read_heartbeat(log))

    def test_pruning_keeps_only_the_newest_runs(self):
        for index in range(4):
            log = VerificationTestLog(f"run-{index}", log_dir=self.dir)
            log.log_run_event("started", "x")
            log.write_heartbeat({})
            os.utime(log.path, (index, index))
        self.assertEqual(prune_test_logs(self.dir, keep=2), 2)
        remaining = sorted(path.name for path in self.dir.glob("test-*.jsonl"))
        self.assertEqual(remaining, ["test-run-2.jsonl", "test-run-3.jsonl"])
        self.assertFalse((self.dir / "test-run-0.heartbeat.json").exists())


class GlobalHeartbeatBridgeTests(unittest.TestCase):
    def test_one_summary_entry_per_tick_reaches_the_experiment_log(self):
        log_dir = Path(tempfile.mkdtemp(prefix="nexus-bridge-"))
        elog = ExperimentLogger("abc123", log_dir=str(log_dir), _mirror=False)
        publish = global_heartbeat_bridge(elog, "verifying")
        publish({
            "current_test": "tests.m.C.test_a",
            "tests_completed": 842,
            "tests_total": 1873,
            "last_progress_age_seconds": 3.5,
        })
        entries = read_log_entries(elog.path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "test_progress")
        self.assertEqual(entries[0]["phase"], "verifying")
        self.assertIn("842/1873", entries[0]["message"])
        self.assertEqual(entries[0]["current_test"], "tests.m.C.test_a")

    def test_a_published_heartbeat_makes_the_subject_look_alive(self):
        """The whole point of publishing: ``assess_progress`` - the shared
        'is this still running?' rule - must see a verification run that is
        quiet in every other respect as alive."""
        log_dir = Path(tempfile.mkdtemp(prefix="nexus-alive-"))
        elog = ExperimentLogger("def456", log_dir=str(log_dir), _mirror=False)
        global_heartbeat_bridge(elog, "verifying")({
            "current_test": "tests.m.C.test_a", "tests_completed": 3,
        })
        last_log_at = read_log_entries(elog.path)[-1]["timestamp"]

        # An hour-old row update, so only the heartbeat can carry the verdict.
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        verdict = assess_progress(
            LIFECYCLE_RUNNING, last_log_at=last_log_at, updated_at=stale,
        )
        self.assertEqual(verdict.state, PROGRESS_RUNNING)
        self.assertEqual(verdict.source, PROGRESS_SOURCE_LOG)

    def test_a_broken_logger_never_breaks_the_run(self):
        class Boom:
            def info(self, *args, **kwargs):
                raise RuntimeError("logger is down")

        from modules.verification_runner import _notify

        _notify(global_heartbeat_bridge(Boom(), "verifying"), {"tests_completed": 1})


class MonitoredRunTests(unittest.TestCase):
    """End-to-end runs of the real watchdog against real test processes."""

    def setUp(self):
        self.log_dir = Path(tempfile.mkdtemp(prefix="nexus-monitored-"))

    def _run(self, root: Path, *, config: WatchdogConfig = FAST, **kwargs):
        beats: list[dict] = []
        result = run_verification_test_monitored(
            DISCOVER, cwd=str(root), run_id="run-under-test",
            config=config, test_log_dir=self.log_dir,
            on_heartbeat=beats.append, **kwargs,
        )
        return result, beats

    def test_progressing_suite_runs_far_past_the_stall_timeout(self):
        """Six tests, each shorter than the stall timeout, total longer than
        it: progress - not total runtime - must decide."""
        root = _write_suite(
            """
            import time, unittest

            class Slow(unittest.TestCase):
                pass

            for index in range(6):
                def _make(i):
                    def test(self):
                        time.sleep(0.4)
                    test.__name__ = f"test_{i}"
                    return test
                setattr(Slow, f"test_{index}", _make(index))
            """
        )
        result, beats = self._run(root)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.stalled)
        self.assertEqual(result.tests_total, 6)
        self.assertEqual(result.progress["tests_completed"], 6)
        self.assertGreaterEqual(result.duration_ms, 2400)
        self.assertTrue(beats, "the watchdog must publish a heartbeat while running")

    def test_a_stuck_test_is_detected_and_diagnosed(self):
        root = _write_suite(
            """
            import time, unittest

            class Stuck(unittest.TestCase):
                def test_a_quick(self):
                    pass

                def test_b_hangs(self):
                    time.sleep(600)
            """
        )
        result, _ = self._run(root)
        self.assertTrue(result.stalled)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.safety_cap_exceeded)
        self.assertIsNone(result.exit_code)
        self.assertIn("Test run stalled", result.stall_diagnostics)
        self.assertIn("test_b_hangs", result.stall_diagnostics)
        self.assertIn("process_alive: true", result.stall_diagnostics)
        self.assertEqual(result.progress["tests_completed"], 1)
        # The diagnostics also reach the captured output, where failure
        # analysis and the UI failure tail will find them.
        self.assertIn("Test run stalled", result.output)

    def test_stall_diagnostics_are_written_to_the_dedicated_test_log(self):
        root = _write_suite(
            """
            import time, unittest

            class Stuck(unittest.TestCase):
                def test_hangs(self):
                    time.sleep(600)
            """
        )
        result, _ = self._run(root)
        entries = list(_read_entries(VerificationTestLog("run-under-test", log_dir=self.log_dir)))
        events = [e["event"] for e in entries]
        self.assertIn("test_run_started", events)
        self.assertIn("stalled", events)
        stall = entries[events.index("stalled")]
        self.assertIn("test_hangs", stall["message"])
        self.assertIn("test_hangs", str(stall["current_test"]))

    def test_a_dead_test_process_is_not_reported_as_a_test_failure(self):
        root = _write_suite(
            """
            import os, signal, unittest

            class Dies(unittest.TestCase):
                def test_kills_the_runner(self):
                    os.kill(os.getpid(), signal.SIGKILL)
            """
        )
        result, _ = self._run(root)
        self.assertTrue(result.process_died)
        self.assertFalse(result.stalled)
        # No exit code: the suite never reached a verdict, so this must not
        # be mistaken for "tests failed".
        self.assertIsNone(result.exit_code)
        self.assertIn("killed by signal 9", result.stall_diagnostics)
        self.assertIn("process_alive: false", result.stall_diagnostics)

    def test_safety_cap_is_the_final_fail_safe(self):
        """A process that keeps *starting* tests forever still ends, but only
        at the absolute cap - never at a short fixed timeout."""
        root = _write_suite(
            """
            import time, unittest

            class Forever(unittest.TestCase):
                pass

            for index in range(500):
                def _make(i):
                    def test(self):
                        time.sleep(0.05)
                    test.__name__ = f"test_{i}"
                    return test
                setattr(Forever, f"test_{index}", _make(index))
            """
        )
        result, _ = self._run(
            root,
            config=WatchdogConfig(
                stall_timeout_seconds=30,
                check_interval_seconds=0.05,
                safety_cap_seconds=1.0,
            ),
        )
        self.assertTrue(result.safety_cap_exceeded)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.stalled)
        self.assertIsNone(result.exit_code)
        self.assertIn("absolute safety cap", result.stall_diagnostics)

    def test_every_test_is_logged_with_timestamps_and_duration(self):
        root = _write_suite(
            """
            import time, unittest

            class Mixed(unittest.TestCase):
                def test_pass(self):
                    time.sleep(0.05)

                def test_fail(self):
                    self.assertEqual(1, 2)

                def test_skip(self):
                    self.skipTest("deliberate")
            """
        )
        result, _ = self._run(root)
        self.assertEqual(result.exit_code, 1)

        entries = [e for e in _read_entries(VerificationTestLog("run-under-test", log_dir=self.log_dir))
                   if e["kind"] == "test"]
        finished = {e["test"]: e for e in entries if e["event"] == "test_finished"}
        started = {e["test"] for e in entries if e["event"] == "test_started"}
        self.assertEqual(started, set(finished))
        self.assertEqual(len(finished), 3)
        for entry in finished.values():
            self.assertIsNotNone(entry["started_at"])
            self.assertIsNotNone(entry["at"])
            self.assertIsInstance(entry["duration_ms"], int)
        results = {name.rsplit(".", 1)[-1]: entry["result"] for name, entry in finished.items()}
        self.assertEqual(
            results,
            {"test_pass": RESULT_PASSED, "test_fail": RESULT_FAILED, "test_skip": RESULT_SKIPPED},
        )
        # `duration_ms` is the interval between this process *observing* the
        # start marker and the result, not the child's execution time. Under a
        # loaded runner the reader thread can miss the gap entirely and see
        # both in one read, so only the arithmetic is asserted here; the
        # measurement itself is pinned deterministically in
        # `DurationMeasurementTests` below.
        self.assertGreaterEqual(finished["test_subject.Mixed.test_pass"]["duration_ms"], 0)

    def test_per_test_detail_stays_out_of_the_global_log(self):
        """The dedicated log must be the only place per-test entries land."""
        root = _write_suite(
            """
            import unittest

            class Small(unittest.TestCase):
                def test_a(self):
                    pass

                def test_b(self):
                    pass
            """
        )
        global_log_dir = Path(tempfile.mkdtemp(prefix="nexus-global-"))
        elog = ExperimentLogger("expabc", log_dir=str(global_log_dir), _mirror=False)
        run_verification_test_monitored(
            DISCOVER, cwd=str(root), run_id="isolated",
            config=FAST, test_log_dir=self.log_dir,
            on_heartbeat=global_heartbeat_bridge(elog, "verifying"),
        )

        test_entries = _read_entries(VerificationTestLog("isolated", log_dir=self.log_dir))
        self.assertTrue([e for e in test_entries if e["event"] == "test_finished"])

        global_entries = read_log_entries(elog.path)
        self.assertEqual(
            [e for e in global_entries if e["event"] in {"test_started", "test_finished"}], []
        )
        # And the test log directory is not part of the unified feed's glob.
        self.assertEqual(list(global_log_dir.glob("*/*.jsonl")), [])

    def test_captured_output_stays_quiet_sized_despite_verbose_mode(self):
        root = _write_suite(
            """
            import unittest

            class Many(unittest.TestCase):
                pass

            for index in range(40):
                setattr(Many, f"test_{index}", lambda self: None)
            """
        )
        result, _ = self._run(root)
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn(" ... ok", result.output)
        self.assertIn("Ran 40 tests", result.output)
        self.assertIn("OK", result.output)

    def test_a_command_that_cannot_be_started_is_reported(self):
        root = _write_suite("import unittest\n")
        result = run_verification_test_monitored(
            ["/nonexistent/interpreter", "-m", "unittest"],
            cwd=str(root), run_id="missing-binary",
            config=FAST, test_log_dir=self.log_dir,
        )
        self.assertIsNone(result.exit_code)
        self.assertIsNotNone(result.exception)
        self.assertIn("failed to run test command", result.stderr)
        self.assertFalse(result.timed_out)


class WatchdogConfigTests(unittest.TestCase):
    def test_environment_values_are_clamped_to_safe_floors(self):
        """An operator must not be able to reintroduce a short fixed timeout."""
        config = WatchdogConfig.resolve(env={
            "NEXUS_TEST_STALL_SECONDS": "5",
            "NEXUS_TEST_SAFETY_CAP_SECONDS": "30",
        })
        self.assertEqual(config.stall_timeout_seconds, MIN_STALL_TIMEOUT_SECONDS)
        self.assertGreaterEqual(config.safety_cap_seconds, MIN_SAFETY_CAP_SECONDS)

    def test_unparsable_configuration_falls_back_to_the_defaults(self):
        config = WatchdogConfig.resolve(env={"NEXUS_TEST_STALL_SECONDS": "soon"})
        self.assertEqual(config.stall_timeout_seconds, 900)
        self.assertEqual(config.safety_cap_seconds, 90 * 60)

    def test_the_cap_always_stays_above_the_stall_timeout(self):
        config = WatchdogConfig.resolve(
            stall_timeout_seconds=4000, safety_cap_seconds=100, env={},
        )
        self.assertEqual(config.safety_cap_seconds, 8000)

    def test_an_unset_service_option_still_reaches_the_environment(self):
        """The service leaves its watchdog options unset by default, which is
        what lets a deployment tune them without editing code."""
        config = WatchdogConfig.resolve(
            stall_timeout_seconds=None,
            env={"NEXUS_TEST_STALL_SECONDS": "1200"},
        )
        self.assertEqual(config.stall_timeout_seconds, 1200)

    def test_explicit_values_are_trusted_so_tests_can_drive_it(self):
        config = WatchdogConfig.resolve(
            stall_timeout_seconds=2, check_interval_seconds=0.1,
            safety_cap_seconds=60, env={},
        )
        self.assertEqual(config.stall_timeout_seconds, 2)
        self.assertEqual(config.check_interval_seconds, 0.1)



class DurationMeasurementTests(unittest.TestCase):
    """Pin the duration arithmetic without depending on runner scheduling."""

    def test_duration_is_measured_on_the_monotonic_clock(self):
        parser = UnittestStreamParser()
        with patch("modules.test_progress.time.monotonic", side_effect=[100.0, 100.25]):
            started = parser.feed("test_pass (test_subject.Mixed.test_pass) ... ")
            finished = parser.feed("ok\n")

        self.assertEqual([event.kind for event in started], [EVENT_START])
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].duration_ms, 250)
        self.assertEqual(finished[0].result, RESULT_PASSED)

    def test_markers_arriving_together_still_report_a_duration(self):
        """The CI case: one read carries both the description and the result."""
        parser = UnittestStreamParser()
        with patch("modules.test_progress.time.monotonic", side_effect=[100.0, 100.0]):
            events = parser.feed("test_pass (test_subject.Mixed.test_pass) ... ok\n")

        finished = [event for event in events if event.duration_ms is not None]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].duration_ms, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
