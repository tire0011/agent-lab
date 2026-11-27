"""#842: the guarded drift rerun must prove the drift was actually active.

The local timing repair (#711) judges a candidate by rerunning the flagged
tests under the same clock drift. The review of the first attempt found two
holes: the canary only checked the clocks at the boundaries of
``pytest_runtest_call`` (a candidate could put a real clock back while the
timing-sensitive code ran and reinstate the drifted one before returning),
and a flagged test skipped during setup made pytest exit 0 without the
canary ever running. These tests pin the strengthened guard: the canary
samples the clocks while the test call runs, and every flagged node id must
show a successful call with completed canary checks in the canary report.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.experiment_service.service_parts import verification as verification_module
from modules.timing_check import (
    _canary_unverified,
    drift_env,
    flagged_tests,
    run_timing_check,
)
from modules.verification_runner import WatchdogConfig

ROOT = Path(__file__).resolve().parents[1]
NODE = "tests/test_probe.py::T::test_budget"
FUNC_NODE = "tests/test_probe.py::test_budget"

PASSING = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_budget(self):\n"
    "        self.assertLess(1, 2)\n"
)
SKIP_AT_SETUP = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def setUp(self):\n"
    "        raise unittest.SkipTest('not on this host')\n"
    "    def test_budget(self):\n"
    "        self.assertLess(1, 2)\n"
)
SKIP_IN_CALL = (
    "import pytest\n"
    "def test_budget():\n"
    "    pytest.skip('not now')\n"
)
# Puts a real clock back during the test and reinstates the drifted one
# before returning: both boundary checks pass, only an in-call sample catches it.
TEMPORARY_RESTORE = (
    "import time\n"
    "import unittest\n"
    "real_monotonic = time.monotonic\n"
    "class T(unittest.TestCase):\n"
    "    def test_budget(self):\n"
    "        saved = time.monotonic\n"
    "        time.monotonic = real_monotonic\n"
    "        time.sleep(0.1)\n"
    "        time.monotonic = saved\n"
    "        self.assertLess(1, 2)\n"
)
PERMANENT_RESTORE = (
    "import time\n"
    "import unittest\n"
    "real_monotonic = time.monotonic\n"
    "class T(unittest.TestCase):\n"
    "    def test_budget(self):\n"
    "        time.monotonic = real_monotonic\n"
    "        self.assertLess(1, 2)\n"
)


def _install_drift_conftest(repo: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import timing_check_replay
    finally:
        sys.path.pop(0)
    assert timing_check_replay.ensure_plugin(repo, ROOT)


class GuardedDriftRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "tests").mkdir(parents=True)
        _install_drift_conftest(self.repo)
        self.env = drift_env(50)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, source: str) -> None:
        (self.repo / "tests" / "test_probe.py").write_text(source, encoding="utf-8")

    def _guarded(self, node: str = NODE, **kwargs) -> str:
        return run_timing_check(self.repo, [node], env=self.env, require_active_drift=True, **kwargs)

    def test_passing_test_is_accepted_and_accepted_again_on_a_second_run(self) -> None:
        self._write(PASSING)
        # Two runs in a row: each guarded run gets its own canary report, so a
        # second run neither inherits nor is confused by the first.
        self.assertEqual(self._guarded(), "ok")
        self.assertEqual(self._guarded(), "ok")

    def test_test_skipped_during_setup_is_rejected(self) -> None:
        self._write(SKIP_AT_SETUP)
        # Without the guard pytest's zero exit status (all skipped) reads "ok".
        self.assertEqual(run_timing_check(self.repo, [NODE], env=self.env), "ok")
        result = self._guarded()
        self.assertNotEqual(result, "ok")
        self.assertEqual(flagged_tests(result), [NODE])

    def test_test_skipped_during_the_call_is_rejected(self) -> None:
        self._write(SKIP_IN_CALL)
        result = self._guarded(FUNC_NODE)
        self.assertNotEqual(result, "ok")
        self.assertEqual(flagged_tests(result), [FUNC_NODE])

    def test_temporary_clock_restoration_is_caught_during_the_call(self) -> None:
        self._write(TEMPORARY_RESTORE)
        tails: dict[str, str] = {}
        result = self._guarded(tails=tails)
        self.assertEqual(flagged_tests(result), [NODE])
        tail = tails.get(NODE, "")
        self.assertIn("clock drift canary", tail)
        self.assertIn("put back during the test", tail)

    def test_permanent_clock_restoration_is_caught(self) -> None:
        self._write(PERMANENT_RESTORE)
        tails: dict[str, str] = {}
        result = self._guarded(tails=tails)
        self.assertEqual(flagged_tests(result), [NODE])
        self.assertIn("clock drift canary: time.monotonic has no injected drift", tails.get(NODE, ""))

    def test_missing_canary_report_fails_closed(self) -> None:
        # A child that exits 0 without ever writing the canary report (crash
        # between pytest's exit and the report, or a faked process) is no
        # verdict: the guarded run must not return "ok".
        self._write(PASSING)

        class Process:
            pid = 123
            returncode = 0

            def communicate(self): return ("", None)
            def poll(self): return 0

        def popen(command, **kwargs):
            self.assertEqual(command[1], "-c")  # the guarded run uses the plugin script
            return Process()

        with patch("modules.timing_check.subprocess.Popen", side_effect=popen):
            self.assertEqual(self._guarded(), "error")

    def test_callback_failure_and_interrupt_close_the_child_on_the_guarded_path(self) -> None:
        self._write(PASSING)
        events: list = []

        class Process:
            pid = 123
            returncode = 0

            def communicate(self): return ("", None)
            def poll(self): return 0

        def popen(command, **kwargs):
            proc = Process()
            events.append(proc)
            return proc

        def callback(process):
            events.append(process)
            if process is not None:
                raise RuntimeError("observer failed")

        with patch("modules.timing_check.subprocess.Popen", side_effect=popen):
            self.assertEqual(self._guarded(on_process=callback), "error")
        self.assertIsNotNone(events[0].poll())
        self.assertIsNone(events[-1])

        def interrupt(process):
            events.append(process)
            if process is not None:
                raise KeyboardInterrupt

        with patch("modules.timing_check.subprocess.Popen", side_effect=popen):
            with self.assertRaises(KeyboardInterrupt):
                self._guarded(on_process=interrupt)
        self.assertIsNone(events[-1])

    def test_guarded_run_with_watchdog_also_requires_the_canary_report(self) -> None:
        self._write(PASSING)
        config = WatchdogConfig(
            stall_timeout_seconds=30, check_interval_seconds=0.5, safety_cap_seconds=120,
        )
        with patch.dict("os.environ", {"NEXUS_TEST_LOG_DIR": str(Path(self.tmp.name) / "test-logs")}):
            self.assertEqual(self._guarded(watchdog=config, run_id="issue-842"), "ok")

            self._write(SKIP_AT_SETUP)
            result = self._guarded(watchdog=config, run_id="issue-842-skip")
            self.assertNotEqual(result, "ok")
            self.assertEqual(flagged_tests(result), [NODE])


class CanaryReportParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.report = Path(self.tmp.name) / "canary.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unreadable_report_means_no_verdict(self) -> None:
        self.assertIsNone(_canary_unverified(self.report, [NODE]))  # missing file
        self.report.write_text("not json", encoding="utf-8")
        self.assertIsNone(_canary_unverified(self.report, [NODE]))
        self.report.write_text("[]", encoding="utf-8")
        self.assertIsNone(_canary_unverified(self.report, [NODE]))

    def test_only_run_and_passed_counts_as_verified(self) -> None:
        self.report.write_text(json.dumps({NODE: {"canary": True, "outcome": "passed"}}), encoding="utf-8")
        self.assertEqual(_canary_unverified(self.report, [NODE]), [])
        self.report.write_text(json.dumps({NODE: {"canary": True, "outcome": "skipped"}}), encoding="utf-8")
        self.assertEqual(_canary_unverified(self.report, [NODE]), [NODE])
        self.report.write_text(json.dumps({NODE: {"canary": False, "outcome": "passed"}}), encoding="utf-8")
        self.assertEqual(_canary_unverified(self.report, [NODE]), [NODE])
        self.report.write_text(json.dumps({"other::test": {"canary": True, "outcome": "passed"}}), encoding="utf-8")
        self.assertEqual(_canary_unverified(self.report, [NODE]), [NODE])


class ProtectedPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "tests").mkdir(parents=True)
        _install_drift_conftest(self.repo)
        (self.repo / ".gitignore").write_text("ignored/\n/tox.ini\n*.db\n", encoding="utf-8")
        (self.repo / "ignored").mkdir()
        (self.repo / "ignored" / "conftest.py").write_text("X = 1\n", encoding="utf-8")
        (self.repo / "tox.ini").write_text("[pytest]\n", encoding="utf-8")
        (self.repo / "state.db").write_text("NEXUS_TEST_CLOCK_DRIFT_MS mentioned in a big db\n", encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("add", ".gitignore", "tests/conftest.py")
        self._git("commit", "-q", "-m", "base")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout

    def test_is_timing_repair_config_matches_by_name_anywhere(self) -> None:
        is_config = verification_module._is_timing_repair_config
        self.assertTrue(is_config("conftest.py"))
        self.assertTrue(is_config("tests/new/conftest.py"))
        self.assertTrue(is_config("__pycache__/conftest.py"))  # not filtered as generated here
        self.assertTrue(is_config("tox.ini"))
        self.assertTrue(is_config("setup.cfg"))
        self.assertFalse(is_config("tests/tox.ini"))  # root configs only
        self.assertFalse(is_config("tests/test_conftest.py"))
        self.assertFalse(is_config("app.py"))

    def test_protected_scan_sees_ignored_files_and_marker_files(self) -> None:
        scanned = verification_module._timing_repair_protected_paths(str(self.repo), None)
        self.assertIsNotNone(scanned)
        protected, marker_files = scanned
        # The tracked drift fixture, the ignored conftest and the ignored
        # root config are all protected, ignore rules notwithstanding.
        self.assertIn("tests/conftest.py", protected)
        self.assertIn("ignored/conftest.py", protected)
        self.assertIn("tox.ini", protected)
        self.assertEqual(marker_files, {"tests/conftest.py", "state.db"})

    def test_ignored_protected_paths_lists_only_what_the_snapshot_must_force_add(self) -> None:
        listed = verification_module._ignored_protected_paths(str(self.repo), None)
        self.assertEqual(
            set(listed), {"ignored/conftest.py", "tox.ini", "state.db"},
        )


if __name__ == "__main__":
    unittest.main()
