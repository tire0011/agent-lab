from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.experiment_service import ExperimentService, STATUS_VERIFYING
from modules.experiment_service.service import _test_file_for_id
from modules.test_progress import parse_failed_test_ids
from modules.verification_runner import VerificationRunResult, run_verification_test_monitored

RUNNER = "modules.experiment_service.service.run_verification_test_monitored"
TEST_A = "test_sample.T.test_a"


def _source(*methods: str) -> str:
    return "import unittest\nclass T(unittest.TestCase):\n" + "".join(
        f"    {method}\n" for method in methods
    )


PASSING = _source("def test_a(self): pass")
FAILING = _source("def test_a(self): self.fail('already broken')")


def _stalled(test_id: str = TEST_A) -> VerificationRunResult:
    return VerificationRunResult(
        None, "", "stalled", True, None, 1, stalled=True,
        failed_test_ids=[test_id], aborted_test_id=test_id,
        observed_test_ids=[test_id],
    )


class BaselineRecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "app.py").write_text("VALUE = 'good'\n", encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self.service = ExperimentService(
            db_path=str(root / "db.sqlite"), repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"), test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"), max_repair_iterations=0,
            worker_factory=lambda _: None, plan_worker_factory=lambda _: None,
        )
        self.temp_parent = root / "temp"
        self.temp_parent.mkdir()
        tempdir = patch.object(tempfile, "tempdir", str(self.temp_parent))
        tempdir.start()
        self.addCleanup(tempdir.stop)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout

    def _write_test(self, source: str, name: str = "test_sample.py") -> None:
        path = self.repo / "tests" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def _commit(self) -> None:
        self._git("add", ".")
        self._git("commit", "-q", "-m", "commit")

    def _change_unrelated_file(self) -> None:
        (self.repo / "notes.txt").write_text("unrelated\n", encoding="utf-8")

    def _verify(self, *, base: str | None = "HEAD"):
        experiment = self.service.create_experiment("Change app behavior")
        sha = self._git("rev-parse", base).strip() if base else None
        self.service._force_status(experiment.id, STATUS_VERIFYING, base_commit_sha=sha)
        return self.service._verify(experiment.id, str(self.repo))

    def _assert_no_base_worktree_left(self) -> None:
        self.assertNotIn("nexus-baseline-", self._git("worktree", "list", "--porcelain"))
        self.assertEqual(list(self.temp_parent.glob("nexus-baseline-*")), [])

    def test_pre_existing_failure_becomes_needs_review_and_worktree_is_removed(self) -> None:
        self._write_test(FAILING)
        self._commit()
        self._change_unrelated_file()

        result = self._verify()

        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.baseline_test_classifications, {TEST_A: "pre_existing"})
        self.assertEqual(result.error, f"pre-existing failures on base: {TEST_A}")
        self._assert_no_base_worktree_left()

    def test_test_that_passes_on_base_remains_a_regression(self) -> None:
        self._write_test(_source(
            "def test_b(self):",
            "    from app import VALUE",
            "    self.assertEqual(VALUE, 'good')",
        ))
        self._commit()
        (self.repo / "app.py").write_text("VALUE = 'bad'\n", encoding="utf-8")

        result = self._verify()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "tests failed (exit code 1)")
        self.assertEqual(
            result.baseline_test_classifications, {"test_sample.T.test_b": "regression"},
        )
        self._assert_no_base_worktree_left()

    def test_mixed_failures_keep_todays_outcome(self) -> None:
        self._write_test(_source(
            "def test_a(self): self.fail('already broken')",
            "def test_b(self):",
            "    from app import VALUE",
            "    self.assertEqual(VALUE, 'good')",
        ))
        self._commit()
        (self.repo / "app.py").write_text("VALUE = 'bad'\n", encoding="utf-8")

        result = self._verify()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.baseline_test_classifications, {
            TEST_A: "pre_existing", "test_sample.T.test_b": "regression",
        })

    def test_stall_on_base_is_pre_existing_within_deadline(self) -> None:
        self._write_test(PASSING)
        self._commit()
        self._change_unrelated_file()
        with patch(RUNNER, side_effect=[_stalled(), _stalled()]) as runner, patch.dict(
            os.environ, {"NEXUS_BASELINE_RECHECK_SECONDS": "7"},
        ):
            result = self._verify()

        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.baseline_test_classifications, {TEST_A: "pre_existing"})
        self.assertEqual(runner.call_args_list[1].kwargs["config"].safety_cap_seconds, 7)
        self._assert_no_base_worktree_left()

    def test_real_hanging_test_on_base_is_stopped_at_the_deadline(self) -> None:
        self._write_test(_source(
            "def test_a(self):",
            "    import time",
            "    time.sleep(600)",
        ))
        self._commit()
        base = self._git("rev-parse", "HEAD").strip()
        experiment = self.service.create_experiment("Change app behavior")
        started = time.monotonic()
        with patch.dict(os.environ, {"NEXUS_BASELINE_RECHECK_SECONDS": "2"}):
            classifications = self.service._baseline_recheck(
                experiment.id, str(self.repo), base, [TEST_A],
                self.service.get_experiment_logger(experiment.id),
            )

        self.assertEqual(classifications, {TEST_A: "pre_existing"})
        self.assertLess(time.monotonic() - started, 30)
        self._assert_no_base_worktree_left()

    def test_stall_that_passes_on_base_stays_timed_out(self) -> None:
        self._write_test(PASSING)
        self._commit()
        self._change_unrelated_file()
        passed = VerificationRunResult(
            0, "", "", False, None, 1, failed_test_ids=[], observed_test_ids=[TEST_A],
        )
        with patch(RUNNER, side_effect=[_stalled(), passed]):
            result = self._verify()

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.baseline_test_classifications, {TEST_A: "regression"})

    def test_stall_on_base_before_the_test_started_is_no_evidence(self) -> None:
        self._write_test(PASSING)
        self._commit()
        self._change_unrelated_file()
        never_started = _stalled()
        never_started.observed_test_ids = []
        with patch(RUNNER, side_effect=[_stalled(), never_started]):
            result = self._verify()

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.baseline_test_classifications, {TEST_A: "regression"})

    def test_stall_without_a_named_test_is_not_rechecked(self) -> None:
        self._write_test(PASSING)
        self._commit()
        self._change_unrelated_file()
        aborted = _stalled()
        aborted.aborted_test_id = None
        with patch(RUNNER, return_value=aborted) as runner:
            result = self._verify()

        self.assertEqual(result.status, "timed_out")
        self.assertIsNone(result.baseline_test_classifications)
        runner.assert_called_once()

    def test_changed_failing_test_file_skips_recheck(self) -> None:
        self._write_test(PASSING)
        self._commit()
        self._write_test(FAILING)

        with patch(RUNNER, wraps=run_verification_test_monitored) as runner:
            result = self._verify()

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.baseline_test_classifications)
        runner.assert_called_once()

    def test_failing_test_file_changed_in_a_checkpoint_commit_skips_recheck(self) -> None:
        self._write_test(PASSING)
        self._commit()
        base = self._git("rev-parse", "HEAD").strip()
        self._write_test(FAILING)
        self._commit()

        result = self._verify(base=base)

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.baseline_test_classifications)

    def test_untracked_failing_test_file_skips_recheck(self) -> None:
        self._write_test(PASSING)
        self._commit()
        self._write_test(FAILING, name="test_added.py")

        result = self._verify()

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.baseline_test_classifications)

    def test_unknown_base_commit_never_compares_the_change_with_itself(self) -> None:
        self._write_test(FAILING)
        self._commit()
        self._change_unrelated_file()

        result = self._verify(base=None)

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.baseline_test_classifications)

    def test_sibling_matching_the_selector_does_not_exculpate(self) -> None:
        """``-k`` is a substring match, so test_ab runs alongside test_a."""
        self._write_test(_source(
            "def test_a(self):",
            "    from app import VALUE",
            "    self.assertEqual(VALUE, 'good')",
            "def test_ab(self): self.fail('already broken')",
        ))
        self._commit()
        (self.repo / "app.py").write_text("VALUE = 'bad'\n", encoding="utf-8")

        result = self._verify()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.baseline_test_classifications, {
            TEST_A: "regression", "test_sample.T.test_ab": "pre_existing",
        })

    def test_module_broken_by_the_change_is_a_regression(self) -> None:
        self._write_test("from app import VALUE\n" + PASSING)
        self._commit()
        (self.repo / "app.py").write_text("OTHER = 1\n", encoding="utf-8")

        result = self._verify()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.baseline_test_classifications, {
            "unittest.loader._FailedTest.test_sample": "regression",
        })

    def test_failure_in_nested_test_directory_is_rerun_under_its_own_id(self) -> None:
        self._write_test(FAILING, name="sub/test_nested.py")
        (self.repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "tests" / "sub" / "__init__.py").write_text("", encoding="utf-8")
        self._commit()
        self._change_unrelated_file()

        result = self._verify()

        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.baseline_test_classifications, {
            "sub.test_nested.T.test_a": "pre_existing",
        })

    def test_passing_run_that_logs_error_lines_is_not_rechecked(self) -> None:
        self._write_test(_source(
            "def test_a(self):",
            "    import sys",
            "    print('ERROR: something harmless', file=sys.stderr)",
        ))
        self._commit()
        self._change_unrelated_file()

        result = self._verify()

        self.assertEqual(result.test_exit_code, 0)
        self.assertIsNone(result.baseline_test_classifications)

    def test_error_line_from_a_test_passing_on_base_is_no_failure_there(self) -> None:
        self._write_test(_source(
            "def test_a(self):",
            "    import sys",
            "    from app import VALUE",
            "    print('ERROR: something harmless', file=sys.stderr)",
            "    self.assertEqual(VALUE, 'good')",
        ))
        self._commit()
        (self.repo / "app.py").write_text("VALUE = 'bad'\n", encoding="utf-8")

        result = self._verify()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.baseline_test_classifications, {TEST_A: "regression"})

    def test_subtest_failure_is_collected_and_classified(self) -> None:
        self._write_test(_source(
            "def test_a(self):",
            "    with self.subTest(i=1):",
            "        self.fail('already broken')",
        ))
        self._commit()
        self._change_unrelated_file()

        result = self._verify()

        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.baseline_test_classifications, {TEST_A: "pre_existing"})

    def test_recheck_crash_is_a_regression_and_still_cleans_up(self) -> None:
        self._write_test(FAILING)
        self._commit()
        self._change_unrelated_file()
        failed = VerificationRunResult(
            1, "", "", False, None, 1, failed_test_ids=[TEST_A],
            observed_test_ids=[TEST_A],
        )
        with patch(RUNNER, side_effect=[failed, RuntimeError("runner broke")]):
            result = self._verify()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.baseline_test_classifications, {TEST_A: "regression"})
        self._assert_no_base_worktree_left()

    def test_invalid_deadline_falls_back_to_the_default(self) -> None:
        for raw in ("nonsense", "0", "-1", "inf", "nan"):
            with patch.dict(os.environ, {"NEXUS_BASELINE_RECHECK_SECONDS": raw}):
                self.assertEqual(self.service._baseline_recheck_seconds(), 120)


