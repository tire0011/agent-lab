"""Slow-machine check before merge (#681).

#675 passed on the VM and turned CI on main red: a token budget derived from
wall-clock deltas came out one token off on the slower runner. The check
reruns the affected tests with ``NEXUS_TEST_CLOCK_DRIFT_MS`` and fails the
verification with ``timing_sensitive: <ids>`` when a test only fails there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import timing_check
from modules.experiment_service import ExperimentService, STATUS_VERIFYING
from modules.timing_check import (
    PytestRun,
    changed_paths_from_diff,
    run_timing_check,
    select_affected_test_files,
)

REAL_CONFTEST = Path(__file__).resolve().parent / "conftest.py"

# A budget computed from the time the work took: exactly what #675 did.
BUDGET_MODULE = '''\
import time


def budget_from_wall_clock(total_seconds=10.0, tokens_per_second=100.0):
    started = time.monotonic()
    elapsed = time.monotonic() - started
    return round((total_seconds - elapsed) * tokens_per_second)


def budget_from_clock(clock, total_seconds=10.0, tokens_per_second=100.0):
    started = clock()
    elapsed = clock() - started
    return round((total_seconds - elapsed) * tokens_per_second)
'''

WALL_CLOCK_TEST = '''\
import unittest

from budget import budget_from_wall_clock


class WallClockBudget(unittest.TestCase):
    def test_budget(self):
        self.assertEqual(budget_from_wall_clock(), 1000)
'''

INJECTED_CLOCK_TEST = '''\
import unittest

from budget import budget_from_clock


class InjectedClockBudget(unittest.TestCase):
    def test_budget(self):
        self.assertEqual(budget_from_clock(lambda: 42.0), 1000)
'''

SENSITIVE_ID = "tests/test_wall_clock_budget.py::WallClockBudget::test_budget"

FIXTURE_DIFF = """\
diff --git a/modules/budget.py b/modules/budget.py
index 1111111..2222222 100644
--- a/modules/budget.py
+++ b/modules/budget.py
@@ -1 +1 @@
-A = 1
+A = 2
diff --git a/tests/test_new_budget.py b/tests/test_new_budget.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_new_budget.py
@@ -0,0 +1 @@
+import unittest
diff --git a/tests/test_removed.py b/tests/test_removed.py
deleted file mode 100644
index 4444444..0000000
--- a/tests/test_removed.py
+++ /dev/null
@@ -1 +0,0 @@
-import unittest
diff --git a/README.md b/README.md
index 5555555..6666666 100644
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
"""


def _sample_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    shutil.copy(REAL_CONFTEST, repo / "tests" / "conftest.py")
    (repo / "budget.py").write_text(BUDGET_MODULE, encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\ntestpaths = tests\npythonpath = .\n", encoding="utf-8")
    (repo / "tests" / "test_wall_clock_budget.py").write_text(WALL_CLOCK_TEST, encoding="utf-8")
    (repo / "tests" / "test_injected_clock_budget.py").write_text(INJECTED_CLOCK_TEST, encoding="utf-8")
    return repo


def _pytest(repo: Path, target: str, drift: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != timing_check.CLOCK_DRIFT_ENV_VAR}
    if drift is not None:
        env[timing_check.CLOCK_DRIFT_ENV_VAR] = drift
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        cwd=repo, env=env, capture_output=True, text=True, timeout=120,
    )


class ClockDriftPluginTests(unittest.TestCase):
    """tests/conftest.py speeds the clock up only when asked to."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _sample_repo(Path(self.tmp.name))

    def test_wall_clock_budget_passes_without_drift_and_fails_with_it(self) -> None:
        self.assertEqual(_pytest(self.repo, "tests/test_wall_clock_budget.py", None).returncode, 0)
        drifted = _pytest(self.repo, "tests/test_wall_clock_budget.py", "50")
        self.assertEqual(drifted.returncode, 1, drifted.stdout)
        self.assertIn("995", drifted.stdout)

    def test_injected_clock_passes_both_ways(self) -> None:
        for drift in (None, "50"):
            with self.subTest(drift=drift):
                run = _pytest(self.repo, "tests/test_injected_clock_budget.py", drift)
                self.assertEqual(run.returncode, 0, run.stdout)

    @unittest.skipIf(os.environ.get(timing_check.CLOCK_DRIFT_ENV_VAR),
                     "this process runs with drift on purpose (the #681 check itself)")
    def test_suite_process_is_untouched_without_the_variable(self) -> None:
        import time

        self.assertFalse(hasattr(time.monotonic, "__wrapped__"))
        self.assertFalse(hasattr(time.perf_counter, "__wrapped__"))
        self.assertFalse(hasattr(time.time, "__wrapped__"))


