"""#711: a drift-only timing-check failure gets one local repair before it fails."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.experiment_evaluation import EvaluationResult
from modules.experiment_service import ExperimentService, STATUS_VERIFYING
from modules.experiment_service.service_parts import verification as verification_module
from modules.experiment_service.service_parts.verification import (
    TIMING_LOCAL_REPAIR_COMMIT_MESSAGE,
    TIMING_LOCAL_REPAIR_MAX_ROUNDS,
)

ROOT = Path(__file__).resolve().parents[1]
FLAGGED = "tests/test_new.py::T::test_budget"

# Passes normally, fails under the drift fixture (a wall-clock budget).
DRIFT_SENSITIVE = (
    "import time\n"
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_budget(self):\n"
    "        started = time.monotonic()\n"
    "        self.assertLess(time.monotonic() - started, 0.02)\n"
)
# The local fix: an injected clock instead of wall time.
CLOCK_INDEPENDENT = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_budget(self):\n"
    "        clock = iter([0.0, 0.001])\n"
    "        started = next(clock)\n"
    "        self.assertLess(next(clock) - started, 0.02)\n"
)
# A "fix" that still reads the wall clock: still fails under drift.
STILL_DRIFTING = DRIFT_SENSITIVE.replace("0.02", "0.03")
# Fails on this host whatever the clock does.
HOST_FAILING = (
    "import os\n"
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_host(self):\n"
    "        self.assertNotIn('NEXUS_TEST_CLOCK_DRIFT_MS', os.environ)\n"
)
# Other work of the experiment, which the repair commit must not take along.
APP = "VALUE = 1\n"
APP_UNCOMMITTED = APP + "# uncommitted tweak\n"


class FakeLocalWorker:
    """Writes *contents* (one per round) into tests/test_new.py, like a local SR patch."""

    def __init__(self, repo: Path, contents: list[str], *, success: bool = True,
                 stray: bool = False, also: dict[str, str] | None = None,
                 raises: BaseException | None = None) -> None:
        self.repo = repo
        self.contents = list(contents)
        self.success = success
        self.stray = stray
        self.also = dict(also or {})
        self.raises = raises
        self.tasks: list[str] = []
        self.kwargs: list[dict] = []
        self.last_run_metrics: dict | None = None

    def run(self, *, task: str, **kwargs):
        self.tasks.append(task)
        self.kwargs.append(kwargs)
        if self.raises is not None:
            raise self.raises
        content = self.contents[min(len(self.tasks), len(self.contents)) - 1]
        (self.repo / "tests" / "test_new.py").write_text(content, encoding="utf-8")
        for rel, text in self.also.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        # A stray file the patch created must go too when the repair is discarded.
        if self.stray:
            (self.repo / "tests" / f"scratch_{len(self.tasks)}.txt").write_text("x", encoding="utf-8")
        self.last_run_metrics = {"tokens_in": 100, "tokens_out": 20}
        return SimpleNamespace(
            success=self.success, error=None if self.success else "no applicable edit",
            usage=None, timed_out=False,
        )


class TimingLocalRepairTests(unittest.TestCase):
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
        self._install_drift_conftest()
        self.fake: FakeLocalWorker | None = None
        self.repair_factory = MagicMock(side_effect=lambda _path: self.fake)
        # Paid coders: none of them may be built or called on any path (#711 test 5).
        self.paid_factory = MagicMock(name="paid worker factory")
        self.plan_factory = MagicMock(name="plan worker factory")
        self.selection = MagicMock(name="selection worker")
        self.service = ExperimentService(
            db_path=str(root / "db.sqlite"), repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"), test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"), max_repair_iterations=1,
            selection_worker=self.selection, worker_factory=self.paid_factory,
            local_worker_factory=self.paid_factory, plan_worker_factory=self.plan_factory,
            timing_repair_worker_factory=self.repair_factory,
        )
        self.quality = patch(
            "modules.experiment_service.evaluate_implementation",
            return_value=EvaluationResult(outcome="pass", reason="ok"),
        )
        self.quality_mock = self.quality.start()
        self.addCleanup(self.quality.stop)
        self.chat = patch("modules.model_registry.chat", side_effect=AssertionError("paid call"))
        self.chat_mock = self.chat.start()
        self.addCleanup(self.chat.stop)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()
        for factory in (self.paid_factory, self.plan_factory):
            factory.assert_not_called()
        self.selection.run.assert_not_called()
        self.chat_mock.assert_not_called()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout

    def _install_drift_conftest(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import timing_check_replay
        finally:
            sys.path.pop(0)
        self.assertTrue(timing_check_replay.ensure_plugin(self.repo, ROOT))
        self._git("add", "tests/conftest.py")
        self._git("commit", "-q", "-m", "drift fixture")

    def _prepare(self, test_source: str | None = None, *,
                 extra_files: dict[str, str] | None = None) -> str:
        """Commit the experiment's work (a checkpoint, like a real branch) and
        leave one uncommitted tweak the repair commit must not take along."""
        base = self._git("rev-parse", "HEAD").strip()
        if test_source is not None:
            (self.repo / "tests" / "test_new.py").write_text(test_source, encoding="utf-8")
        for name, content in (extra_files or {}).items():
            (self.repo / name).write_text(content, encoding="utf-8")
        (self.repo / "app.py").write_text(APP, encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "experiment checkpoint")
        (self.repo / "app.py").write_text(APP_UNCOMMITTED, encoding="utf-8")
        experiment = self.service.create_experiment("Add a test")
        self.service._force_status(experiment.id, STATUS_VERIFYING, base_commit_sha=base)
        return experiment.id

    def _run_verify(self, experiment_id: str, *, repair: str = "1"):
        env = {"NEXUS_TIMING_CHECK": "1", "NEXUS_TIMING_LOCAL_REPAIR": repair}
        with patch.dict("os.environ", env):
            return self.service._verify(experiment_id, str(self.repo))

    def _verify(self, test_source: str | None = None, *, repair: str = "1",
                extra_files: dict[str, str] | None = None):
        experiment_id = self._prepare(test_source, extra_files=extra_files)
        return experiment_id, self._run_verify(experiment_id, repair=repair)

    def _events(self, experiment_id: str, event: str) -> list[dict]:
        path = Path(self.service.get_experiment_logger(experiment_id).path)
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        return [entry for entry in entries if entry.get("event") == event]

    def _tree_state(self) -> tuple[str, str, dict[str, str]]:
        files = {
            str(path.relative_to(self.repo)): path.read_text(encoding="utf-8")
            for path in sorted(self.repo.rglob("*"))
            if path.is_file() and ".git" not in path.parts
            and "__pycache__" not in path.parts and ".pytest_cache" not in path.parts
        }
        status = "\n".join(
            line for line in self._git("status", "--porcelain").splitlines()
            if "__pycache__" not in line and ".pytest_cache" not in line
        )
        return self._git("rev-parse", "HEAD").strip(), status, files

    # (1) a passing local fix is committed and verification continues
    def test_passing_fix_is_committed_and_verification_continues(self) -> None:
        self.fake = FakeLocalWorker(self.repo, [CLOCK_INDEPENDENT])
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        head_before = self._git("rev-parse", "HEAD").strip()
        result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(len(self.fake.tasks), 1)
        # Flagged id, failure tail, drift value and the change's files reach the worker.
        task = self.fake.tasks[0]
        self.assertIn(FLAGGED, task)
        self.assertIn("AssertionError", task)
        self.assertIn("NEXUS_TEST_CLOCK_DRIFT_MS=50", task)
        self.assertIn("app.py", task)
        self.assertIn("injected clock", task)
        # The worker is told the drift fixture itself is off limits.
        self.assertIn("conftest.py", task)
        # A new commit on the branch, on top of the old HEAD, with only the repair.
        self.assertEqual(self._git("rev-parse", "HEAD~1").strip(), head_before)
        self.assertEqual(self._git("log", "-1", "--format=%s").strip(), TIMING_LOCAL_REPAIR_COMMIT_MESSAGE)
        committed = self._git("show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(committed, ["tests/test_new.py"])
        self.assertEqual(self._git("show", "HEAD:tests/test_new.py"), CLOCK_INDEPENDENT)
        # The experiment's other uncommitted work stays uncommitted; the
        # repaired file itself is clean against the new HEAD.
        status_lines = self._git("status", "--porcelain").splitlines()
        self.assertIn(" M app.py", status_lines)
        self.assertFalse(any(line.endswith("tests/test_new.py") for line in status_lines))
        # Verification went on to the quality gate and records the repaired file.
        self.quality_mock.assert_called_once()
        self.assertIn("tests/test_new.py", result.git_status)
        self.assertIn("clock = iter", result.diff_text)

        started = self._events(experiment_id, "timing_check_local_repair_started")
        passed = self._events(experiment_id, "timing_check_local_repair_passed")
        self.assertEqual(len(started), 1)
        self.assertEqual(len(passed), 1)
        self.assertEqual(self._events(experiment_id, "timing_check_local_repair_failed"), [])
        fields = passed[0]
        self.assertEqual(fields["rounds"], 1)
        self.assertEqual(fields["tokens"], 120)
        self.assertGreaterEqual(fields["seconds"], 0)

    # (2) a fix that still fails under drift is discarded; today's verdict stays
    def test_fix_still_failing_under_drift_is_discarded(self) -> None:
        self.fake = FakeLocalWorker(self.repo, [STILL_DRIFTING, STILL_DRIFTING], stray=True)
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        self.assertIn(f"timing_sensitive: {FLAGGED}", result.error)
        # Both rounds ran; the second one saw the first round's failure.
        self.assertEqual(len(self.fake.tasks), TIMING_LOCAL_REPAIR_MAX_ROUNDS)
        self.assertIn("Previous local round failed", self.fake.tasks[1])
        self.assertIn("timing check under the same drift", self.fake.tasks[1])
        # Nothing of the attempt is left: same HEAD, same status, same files.
        head, status, files = self._tree_state()
        self.assertEqual(head, before[0])
        self.assertEqual(status, before[1])
        self.assertEqual(files, before[2])
        self.assertNotIn(TIMING_LOCAL_REPAIR_COMMIT_MESSAGE, self._git("log", "--format=%s"))
        failed = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["rounds"], 2)
        self.assertEqual(failed[0]["tokens"], 240)
        self.assertIn("seconds", failed[0])
        self.assertEqual(self._events(experiment_id, "timing_check_local_repair_passed"), [])

    def test_failed_worker_run_is_discarded(self) -> None:
        self.fake = FakeLocalWorker(self.repo, [CLOCK_INDEPENDENT], success=False, stray=True)
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        head_before = self._git("rev-parse", "HEAD")
        result = self._run_verify(experiment_id)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self._git("rev-parse", "HEAD"), head_before)
        self.assertEqual(
            (self.repo / "tests" / "test_new.py").read_text(encoding="utf-8"), DRIFT_SENSITIVE,
        )
        self.assertFalse((self.repo / "tests" / "scratch_1.txt").exists())
        self.assertEqual(len(self._events(experiment_id, "timing_check_local_repair_failed")), 1)

    def test_patch_that_skips_the_test_is_rejected(self) -> None:
        skipped = DRIFT_SENSITIVE.replace(
            "    def test_budget", "    @unittest.skip('slow host')\n    def test_budget",
        )
        self.fake = FakeLocalWorker(self.repo, [skipped, skipped])
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        result = self._run_verify(experiment_id)
        self.assertEqual(result.status, "failed")
        failed = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertIn("skips or xfails", failed[0]["reason"])

    # A candidate that neuters the drift fixture must not make the drift rerun
    # pass vacuously: it is rejected before the judge runs.
    def test_candidate_that_neuters_the_drift_fixture_is_rejected(self) -> None:
        conftest = (self.repo / "tests" / "conftest.py").read_text(encoding="utf-8")
        neutered = conftest.replace("NEXUS_TEST_CLOCK_DRIFT_MS", "NEXUS_TEST_CLOCK_DISABLED_MS")
        self.assertNotEqual(neutered, conftest)
        self.fake = FakeLocalWorker(
            self.repo, [CLOCK_INDEPENDENT, CLOCK_INDEPENDENT],
            also={"tests/conftest.py": neutered},
        )
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        failed = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("timing-drift fixture", failed[0]["reason"])
        self.assertEqual(self._events(experiment_id, "timing_check_local_repair_passed"), [])
        # No commit, and the fixture is back exactly as it was.
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), before[0])
        self.assertNotIn(TIMING_LOCAL_REPAIR_COMMIT_MESSAGE, self._git("log", "--format=%s"))
        head, status, files = self._tree_state()
        self.assertEqual(files, before[2])
        self.assertEqual(
            (self.repo / "tests" / "conftest.py").read_text(encoding="utf-8"), conftest,
        )
        # The worker was told the fixture is off limits.
        self.assertIn("conftest.py", self.fake.tasks[0])

    # A candidate that detects the injected drift in the test itself is rejected too.
    def test_candidate_that_reads_the_drift_marker_is_rejected(self) -> None:
        sneaky = CLOCK_INDEPENDENT + "\n# opt out when NEXUS_TEST_CLOCK_DRIFT_MS is set\n"
        self.fake = FakeLocalWorker(self.repo, [sneaky, sneaky])
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        failed = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("timing-drift marker", failed[0]["reason"])
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), before[0])
        self.assertEqual(self._tree_state()[2], before[2])

    def test_candidate_that_adds_configs_is_rejected_before_the_judge(self) -> None:
        paths = (
            "conftest.py", "tests/new/conftest.py", "tox.ini", "setup.cfg",
            "pytest.ini", "pyproject.toml", "setup.py", ".pytest.ini",
            "pytest.toml", ".pytest.toml",
        )
        self.fake = FakeLocalWorker(
            self.repo, [CLOCK_INDEPENDENT], also=dict.fromkeys(paths, ""),
        )
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        with patch.object(self.service, "_judge_timing_repair") as judge:
            result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        judge.assert_not_called()
        failed, = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertTrue(failed["reason"].endswith(", ".join(sorted(paths))))
        self.assertEqual(self._tree_state(), before)

    def test_candidate_that_restores_the_real_clock_fails_the_canary(self) -> None:
        restored_clock = DRIFT_SENSITIVE.replace(
            "class T", "real_monotonic = time.monotonic\nclass T",
        ).replace(
            "        started =", "        time.monotonic = real_monotonic\n        started =",
        )
        self.fake = FakeLocalWorker(self.repo, [restored_clock])
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        failed, = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertIn("clock drift canary: time.monotonic has no injected drift", failed["reason"])
        self.assertEqual(self._tree_state(), before)

    # #842: a candidate that puts a real clock back during the test and
    # reinstates the drifted one before returning passes both boundary checks;
    # the canary samples the clocks while the call runs, so the guarded rerun
    # still fails and the candidate is rejected.
    def test_candidate_that_temporarily_restores_the_clock_is_rejected(self) -> None:
        temporary = (
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
        self.fake = FakeLocalWorker(self.repo, [temporary, temporary])
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        failed, = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertIn("clock drift canary", failed["reason"])
        self.assertIn("put back during the test", failed["reason"])
        self.assertEqual(self._tree_state(), before)
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), before[0])
        self.assertNotIn(TIMING_LOCAL_REPAIR_COMMIT_MESSAGE, self._git("log", "--format=%s"))

    # #842: a skip that avoids the literal skip keywords (SkipTest) reaches the
    # judge; there the guarded rerun must refuse it: the flagged test never
    # ran its call, so pytest's zero exit status proves nothing.
    SKIP_AT_SETUP = (
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def setUp(self):\n"
        "        raise unittest.SkipTest('not on this host')\n"
        "    def test_budget(self):\n"
        "        self.assertLess(1, 2)\n"
    )

    def test_candidate_that_skips_at_setup_without_the_skip_keyword_is_rejected(self) -> None:
        self.fake = FakeLocalWorker(self.repo, [self.SKIP_AT_SETUP, self.SKIP_AT_SETUP])
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        failed, = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertIn("timing check under the same drift", failed["reason"])
        self.assertIn(FLAGGED, failed["reason"])
        self.assertEqual(self._tree_state(), before)
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), before[0])

    # #842: an ignored conftest.py or root config is invisible to `git add -A`,
    # but it still steers pytest: creating one is rejected before the judge,
    # and the rollback removes it again.
    def test_candidate_creating_ignored_protected_files_is_rejected_and_removed(self) -> None:
        (self.repo / ".gitignore").write_text("ignored/\n/tox.ini\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-q", "-m", "gitignore")
        self.fake = FakeLocalWorker(
            self.repo, [CLOCK_INDEPENDENT],
            also={"ignored/conftest.py": "HOOK = 1\n", "tox.ini": "[pytest]\n"},
        )
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        with patch.object(self.service, "_judge_timing_repair") as judge:
            result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        judge.assert_not_called()
        failed, = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertIn("ignored/conftest.py", failed["reason"])
        self.assertIn("tox.ini", failed["reason"])
        self.assertFalse((self.repo / "ignored" / "conftest.py").exists())
        self.assertFalse((self.repo / "tox.ini").exists())
        self.assertEqual(self._tree_state(), before)
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), before[0])

    # #842: an ignored conftest.py that already existed is protected too, and
    # a rejected attempt restores its content, not just its existence.
    def test_candidate_modifying_an_existing_ignored_conftest_is_rejected_and_restored(self) -> None:
        (self.repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-q", "-m", "gitignore")
        ignored = self.repo / "ignored"
        ignored.mkdir()
        (ignored / "conftest.py").write_text("ORIGINAL = 1\n", encoding="utf-8")
        self.fake = FakeLocalWorker(
            self.repo, [CLOCK_INDEPENDENT],
            also={"ignored/conftest.py": "ORIGINAL = 2\n"},
        )
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        with patch.object(self.service, "_judge_timing_repair") as judge:
            result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        judge.assert_not_called()
        failed, = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertIn("ignored/conftest.py", failed["reason"])
        self.assertEqual((ignored / "conftest.py").read_text(encoding="utf-8"), "ORIGINAL = 1\n")
        self.assertEqual(self._tree_state(), before)

    # #842: a hang inside the guarded rerun aborts the whole repair; worktree
    # and branch are put back exactly and the old verdict stands.
    def test_judge_hang_mid_repair_restores_everything(self) -> None:
        from modules.verification_runner import HungTest

        self.fake = FakeLocalWorker(self.repo, [CLOCK_INDEPENDENT])
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        original = verification_module.timing_check_module.run_timing_check

        def hanging(*args, **kwargs):
            if kwargs.get("require_active_drift"):
                raise HungTest(FLAGGED)
            return original(*args, **kwargs)

        with patch.object(verification_module.timing_check_module, "run_timing_check", hanging):
            result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        self.assertIn(f"timing_sensitive: {FLAGGED}", result.error)
        failed, = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertIn("hung", failed["reason"])
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), before[0])
        self.assertNotIn(TIMING_LOCAL_REPAIR_COMMIT_MESSAGE, self._git("log", "--format=%s"))
        self.assertEqual(self._tree_state(), before)
        self.assertEqual(self._events(experiment_id, "timing_check_local_repair_passed"), [])

    # #842: halfway failure then success - round 1 is rejected by the canary
    # (skip at setup), round 2 passes and commits. Each guarded rerun reads
    # its own canary report, so round 1's exit-0-skipped run is never
    # mistaken for a pass.
    def test_canary_rejected_round_one_then_passing_round_two_commits(self) -> None:
        self.fake = FakeLocalWorker(self.repo, [self.SKIP_AT_SETUP, CLOCK_INDEPENDENT])
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        head_before = self._git("rev-parse", "HEAD").strip()
        result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(len(self.fake.tasks), 2)
        self.assertIn("Previous local round failed", self.fake.tasks[1])
        self.assertEqual(self._git("rev-parse", "HEAD~1").strip(), head_before)
        self.assertEqual(self._git("log", "-1", "--format=%s").strip(), TIMING_LOCAL_REPAIR_COMMIT_MESSAGE)
        committed = self._git("show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(committed, ["tests/test_new.py"])
        passed = self._events(experiment_id, "timing_check_local_repair_passed")
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0]["rounds"], 2)

    # A patch that overlaps uncommitted work cannot be committed as only its
    # delta, so the candidate is discarded - never committed file-whole.
    def test_candidate_overlapping_uncommitted_work_is_discarded(self) -> None:
        self.fake = FakeLocalWorker(
            self.repo, [CLOCK_INDEPENDENT, CLOCK_INDEPENDENT],
            also={"app.py": APP + "# worker edit\n"},
        )
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        failed = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("does not apply to the branch HEAD", failed[0]["reason"])
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), before[0])
        self.assertNotIn(TIMING_LOCAL_REPAIR_COMMIT_MESSAGE, self._git("log", "--format=%s"))
        head, status, files = self._tree_state()
        self.assertEqual(files, before[2])
        # The uncommitted tweak is still there, still uncommitted.
        self.assertIn(" M app.py", status.splitlines())
        self.assertEqual((self.repo / "app.py").read_text(encoding="utf-8"), APP_UNCOMMITTED)

    # A crash after the branch ref moved must roll the branch back, so no
    # repair commit stays on a worktree that was restored.
    def test_crash_after_the_branch_moves_rolls_the_branch_back(self) -> None:
        self.fake = FakeLocalWorker(self.repo, [CLOCK_INDEPENDENT])
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        original_run_git = verification_module._run_git

        def flaky(args, **kwargs):
            if args[:3] == ["--literal-pathspecs", "reset", "-q"]:
                raise subprocess.CalledProcessError(
                    128, ["git", *args], output="", stderr="injected failure",
                )
            return original_run_git(args, **kwargs)

        with patch.object(verification_module, "_run_git", flaky):
            result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "failed")
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), before[0])
        self.assertNotIn(TIMING_LOCAL_REPAIR_COMMIT_MESSAGE, self._git("log", "--format=%s"))
        head, status, files = self._tree_state()
        self.assertEqual(files, before[2])
        self.assertEqual(status, before[1])
        failed = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(self._events(experiment_id, "timing_check_local_repair_passed"), [])

    # An interruption mid-repair still leaves worktree and branch as they were
    # and falls back to today's verdict, like an interruption of the check.
    def _assert_interruption_restores_everything(self, raised: BaseException) -> None:
        self.fake = FakeLocalWorker(self.repo, [CLOCK_INDEPENDENT], raises=raised)
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        before = self._tree_state()
        result = self._run_verify(experiment_id)
        self.assertEqual(result.status, "failed")
        self.assertIn(f"timing_sensitive: {FLAGGED}", result.error)
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), before[0])
        self.assertNotIn(TIMING_LOCAL_REPAIR_COMMIT_MESSAGE, self._git("log", "--format=%s"))
        head, status, files = self._tree_state()
        self.assertEqual(files, before[2])
        failed = self._events(experiment_id, "timing_check_local_repair_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("interrupted", failed[0]["reason"])
        self.assertEqual(self._events(experiment_id, "timing_check_local_repair_passed"), [])

    def test_keyboard_interrupt_mid_repair_restores_everything(self) -> None:
        self._assert_interruption_restores_everything(KeyboardInterrupt())

    def test_system_exit_mid_repair_restores_everything(self) -> None:
        self._assert_interruption_restores_everything(SystemExit(1))

    # Fails halfway: round 1 is judged and discarded, round 2 passes and commits.
    def test_second_round_can_still_pass_and_commit(self) -> None:
        self.fake = FakeLocalWorker(self.repo, [STILL_DRIFTING, CLOCK_INDEPENDENT])
        experiment_id = self._prepare(DRIFT_SENSITIVE)
        head_before = self._git("rev-parse", "HEAD").strip()
        result = self._run_verify(experiment_id)

        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(len(self.fake.tasks), 2)
        self.assertIn("Previous local round failed", self.fake.tasks[1])
        self.assertEqual(self._git("rev-parse", "HEAD~1").strip(), head_before)
        self.assertEqual(self._git("log", "-1", "--format=%s").strip(), TIMING_LOCAL_REPAIR_COMMIT_MESSAGE)
        committed = self._git("show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(committed, ["tests/test_new.py"])
        passed = self._events(experiment_id, "timing_check_local_repair_passed")
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0]["rounds"], 2)
        self.assertEqual(passed[0]["tokens"], 240)

    # Runs twice: after a successful repair the next verification does not
    # attempt (or commit) a second repair.
    def test_successful_repair_is_not_attempted_again_on_the_next_verification(self) -> None:
        self.fake = FakeLocalWorker(self.repo, [CLOCK_INDEPENDENT])
        experiment_id, result = self._verify(DRIFT_SENSITIVE)
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(self.repair_factory.call_count, 1)

        self.service._force_status(experiment_id, STATUS_VERIFYING)
        with patch.dict("os.environ", {"NEXUS_TIMING_CHECK": "1", "NEXUS_TIMING_LOCAL_REPAIR": "1"}):
            again = self.service._verify(experiment_id, str(self.repo))
        self.assertEqual(again.status, "succeeded", again.error)
        self.assertEqual(self.repair_factory.call_count, 1)
        subjects = self._git("log", "--format=%s").splitlines()
        self.assertEqual(subjects.count(TIMING_LOCAL_REPAIR_COMMIT_MESSAGE), 1)

    # (3) host regressions and known (pre-existing) drift never trigger a repair
    def test_host_failure_never_triggers_a_repair(self) -> None:
        experiment_id, result = self._verify(HOST_FAILING)
        self.assertEqual(result.status, "needs_review")
        self.assertIn("fail on this host without drift too", result.error)
        self.repair_factory.assert_not_called()
        self.assertEqual(self._events(experiment_id, "timing_check_local_repair_started"), [])

    def test_known_base_drift_never_triggers_a_repair(self) -> None:
        # The drift-sensitive test exists on the base commit already.
        (self.repo / "tests" / "test_new.py").write_text(DRIFT_SENSITIVE, encoding="utf-8")
        self._git("add", "tests/test_new.py")
        self._git("commit", "-q", "-m", "old drift test")
        experiment_id, result = self._verify(DRIFT_SENSITIVE + "# touched\n")
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(len(self._events(experiment_id, "timing_check_passed_with_known_drift")), 1)
        self.repair_factory.assert_not_called()
        self.assertEqual(self._events(experiment_id, "timing_check_local_repair_started"), [])

    def test_mixed_host_failure_and_drift_still_repairs_only_drift(self) -> None:
        self.fake = FakeLocalWorker(self.repo, [CLOCK_INDEPENDENT])
        experiment_id, result = self._verify(
            DRIFT_SENSITIVE, extra_files={"tests/test_host.py": HOST_FAILING},
        )
        task = self.fake.tasks[0]
        self.assertIn(FLAGGED, task)
        self.assertNotIn("test_host", task.split("Files of this change")[0])
        started = self._events(experiment_id, "timing_check_local_repair_started")
        self.assertIn(FLAGGED, str(started[0]["tests"]))
        self.assertNotIn("test_host", str(started[0]["tests"]))
        # The host failure was known before the repair, so it does not reject it.
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(self._git("log", "-1", "--format=%s").strip(), TIMING_LOCAL_REPAIR_COMMIT_MESSAGE)

    # (4) the switch turns it off: today's behaviour
    def test_switch_off_keeps_todays_behaviour(self) -> None:
        experiment_id, result = self._verify(DRIFT_SENSITIVE, repair="0")
        self.assertEqual(result.status, "failed")
        self.assertIn(f"timing_sensitive: {FLAGGED}; hint:", result.error)
        self.repair_factory.assert_not_called()
        self.assertEqual(self._events(experiment_id, "timing_check_local_repair_started"), [])
        self.assertIn(" M app.py", self._git("status", "--porcelain").splitlines())

    def test_unset_switch_means_on(self) -> None:
        with patch.dict("os.environ"):
            import os
            os.environ.pop("NEXUS_TIMING_LOCAL_REPAIR", None)
            self.assertTrue(verification_module.timing_local_repair_enabled())
        for value in ("0", "false", "off", "no"):
            with patch.dict("os.environ", {"NEXUS_TIMING_LOCAL_REPAIR": value}):
                self.assertFalse(verification_module.timing_local_repair_enabled())


class DefaultWorkerTests(unittest.TestCase):
    # (5) the default worker is local-only: no split planner, no free tier, no test-first.
    def test_default_worker_never_reaches_a_remote_model(self) -> None:
        worker = verification_module._default_timing_repair_worker(("tests/test_new.py",))
        self.assertEqual(worker.provider, "local")
        self.assertFalse(worker.split_tasks)
        self.assertFalse(worker.test_first)
        self.assertEqual(worker.patch_rounds, 1)
        self.assertIsNone(worker.planner("any prompt"))
        self.assertEqual(worker._provided_likely_files, ("tests/test_new.py",))


if __name__ == "__main__":
    unittest.main()
