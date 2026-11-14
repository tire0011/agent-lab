"""#689: the slow-machine check is part of verification and fails closed."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules import timing_check as timing_check_module
from modules.experiment_evaluation import EvaluationResult
from modules.experiment_service import ExperimentService, STATUS_VERIFYING
from modules.experiment_service.service_parts.verification import TIMING_CHECK_HINT

ROOT = Path(__file__).resolve().parents[1]

HINT = "inject the clock or compute the value from configured values, not from wall time"

# Passes in the normal run and without drift, fails only under the drift
# fixture: a budget computed from wall-clock deltas.
DRIFT_SENSITIVE = (
    "import time\n"
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_budget(self):\n"
    "        started = time.monotonic()\n"
    "        self.assertLess(time.monotonic() - started, 0.02)\n"
)
# Fails here whatever the clock does (like a missing test dependency).
ALWAYS_FAILING_UNDER_PYTEST = (
    "import os\n"
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_host(self):\n"
    "        self.assertNotIn('NEXUS_TEST_CLOCK_DRIFT_MS', os.environ)\n"
)
PLAIN = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        pass\n"
)


class TimingCheckVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "README.md").write_text("x\n", encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")
        self.worker_tasks: list[str] = []
        self.service = ExperimentService(
            db_path=str(root / "db.sqlite"), repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"), test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"), max_repair_iterations=1,
            worker_factory=lambda _: self._worker(), plan_worker_factory=lambda _: None,
        )
        # Keep the quality gate out of the way: these tests are about the timing check.
        quality = patch(
            "modules.experiment_service.evaluate_implementation",
            return_value=EvaluationResult(outcome="pass", reason="ok"),
        )
        quality.start()
        self.addCleanup(quality.stop)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout

    def _worker(self):
        worker = MagicMock()

        def run(*, task, **_kwargs):
            self.worker_tasks.append(task)
            return SimpleNamespace(usage=None, timed_out=False, error=None)

        worker.run.side_effect = run
        return worker

    def _verify(self, test_source: str, timing_check: str):
        (self.repo / "tests" / "test_new.py").write_text(test_source, encoding="utf-8")
        experiment = self.service.create_experiment("Add a test")
        base = self._git("rev-parse", "HEAD").strip()
        self.service._force_status(experiment.id, STATUS_VERIFYING, base_commit_sha=base)
        with patch.dict("os.environ", {"NEXUS_TIMING_CHECK": timing_check}):
            return experiment.id, self.service._verify(experiment.id, str(self.repo))

    def test_hint_text_is_the_one_from_the_issue(self) -> None:
        self.assertEqual(TIMING_CHECK_HINT, HINT)

    def test_disabled_check_makes_no_second_run(self) -> None:
        with patch("modules.timing_check.run_timing_check") as check, \
                patch("modules.timing_check.affected_tests") as selection:
            _, result = self._verify(DRIFT_SENSITIVE, "0")
        check.assert_not_called()
        selection.assert_not_called()
        self.assertEqual(result.status, "succeeded")

    def test_enabled_check_passes_a_clock_independent_test(self) -> None:
        self._install_drift_conftest()
        _, result = self._verify(PLAIN, "1")
        self.assertEqual(result.status, "succeeded", result.error)

    def test_flagged_test_fails_verification_with_the_hint(self) -> None:
        self._install_drift_conftest()
        _, result = self._verify(DRIFT_SENSITIVE, "1")
        self.assertEqual(result.status, "failed")
        self.assertIn("timing_sensitive: tests/test_new.py::T::test_budget", result.error)
        self.assertIn(HINT, result.error)

    def test_test_failing_without_drift_too_gives_needs_review(self) -> None:
        # E.g. CI passed but this host lacks a test dependency: no timing verdict.
        self._install_drift_conftest()
        _, result = self._verify(ALWAYS_FAILING_UNDER_PYTEST, "1")
        self.assertEqual(result.status, "needs_review")
        self.assertIn("fail on this host without drift too", result.error)
        self.assertIn("tests/test_new.py::T::test_host", result.error)

    def test_only_drift_only_tests_are_reported(self) -> None:
        self._install_drift_conftest()
        (self.repo / "tests" / "test_host.py").write_text(
            ALWAYS_FAILING_UNDER_PYTEST, encoding="utf-8",
        )
        _, result = self._verify(DRIFT_SENSITIVE, "1")
        self.assertEqual(result.status, "failed")
        self.assertIn("timing_sensitive: tests/test_new.py::T::test_budget; hint:", result.error)
        self.assertNotIn("test_host", result.error)

    def test_retry_task_receives_the_hint(self) -> None:
        self._install_drift_conftest()
        experiment_id, result = self._verify(DRIFT_SENSITIVE, "1")
        elog = self.service.get_experiment_logger(experiment_id)
        with patch.dict("os.environ", {"NEXUS_TIMING_CHECK": "1"}):
            self.service._repair_failed_verification(
                experiment_id, str(self.repo), elog, result,
            )
        self.assertEqual(len(self.worker_tasks), 1)
        self.assertIn(HINT, self.worker_tasks[0])

    def test_check_error_gives_needs_review(self) -> None:
        with patch("modules.timing_check.run_timing_check", return_value="error") as check:
            _, result = self._verify(PLAIN, "1")
        check.assert_called_once()
        self.assertEqual(check.call_args.args[1], ["tests/test_new.py"])
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.error, "timing check failed closed: error")

    def test_interruption_and_crash_give_needs_review(self) -> None:
        for raised in (KeyboardInterrupt(), SystemExit(1), RuntimeError("boom")):
            with self.subTest(raised=type(raised).__name__), \
                    patch("modules.timing_check.run_timing_check", side_effect=raised):
                _, result = self._verify(PLAIN, "1")
            self.assertEqual(result.status, "needs_review")
            self.assertTrue(result.error.startswith("timing check failed closed: error"))

    def test_interruption_or_crash_in_the_control_run_gives_needs_review(self) -> None:
        # The first run flagged a test, then the control run without drift breaks.
        for raised in (KeyboardInterrupt(), RuntimeError("boom")):
            with self.subTest(raised=type(raised).__name__), \
                    patch("modules.timing_check.run_timing_check",
                          return_value="timing_sensitive: tests/test_new.py::T::test_ok"), \
                    patch("modules.timing_check.control_check", side_effect=raised):
                _, result = self._verify(PLAIN, "1")
            self.assertEqual(result.status, "needs_review")
            self.assertTrue(result.error.startswith("timing check failed closed: error"))
            self.assertNotIn(HINT, result.error)

    def test_control_run_without_verdict_gives_needs_review(self) -> None:
        with patch("modules.timing_check.run_timing_check",
                   return_value="timing_sensitive: tests/test_new.py::T::test_ok"), \
                patch("modules.timing_check.control_check", return_value=None):
            _, result = self._verify(PLAIN, "1")
        self.assertEqual(result.status, "needs_review")
        self.assertIn("control run without drift gave no verdict", result.error)

    def test_crash_while_selecting_tests_gives_needs_review(self) -> None:
        with patch("modules.timing_check.affected_tests", side_effect=OSError("disk")), \
                patch("modules.timing_check.run_timing_check") as check:
            _, result = self._verify(PLAIN, "1")
        check.assert_not_called()
        self.assertEqual(result.status, "needs_review")
        self.assertIn("OSError: disk", result.error)

    def test_running_verification_twice_gives_the_same_verdict(self) -> None:
        self._install_drift_conftest()
        first_id, first = self._verify(DRIFT_SENSITIVE, "1")
        with patch.dict("os.environ", {"NEXUS_TIMING_CHECK": "1"}):
            self.service._force_status(first_id, STATUS_VERIFYING)
            second = self.service._verify(first_id, str(self.repo))
        self.assertEqual((first.status, first.error), (second.status, second.error))
        self.assertEqual(second.status, "failed")
        # No pytest child is left tracked after either run.
        self.assertNotIn(first_id, self.service._active_processes)

    def test_unset_variable_means_on(self) -> None:
        with patch("modules.timing_check.run_timing_check", return_value="ok") as check, \
                patch.dict("os.environ"):
            import os
            os.environ.pop("NEXUS_TIMING_CHECK", None)
            (self.repo / "tests" / "test_new.py").write_text(PLAIN, encoding="utf-8")
            experiment = self.service.create_experiment("Add a test")
            result = self.service._verify(experiment.id, str(self.repo))
        check.assert_called_once()
        self.assertEqual(result.status, "succeeded")

    def _install_drift_conftest(self) -> None:
        # Copy only the drift fixture block; the full conftest also isolates
        # the suite, which is irrelevant for a throwaway repository.
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import timing_check_replay
        finally:
            sys.path.pop(0)
        self.assertTrue(timing_check_replay.ensure_plugin(self.repo, ROOT))
        self._git("add", "tests/conftest.py")
        self._git("commit", "-q", "-m", "drift fixture")


class ControlCheckTests(unittest.TestCase):
    def test_separates_drift_only_from_failing_anyway(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import timing_check_replay as replay
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            replay.ensure_plugin(repo, ROOT)
            (repo / "tests" / "test_x.py").write_text(
                DRIFT_SENSITIVE
                + "class U(unittest.TestCase):\n"
                "    def test_host(self):\n"
                "        import os\n"
                "        self.assertNotIn('NEXUS_TEST_CLOCK_DRIFT_MS', os.environ)\n",
                encoding="utf-8",
            )
            first = timing_check_module.run_timing_check(repo, ["tests/test_x.py"])
            flagged = timing_check_module.flagged_tests(first)
            self.assertEqual(
                flagged, ["tests/test_x.py::T::test_budget", "tests/test_x.py::U::test_host"],
            )
            self.assertEqual(
                timing_check_module.control_check(repo, flagged),
                (["tests/test_x.py::T::test_budget"], ["tests/test_x.py::U::test_host"]),
            )

    def test_no_verdict_from_the_control_run_is_none(self) -> None:
        with patch("modules.timing_check.run_timing_check", return_value="error"):
            self.assertIsNone(timing_check_module.control_check(".", ["t.py::a"]))


class ReplayScriptTests(unittest.TestCase):
    def test_help_works(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "timing_check_replay.py"), "--help"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--last", proc.stdout)

    def test_verdict_matches_verification(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import timing_check_replay as replay
        finally:
            sys.path.pop(0)
        self.assertEqual(replay.verdict("ok", None), "ok")
        self.assertEqual(replay.verdict("timing_sensitive: a", (["a"], [])), "failed")
        self.assertEqual(replay.verdict("timing_sensitive: a", ([], ["a"])), "needs_review")
        self.assertEqual(replay.verdict("error", None), "needs_review")
        self.assertFalse(replay.is_flagged({"verdict": "ok"}))
        self.assertTrue(replay.is_flagged({"verdict": "needs_review"}))
        self.assertTrue(replay.is_flagged({}))

    def test_exit_codes(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import timing_check_replay as replay
        finally:
            sys.path.pop(0)
        results = {"a": "ok", "b": "failed"}

        def fake(commit, **_kwargs):
            return {"commit": commit, "verdict": results[commit]}

        with patch("builtins.print"):
            self.assertEqual(replay.main(["a"], check_fn=fake), replay.EXIT_OK)
            self.assertEqual(replay.main(["a", "b"], check_fn=fake), replay.EXIT_FLAGGED)

            def interrupted(commit, **_kwargs):
                raise KeyboardInterrupt

            self.assertEqual(replay.main(["a"], check_fn=interrupted), replay.EXIT_INTERRUPTED)

    def _replay(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import timing_check_replay as replay
        finally:
            sys.path.pop(0)
        return replay

    def test_a_crashing_commit_is_needs_review_and_the_rest_still_runs(self) -> None:
        replay = self._replay()
        seen: list[str] = []

        def fake(commit, **_kwargs):
            seen.append(commit)
            if commit == "bad":
                raise subprocess.CalledProcessError(128, ["git", "rev-parse"])
            return {"commit": commit, "verdict": "ok"}

        with tempfile.TemporaryDirectory() as tmp, patch("builtins.print"):
            out = Path(tmp) / "r.jsonl"
            code = replay.main(["a", "bad", "c", "--out", str(out)], check_fn=fake)
            lines = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(code, replay.EXIT_FLAGGED)
        self.assertEqual(seen, ["a", "bad", "c"])
        self.assertEqual([r["verdict"] for r in lines], ["ok", "needs_review", "ok"])
        self.assertIn("replay crashed: CalledProcessError", lines[1]["result"])

    def test_interrupted_replay_keeps_what_it_measured(self) -> None:
        replay = self._replay()

        def fake(commit, **_kwargs):
            if commit == "b":
                raise KeyboardInterrupt
            return {"commit": commit, "verdict": "ok"}

        with tempfile.TemporaryDirectory() as tmp, patch("builtins.print") as printed:
            out = Path(tmp) / "r.jsonl"
            code = replay.main(["a", "b", "c", "--out", str(out)], check_fn=fake)
            kept = out.read_text().splitlines()
        self.assertEqual(code, replay.EXIT_INTERRUPTED)
        self.assertEqual([json.loads(line)["commit"] for line in kept], ["a"])
        summary = json.loads(printed.call_args_list[-1].args[0])["summary"]
        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["commits"], 1)

    def test_running_the_replay_twice_appends_not_corrupts(self) -> None:
        replay = self._replay()
        fake = lambda commit, **_kwargs: {"commit": commit, "verdict": "ok"}  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp, patch("builtins.print"):
            out = Path(tmp) / "r.jsonl"
            replay.main(["a", "--out", str(out)], check_fn=fake)
            replay.main(["a", "--out", str(out)], check_fn=fake)
            lines = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(lines, [{"commit": "a", "verdict": "ok"}] * 2)

    def test_summary_counts_false_positives_only_on_green_commits(self) -> None:
        replay = self._replay()
        records = [
            {"commit": "67c002980411", "verdict": "failed"},
            {"commit": "a8d1e692979a", "verdict": "failed"},
            {"commit": "adf8f94cd211", "verdict": "needs_review"},
            {"commit": "7fd3573676fd", "verdict": "ok"},
        ]
        summary = replay.summarize(
            records, ["a8d1e692979ae41f3eb7f916d99c05e0c9390110", "adf8f94", "7fd3573676fd"],
        )
        self.assertEqual(summary["commits"], 4)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["green_commits"], 3)
        self.assertEqual(summary["false_positives"], 1)
        self.assertEqual(summary["green_needs_review"], 1)


if __name__ == "__main__":
    unittest.main()