REAL_CLOCK_TEST = '''\
import unittest

import pytest

from budget import budget_from_wall_clock


class MarkedBudget(unittest.TestCase):
    @pytest.mark.real_clock(reason="measures real time on purpose")
    def test_a_marked(self):
        self.assertEqual(budget_from_wall_clock(), 1000)

    def test_b_unmarked(self):
        self.assertEqual(budget_from_wall_clock(), 1000)
'''


class RealClockMarkerTests(unittest.TestCase):
    """``@pytest.mark.real_clock`` pauses the drift for that test only."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _sample_repo(Path(self.tmp.name))
        (self.repo / "tests" / "test_real_clock.py").write_text(REAL_CLOCK_TEST, encoding="utf-8")

    def test_marked_test_passes_and_drift_resumes_for_the_next_test(self) -> None:
        drifted = _pytest(self.repo, "tests/test_real_clock.py", "50")
        self.assertIn("test_b_unmarked", drifted.stdout)
        self.assertNotIn("FAILED tests/test_real_clock.py::MarkedBudget::test_a_marked", drifted.stdout)
        self.assertIn("1 failed, 1 passed", drifted.stdout)
        self.assertEqual(_pytest(self.repo, "tests/test_real_clock.py", None).returncode, 0)

    def test_check_flags_only_the_unmarked_test(self) -> None:
        result = run_timing_check(sys.executable, str(self.repo), ["tests/test_real_clock.py"],
                                  timeout_seconds=120)
        self.assertEqual(result.timing_sensitive,
                         ["tests/test_real_clock.py::MarkedBudget::test_b_unmarked"], result.output)


class RunTimingCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _sample_repo(Path(self.tmp.name))

    def test_flags_only_the_wall_clock_test(self) -> None:
        result = run_timing_check(
            sys.executable, str(self.repo),
            ["tests/test_injected_clock_budget.py", "tests/test_wall_clock_budget.py"],
            timeout_seconds=120,
        )
        self.assertEqual(result.status, "timing_sensitive", result.output)
        self.assertEqual(result.timing_sensitive, [SENSITIVE_ID])
        self.assertTrue(result.failed)
        message = result.error_message()
        self.assertTrue(message.startswith(f"timing_sensitive: {SENSITIVE_ID}"))
        self.assertIn("Inject the clock", message)
        self.assertIn("configured values", message)

    def test_injected_clock_passes(self) -> None:
        result = run_timing_check(
            sys.executable, str(self.repo), ["tests/test_injected_clock_budget.py"],
            timeout_seconds=120,
        )
        self.assertEqual(result.status, "passed", result.output)
        self.assertFalse(result.failed)

    def test_failure_without_drift_too_is_not_timing_sensitive(self) -> None:
        calls: list[dict[str, str]] = []

        def runner(command, cwd, env, timeout):
            calls.append(dict(env))
            output = "FAILED tests/test_x.py::T::test_a - AssertionError\n"
            return PytestRun(1, output, ["tests/test_x.py::T::test_a"])

        result = run_timing_check("python", ".", ["tests/test_x.py"], runner=runner)
        self.assertEqual(result.status, "passed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][timing_check.CLOCK_DRIFT_ENV_VAR], "50")
        self.assertNotIn(timing_check.CLOCK_DRIFT_ENV_VAR, calls[1])

    def test_unparsable_failure_is_inconclusive_not_a_failure(self) -> None:
        result = run_timing_check(
            "python", ".", ["tests/test_x.py"],
            runner=lambda *args: PytestRun(4, "usage error", []),
        )
        self.assertEqual(result.status, "inconclusive")
        self.assertFalse(result.failed)

    def test_timeout_is_inconclusive(self) -> None:
        result = run_timing_check(
            "python", ".", ["tests/test_x.py"],
            runner=lambda *args: PytestRun(None, "", [], timed_out=True, error="timed out"),
        )
        self.assertEqual(result.status, "inconclusive")

    def test_partial_control_run_is_inconclusive(self) -> None:
        calls = 0

        def runner(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return PytestRun(1, f"FAILED {SENSITIVE_ID} - mismatch\n", [SENSITIVE_ID])
            return PytestRun(None, "halfway", [], error="runner crashed")

        result = run_timing_check("python", ".", ["tests/test_x.py"], runner=runner)
        self.assertEqual(result.status, "inconclusive")
        self.assertIn("runner crashed", result.detail)

    def test_interruption_is_inconclusive_not_a_pass(self) -> None:
        for exc in (KeyboardInterrupt(), SystemExit(1)):
            result = run_timing_check(
                "python", ".", ["tests/test_x.py"],
                runner=lambda *args, exc=exc: (_ for _ in ()).throw(exc),
            )
            self.assertEqual(result.status, "inconclusive")
            self.assertTrue(result.detail.startswith(timing_check.INTERRUPTED_DETAIL), result.detail)
            self.assertFalse(result.failed)

    def test_interruption_during_control_run_is_inconclusive(self) -> None:
        calls = 0

        def runner(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return PytestRun(1, f"FAILED {SENSITIVE_ID} - mismatch\n", [SENSITIVE_ID])
            raise KeyboardInterrupt()

        result = run_timing_check("python", ".", ["tests/test_x.py"], runner=runner)
        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.timing_sensitive, [])

    def test_missing_pytest_is_inconclusive(self) -> None:
        result = run_timing_check(
            str(Path(self.tmp.name) / "no-such-python"), str(self.repo),
            ["tests/test_injected_clock_budget.py"], timeout_seconds=30,
        )
        self.assertEqual(result.status, "inconclusive")
        self.assertIn("could not start pytest", result.detail)

    def test_interrupted_pytest_child_is_killed(self) -> None:
        """An interruption while pytest runs kills its process group and clears the handle."""
        (self.repo / "tests" / "test_sleeps.py").write_text(
            "import time\n\ndef test_sleep():\n    time.sleep(60)\n", encoding="utf-8",
        )
        handles: list = []

        def on_process(process):
            handles.append(process)
            if process is not None:
                # Simulate Ctrl-C arriving while communicate() waits.
                def interrupt(*args, **kwargs):
                    raise KeyboardInterrupt()
                original = process.communicate
                calls = {"n": 0}

                def communicate(*args, **kwargs):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return interrupt()
                    return original(*args, **kwargs)
                process.communicate = communicate

        result = run_timing_check(
            sys.executable, str(self.repo), ["tests/test_sleeps.py"],
            timeout_seconds=120, on_process=on_process,
        )
        self.assertEqual(result.status, "inconclusive")
        self.assertTrue(result.detail.startswith("interrupted"))
        started = handles[0]
        self.assertIsNotNone(started.returncode)  # reaped, not left running
        self.assertIsNone(handles[-1])  # the tracked handle was cleared

    def test_repeated_runs_are_independent_and_complete(self) -> None:
        def runner(*args):
            return PytestRun(0, "1 passed", [])

        first = run_timing_check("python", ".", ["tests/test_x.py"], runner=runner)
        second = run_timing_check("python", ".", ["tests/test_x.py"], runner=runner)
        self.assertEqual((first.status, second.status), ("passed", "passed"))

    def test_no_files_is_skipped(self) -> None:
        self.assertEqual(run_timing_check("python", ".", []).status, "skipped")

    def test_parses_pytest_short_summary(self) -> None:
        output = (
            "F.\n=== short test summary info ===\n"
            "FAILED tests/test_a.py::A::test_x - AssertionError: 3776 != 3775\n"
            "ERROR tests/test_b.py::test_y\n"
            "FAILED tests/test_a.py::A::test_x - again\n"
        )
        self.assertEqual(
            timing_check.parse_pytest_failures(output),
            ["tests/test_a.py::A::test_x", "tests/test_b.py::test_y"],
        )


class AffectedFileSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        files = {
            "modules/__init__.py": "",
            "modules/budget.py": "A = 2\n",
            "modules/other.py": "B = 1\n",
            "tests/test_new_budget.py": "import unittest\n",
            "tests/test_imports_module.py": "from modules.budget import A\n",
            "tests/test_imports_from_package.py": "from modules import budget\n",
            "tests/test_imports_plain.py": "def f():\n    import modules.budget\n",
            "tests/test_unrelated.py": "from modules.other import B\n",
            "tests/test_broken_syntax.py": "def (:\n",
            "tests/helper_budget.py": "from modules.budget import A\n",
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def test_paths_from_fixture_diff(self) -> None:
        self.assertEqual(
            changed_paths_from_diff(FIXTURE_DIFF),
            ["modules/budget.py", "tests/test_new_budget.py", "README.md"],
        )

    def test_selects_changed_tests_and_importers_from_fixture_diff(self) -> None:
        affected = select_affected_test_files(
            changed_paths_from_diff(FIXTURE_DIFF), self.root,
        )
        self.assertEqual(affected.changed, ["tests/test_new_budget.py"])
        self.assertEqual(affected.files, [
            "tests/test_new_budget.py",
            "tests/test_imports_from_package.py",
            "tests/test_imports_module.py",
            "tests/test_imports_plain.py",
        ])
        self.assertTrue(any("test_broken_syntax.py" in item for item in affected.discovery_errors))

    def test_there_is_no_cap_argument_and_no_dropped_list(self) -> None:
        with self.assertRaises(TypeError):
            select_affected_test_files(["modules/budget.py"], self.root, max_files=2)  # type: ignore[call-arg]
        affected = select_affected_test_files(["modules/budget.py"], self.root)
        self.assertFalse(hasattr(affected, "dropped"))

    def test_many_changed_tests_and_importers_are_all_selected(self) -> None:
        changed = []
        for number in range(30):
            relative = f"tests/test_changed_{number}.py"
            (self.root / relative).write_text("def test_ok(): pass\n", encoding="utf-8")
            changed.append(relative)
        importers = []
        for number in range(30):
            relative = f"tests/test_importer_{number}.py"
            (self.root / relative).write_text("import modules.budget\n", encoding="utf-8")
            importers.append(relative)
        affected = select_affected_test_files([*changed, "modules/budget.py"], self.root)
        self.assertEqual(affected.changed, sorted(changed))
        self.assertTrue(set(importers) <= set(affected.importers))
        self.assertEqual(set(affected.files), set(changed) | set(affected.importers))
        self.assertGreaterEqual(len(affected.files), 63)

    def test_selection_runs_twice_with_the_same_result(self) -> None:
        paths = changed_paths_from_diff(FIXTURE_DIFF)
        first = select_affected_test_files(paths, self.root)
        second = select_affected_test_files(paths, self.root)
        self.assertEqual(first, second)

    def test_more_than_forty_changed_tests_are_all_selected(self) -> None:
        paths = []
        for number in range(45):
            relative = f"tests/test_changed_{number}.py"
            (self.root / relative).write_text("def test_ok(): pass\n", encoding="utf-8")
            paths.append(relative)
        affected = select_affected_test_files(paths, self.root)
        self.assertEqual(len(affected.files), 45)
        self.assertEqual(set(affected.files), set(paths))

    def test_missing_files_and_non_python_changes_select_nothing(self) -> None:
        affected = select_affected_test_files(
            ["README.md", "tests/test_removed.py", "docs/x.py"], self.root,
        )
        self.assertEqual(affected.files, [])

    def test_drift_defaults_to_50_ms_and_can_be_overridden(self) -> None:
        self.assertEqual(timing_check.DEFAULT_DRIFT_MS, 50.0)
        self.assertEqual(timing_check.drift_from_env({}), 50.0)
        self.assertEqual(timing_check.drift_from_env({"NEXUS_TIMING_CHECK_DRIFT_MS": "x"}), 50.0)
        self.assertEqual(timing_check.drift_from_env({"NEXUS_TIMING_CHECK_DRIFT_MS": "0"}), 50.0)
        self.assertEqual(timing_check.drift_from_env({"NEXUS_TIMING_CHECK_DRIFT_MS": "75"}), 75.0)

    def test_default_run_uses_50_ms_drift(self) -> None:
        seen: list[str] = []

        def runner(command, cwd, env, timeout):
            seen.append(env[timing_check.CLOCK_DRIFT_ENV_VAR])
            return PytestRun(0, "1 passed", [])

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEXUS_TIMING_CHECK_DRIFT_MS", None)
            result = run_timing_check("python", ".", ["tests/test_x.py"], runner=runner)
        self.assertEqual(seen, ["50"])
        self.assertEqual(result.drift_ms, 50.0)

    def test_switch_defaults_on(self) -> None:
        self.assertTrue(timing_check.timing_check_enabled({}))
        self.assertTrue(timing_check.timing_check_enabled({"NEXUS_TIMING_CHECK": "1"}))
        self.assertFalse(timing_check.timing_check_enabled({"NEXUS_TIMING_CHECK": "0"}))


class VerifyReportsTimingSensitiveTests(unittest.TestCase):
    """The verification step fails a change whose tests only pass on a fast clock."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = _sample_repo(root)
        # The built-in suite is ``unittest discover -s tests``: modules beside the tests.
        shutil.move(str(self.repo / "budget.py"), str(self.repo / "tests" / "budget.py"))
        (self.repo / "pytest.ini").write_text("[pytest]\ntestpaths = tests\npythonpath = tests\n",
                                              encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")
        self.service = ExperimentService(
            db_path=str(root / "db.sqlite"), repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"), test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"), max_repair_iterations=0,
            worker_factory=lambda _: None, plan_worker_factory=lambda _: None,
        )
        self.addCleanup(self.service.close)

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout

    def _verify(self):
        experiment = self.service.create_experiment("Compute the budget")
        sha = self._git("rev-parse", "HEAD").strip()
        self.service._force_status(experiment.id, STATUS_VERIFYING, base_commit_sha=sha)
        return self.service._verify(experiment.id, str(self.repo))

    def _touch(self, name: str) -> None:
        path = self.repo / "tests" / name
        path.write_text(path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")

    def test_wall_clock_change_fails_with_timing_sensitive(self) -> None:
        self._touch("test_wall_clock_budget.py")
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            result = self._verify()
        self.assertEqual(result.status, "failed", result.test_output)
        self.assertEqual(result.test_exit_code, 0)
        self.assertTrue(result.error.startswith(f"timing_sensitive: {SENSITIVE_ID}"), result.error)
        self.assertIn("Inject the clock", result.error)
        self.assertIn("timing check (NEXUS_TEST_CLOCK_DRIFT_MS=50)", result.test_output)

    def test_injected_clock_change_passes(self) -> None:
        self._touch("test_injected_clock_budget.py")
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            result = self._verify()
        self.assertNotEqual(result.status, "failed", result.error)
        self.assertFalse((result.error or "").startswith("timing_sensitive"))

    def test_disabled_check_runs_no_second_time(self) -> None:
        self._touch("test_wall_clock_budget.py")
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "0"}), \
                patch.object(timing_check, "run_timing_check") as rerun:
            result = self._verify()
        rerun.assert_not_called()
        self.assertFalse((result.error or "").startswith("timing_sensitive"))

    def test_inconclusive_check_fails_closed(self) -> None:
        self._touch("test_injected_clock_budget.py")
        inconclusive = timing_check.TimingCheckResult(
            timing_check.STATUS_INCONCLUSIVE, detail="pytest missing",
        )
        with patch.object(self.service, "_run_timing_check", return_value=inconclusive):
            result = self._verify()
        self.assertEqual(result.status, "needs_review")
        self.assertIn("timing check inconclusive: pytest missing", result.error)

    def test_check_crash_fails_closed(self) -> None:
        self._touch("test_injected_clock_budget.py")
        with patch.object(timing_check, "select_affected_test_files", side_effect=RuntimeError("boom")):
            result = self._verify()
        self.assertEqual(result.status, "needs_review")
        self.assertIn("timing check crashed: boom", result.error)

    def test_interruption_fails_closed_and_leaves_no_experiment_in_verifying(self) -> None:
        self._touch("test_injected_clock_budget.py")
        for exc in (KeyboardInterrupt, SystemExit):
            with self.subTest(exc=exc.__name__), patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}), \
                    patch.object(timing_check, "run_timing_check", side_effect=exc()):
                result = self._verify()
            self.assertEqual(result.status, "needs_review", result.error)
            self.assertIn("timing check inconclusive: interrupted", result.error)
            stored = self.service.get_experiment(result.id)
            self.assertEqual(stored.status, "needs_review")
            pgid = self.service._conn.execute(
                "SELECT worker_pgid FROM experiments WHERE id = ?", (result.id,),
            ).fetchone()[0]
            self.assertIsNone(pgid)
            self.assertNotIn(result.id, self.service._active_processes)

    def test_interruption_during_discovery_fails_closed(self) -> None:
        self._touch("test_injected_clock_budget.py")
        with patch.object(timing_check, "select_affected_test_files", side_effect=KeyboardInterrupt()):
            result = self._verify()
        self.assertEqual(result.status, "needs_review")
        self.assertIn("interrupted: KeyboardInterrupt", result.error)

    def test_cleanup_failure_after_crash_still_fails_closed(self) -> None:
        self._touch("test_injected_clock_budget.py")
        original = self.service._track_process
        check_started = []

        def crash(*args, **kwargs):
            check_started.append(True)
            raise RuntimeError("boom")

        def track(experiment_id, process):
            if process is None and check_started:
                raise RuntimeError("db gone")
            return original(experiment_id, process)

        with patch.object(timing_check, "run_timing_check", side_effect=crash), \
                patch.object(self.service, "_track_process", side_effect=track):
            result = self._verify()
        self.assertEqual(result.status, "needs_review")
        self.assertIn("timing check crashed: boom", result.error)

    def test_discovery_error_fails_closed(self) -> None:
        self._touch("test_injected_clock_budget.py")
        (self.repo / "tests" / "test_injected_clock_budget.py").write_text(
            INJECTED_CLOCK_TEST + "# changed\n", encoding="utf-8")
        broken = timing_check.AffectedTests(
            files=["tests/test_injected_clock_budget.py"],
            discovery_errors=["tests/test_x.py: cannot parse imports"],
        )
        with patch.object(timing_check, "select_affected_test_files", return_value=broken), \
                patch.object(timing_check, "run_timing_check") as rerun:
            result = self._verify()
        rerun.assert_not_called()
        self.assertEqual(result.status, "needs_review")
        self.assertIn("discovery incomplete", result.error)

    def test_verifying_twice_gives_the_same_verdict(self) -> None:
        self._touch("test_wall_clock_budget.py")
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            first = self._verify()
            second = self._verify()
        self.assertEqual((first.status, second.status), ("failed", "failed"))
        self.assertEqual(first.error, second.error)