class TestIdHelpersTests(unittest.TestCase):
    def test_test_file_for_id(self) -> None:
        for test_id, expected in {
            "test_x.T.test_a": "tests/test_x.py",
            "tests.test_x.T.test_a": "tests/test_x.py",
            "sub.test_x.T.test_a": "tests/sub/test_x.py",
            "testfoo.T.test_a": "tests/testfoo.py",
            "unittest.loader._FailedTest.test_x": "tests/test_x.py",
            "unittest.loader._FailedTest.sub.test_x": "tests/sub/test_x.py",
            "<unknown test>": None,
        }.items():
            self.assertEqual(_test_file_for_id(test_id), expected, test_id)

    def test_parse_failed_test_ids(self) -> None:
        output = (
            "ERROR: something harmless\n"
            "ERROR: test_x (unittest.loader._FailedTest.test_x)\n"
            "ERROR: sub.test_y (unittest.loader._FailedTest.sub.test_y)\n"
            "FAIL: test_a (mod.T.test_a) (i=1)\n"
            "FAIL: test_a (mod.T.test_a)\nDocstring\n"
            "ERROR: setUpClass (mod.T)\n"
            "UNEXPECTED SUCCESS: test_b (mod.T.test_b)\n"
        )
        self.assertEqual(parse_failed_test_ids(output), {
            "unittest.loader._FailedTest.test_x",
            "unittest.loader._FailedTest.sub.test_y",
            "mod.T.test_a", "mod.T.setUpClass", "mod.T.test_b",
        })


if __name__ == "__main__":
    unittest.main()
