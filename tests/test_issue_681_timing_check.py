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

    def test_suite_process_is_untouched_without_the_variable(self) -> None:
        import time

        self.assertFalse(hasattr(time.monotonic, "__wrapped__"))
        self.assertFalse(hasattr(time.perf_counter, "__wrapped__"))
        self.assertFalse(hasattr(time.time, "__wrapped__"))


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
        self.assertEqual(calls[0][timing_check.CLOCK_DRIFT_ENV_VAR], "10")
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
            changed_paths_from_diff(FIXTURE_DIFF), self.root, max_files=40,
        )
        self.assertEqual(affected.changed, ["tests/test_new_budget.py"])
        self.assertEqual(affected.files, [
            "tests/test_new_budget.py",
            "tests/test_imports_from_package.py",
            "tests/test_imports_module.py",
            "tests/test_imports_plain.py",
        ])
        self.assertEqual(affected.dropped, [])

    def test_cap_keeps_changed_tests_first(self) -> None:
        affected = select_affected_test_files(
            ["tests/test_new_budget.py", "modules/budget.py"], self.root, max_files=2,
        )
        self.assertEqual(affected.files[0], "tests/test_new_budget.py")
        self.assertEqual(len(affected.files), 2)
        self.assertEqual(len(affected.dropped), 2)

    def test_missing_files_and_non_python_changes_select_nothing(self) -> None:
        affected = select_affected_test_files(
            ["README.md", "tests/test_removed.py", "docs/x.py"], self.root, max_files=40,
        )
        self.assertEqual(affected.files, [])

    def test_drift_defaults_to_the_measured_value(self) -> None:
        self.assertEqual(timing_check.drift_from_env({}), 10.0)
        self.assertEqual(timing_check.drift_from_env({"NEXUS_TIMING_CHECK_DRIFT_MS": "50"}), 50.0)
        self.assertEqual(timing_check.drift_from_env({"NEXUS_TIMING_CHECK_DRIFT_MS": "x"}), 10.0)

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
        self.assertIn("timing check (NEXUS_TEST_CLOCK_DRIFT_MS=10)", result.test_output)

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


if __name__ == "__main__":
    unittest.main()