def _load_replay_script():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "timing_check_replay.py"
    spec = importlib.util.spec_from_file_location("timing_check_replay_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ReplayScriptTests(unittest.TestCase):
    """scripts/timing_check_replay.py must run (the last attempt crashed on --help)."""

    def setUp(self) -> None:
        self.replay = _load_replay_script()

    def test_help_runs(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "timing_check_replay.py"
        completed = subprocess.run([sys.executable, str(script), "--help"],
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--drift-ms", completed.stdout)
        self.assertNotIn("--max-files", completed.stdout)

    def test_default_drift_is_50_ms(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEXUS_TIMING_CHECK_DRIFT_MS", None)
            args = self.replay.build_parser().parse_args([])
        self.assertEqual(args.drift_ms, 50.0)

    def test_max_files_option_is_gone(self) -> None:
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            self.replay.build_parser().parse_args(["--max-files", "40"])

    def _record(self, status, detail=""):
        return {"commit": "abc", "status": status, "detail": detail, "timing_sensitive": []}

    def _main(self, records, argv=("a", "b", "c")):
        calls = []

        def fake(commit, *, timeout, drift_ms):
            calls.append((commit, drift_ms))
            record = records[len(calls) - 1]
            if isinstance(record, BaseException):
                raise record
            return record

        with patch("sys.stdout"):
            code = self.replay.main(list(argv), replay_fn=fake)
        return code, calls

    def test_exit_codes(self) -> None:
        code, calls = self._main([self._record("passed"), self._record("skipped"),
                                  self._record("passed")])
        self.assertEqual((code, len(calls)), (0, 3))
        self.assertEqual(calls[0][1], 50.0)
        code, _ = self._main([self._record("passed"), self._record("timing_sensitive"),
                              self._record("passed")])
        self.assertEqual(code, 1)
        code, _ = self._main([self._record("inconclusive", "timed out"),
                              self._record("passed"), self._record("passed")])
        self.assertEqual(code, 1)

    def test_interruption_stops_the_replay(self) -> None:
        code, calls = self._main([self._record("inconclusive", "interrupted: KeyboardInterrupt"),
                                  self._record("passed"), self._record("passed")])
        self.assertEqual((code, len(calls)), (130, 1))
        code, calls = self._main([self._record("passed"), KeyboardInterrupt(),
                                  self._record("passed")])
        self.assertEqual((code, len(calls)), (130, 2))

    def test_plugin_is_added_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "tests").mkdir()
            (worktree / "tests" / "conftest.py").write_text("X = 1\n", encoding="utf-8")
            self.replay._ensure_plugin(worktree)
            self.replay._ensure_plugin(worktree)
            text = (worktree / "tests" / "conftest.py").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("X = 1\n"))
        self.assertEqual(text.count("def _install_clock_drift"), 1)


if __name__ == "__main__":
    unittest.main()
