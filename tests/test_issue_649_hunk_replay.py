"""#649: hunk-level local shadow replay.

A temporary git repository holds a base and a merged commit. Fake workers
stand in for the local model; the merged tests run for real with pytest in
the throwaway worktree.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules import local_shadow_replay as lsr
from modules import local_shadow_replay_hunks as lsh
from modules.coding_worker import WorkerResult
from modules.operations_registry import OperationsRegistry
from modules.run_store import init_db

HEADERS = {"x-api-key": "test-key"}
SHAPES = "pkg/shapes.py"
UNITS = "pkg/units.py"

BASE_SHAPES = "import math\n\n\ndef area(r):\n    return 0\n\n\ndef perimeter(r):\n    return 0\n"
MERGED_SHAPES = (
    "import math\n\n\ndef area(r):\n    return math.pi * r * r\n\n\n"
    "def perimeter(r):\n    return 2 * math.pi * r\n"
)
TEST_SHAPES = (
    "import math\n\nfrom pkg import shapes\n\n\n"
    "def test_area():\n    assert shapes.area(1) == math.pi\n\n\n"
    "def test_perimeter():\n    assert shapes.perimeter(1) == 2 * math.pi\n"
)
# Multi-file merge: area needs a function the same merge added to another file.
MULTI_SHAPES = (
    "import math\nfrom pkg import units\n\n\ndef area(r):\n    return units.scale(math.pi * r * r)\n\n\n"
    "def perimeter(r):\n    return 2 * math.pi * r\n"
)
BASE_UNITS = "FACTOR = 1\n"
MERGED_UNITS = "FACTOR = 1\n\n\ndef scale(x):\n    return x * FACTOR\n"
TEST_UNITS = "from pkg import units\n\n\ndef test_scale():\n    assert units.scale(2) == 2\n"

GOAL = "GitHub Issue #42: shapes compute real values\n\nA long issue body the micro-task must not carry."


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(root, base_files, merged_files, *, message="merged shapes (#42)", deleted=()):
    repo = str(Path(root) / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    for rel, text in base_files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    for rel, text in merged_files.items():
        _write(repo, rel, text)
    for rel in deleted:
        _git(repo, "rm", "-q", rel)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return repo, base, _git(repo, "rev-parse", "HEAD")


def worker_result(success=True, *, error=None, timed_out=False, working_dir="/tmp"):
    return WorkerResult(
        success=success, exit_code=0 if success else 1, stdout="", stderr="", runtime_ms=1,
        timed_out=timed_out, stdout_truncated=False, stderr_truncated=False, thread_id=None,
        final_message=None, usage=None, task="t", working_dir=working_dir,
        sandbox="workspace-write", error=error, provider="local",
    )


class FakeClient:
    model = "fake-local-model"

    async def is_available(self):
        return True

    async def busy_slots(self):
        return 0


class FakeWorker:
    """Writes *files* (path -> text) into the worktree and records what it saw."""

    def __init__(self, files=None, *, raises=None, result=None, per_call=None):
        self.files, self.raises, self.result, self.per_call = files or {}, raises, result, per_call
        self.calls = []

    def run(self, *, task, working_dir, sandbox, experiment_logger=None, timeout_seconds=None, **_):
        root = Path(working_dir)
        self.calls.append({
            "task": task, "timeout": timeout_seconds,
            "shapes": (root / SHAPES).read_text() if (root / SHAPES).exists() else None,
            "units": (root / UNITS).read_text() if (root / UNITS).exists() else None,
            "saw_merged_test": (root / "tests" / "test_shapes.py").exists()
            or (root / "tests" / "test_units.py").exists(),
        })
        experiment_logger.info("p", "local_patch_round", "round 1", round=1)
        experiment_logger.info("p", "local_model_call", "call", prompt_tokens=90,
                               prompt_tokens_total=100, prompt_tokens_cached=40)
        if self.per_call is not None:
            action = self.per_call(len(self.calls))
            if isinstance(action, BaseException):
                raise action
        if self.raises is not None:
            raise self.raises
        for rel, text in self.files.items():
            _write(root, rel, text)
        return self.result or worker_result(working_dir=working_dir)


class HunkFixture:
    def __init__(self, case, *, multi=False):
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.worktrees = str(self.root / "worktrees")
        self.db_path = str(self.root / "agent.db")
        env = patch.dict(os.environ, {lsr.ENV_WORKTREE_DIR: self.worktrees}, clear=False)
        env.start()
        case.addCleanup(env.stop)
        for name in (lsr.ENV_VARIANT, lsr.ENV_ENABLED, lsr.ENV_UNIT, lsh.ENV_UNIT_TIMEOUT):
            os.environ.pop(name, None)
        base_files = {"pkg/__init__.py": "", SHAPES: BASE_SHAPES, "README.md": "base\n"}
        merged_files = {SHAPES: MERGED_SHAPES, "tests/test_shapes.py": TEST_SHAPES, "README.md": "merged\n"}
        if multi:
            base_files[UNITS] = BASE_UNITS
            merged_files.update({SHAPES: MULTI_SHAPES, UNITS: MERGED_UNITS, "tests/test_units.py": TEST_UNITS})
        self.merged_shapes = merged_files[SHAPES]
        self.repo, self.base, self.merged = make_repo(self.root, base_files, merged_files)
        init_db(self.db_path).close()
        self.add_experiment("exp-1", merged=self.merged, base=self.base)

    def add_experiment(self, experiment_id, *, merged, base, ended_at="2026-11-01T00:00:00+00:00"):
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at, implementation_provider, "
                "base_commit_sha) VALUES (?, ?, 'succeeded', ?, 'codex', ?)",
                (experiment_id, GOAL, ended_at, base),
            )
            conn.execute(
                "INSERT INTO experiment_adoptions (id, experiment_id, status, ended_at, "
                "integrated_commit_sha, created_at, updated_at) VALUES (?, ?, 'adopted', ?, ?, ?, ?)",
                (f"ad-{experiment_id}", experiment_id, ended_at, merged, ended_at, ended_at),
            )
        conn.close()

    def replay(self, worker, *, experiment_id="exp-1", variant="v1", max_units=None):
        return lsr.replay(
            experiment_id, repo_root=self.repo, db_path=self.db_path, variant=variant,
            client=FakeClient(), worker_factory=lambda _client: worker, unit="hunk",
            max_units=max_units,
        )

    def sweep(self, worker, *, variant="v1"):
        return lsr.replay_next(repo_root=self.repo, db_path=self.db_path, variant=variant,
                               client=FakeClient(), worker_factory=lambda _client: worker, unit="hunk")

    def units(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return {(r["file"], r["symbol"]): dict(r) for r in conn.execute("SELECT * FROM local_replay_units")}
        finally:
            conn.close()

    def run_row(self, experiment_id="exp-1", variant="v1"):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM local_replay_unit_runs WHERE experiment_id=? AND prompt_variant=?",
                               (experiment_id, variant)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def leftover_worktrees(self):
        listing = _git(self.repo, "worktree", "list", "--porcelain")
        registered = [line for line in listing.splitlines() if lsr.WORKTREE_PREFIX in line]
        on_disk = list(Path(self.worktrees).glob(lsr.WORKTREE_PREFIX + "*")) if Path(self.worktrees).exists() else []
        return registered + on_disk


# ---- units and splicing (no worker) -----------------------------------------

class UnitTests(unittest.TestCase):
    def setUp(self):
        self.fx = HunkFixture(self)

    def test_two_functions_in_one_file_plus_a_test_file_give_two_units(self):
        units = lsh.hunk_units(self.fx.repo, self.fx.base, self.fx.merged)
        self.assertEqual([(u.file, u.symbol) for u in units], [(SHAPES, "area"), (SHAPES, "perimeter")])

    def test_unit_start_text_reverts_only_its_own_hunks(self):
        area, perimeter = lsh.hunk_units(self.fx.repo, self.fx.base, self.fx.merged)
        self.assertIn("def area(r):\n    return 0\n", area.start_text)
        self.assertIn("return 2 * math.pi * r", area.start_text)
        self.assertIn("return math.pi * r * r", perimeter.start_text)
        self.assertIn("def perimeter(r):\n    return 0\n", perimeter.start_text)

    def test_splicing_a_unit_restores_the_other_hunks_from_merged(self):
        area = lsh.hunk_units(self.fx.repo, self.fx.base, self.fx.merged)[0]
        # The local file even has the *base* perimeter: only area may come from it.
        local = BASE_SHAPES.replace("def area(r):\n    return 0", "def area(r):\n    return 42")
        spliced = lsh.splice_unit(area, local)
        self.assertIn("def area(r):\n    return 42\n", spliced)
        self.assertIn("return 2 * math.pi * r", spliced)
        self.assertEqual(lsh.splice_unit(area, MERGED_SHAPES), MERGED_SHAPES)

    def test_unparseable_or_missing_symbol_never_falls_back_to_merged(self):
        area = lsh.hunk_units(self.fx.repo, self.fx.base, self.fx.merged)[0]
        with self.assertRaisesRegex(lsh.SpliceError, "does not parse"):
            lsh.splice_unit(area, "def area(r):\n    return (\n")
        with self.assertRaisesRegex(lsh.SpliceError, "has no area"):
            lsh.splice_unit(area, MERGED_SHAPES.replace("def area(", "def area2("))


class GroupingTests(unittest.TestCase):
    def _repo(self, base, merged):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return make_repo(tmp.name, {"pkg/m.py": base}, {"pkg/m.py": merged})

    def test_same_named_methods_are_separate_units_and_splice_separately(self):
        base = ("class A:\n    def run(self):\n        return 1\n\n\n"
                "class B:\n    def run(self):\n        return 1\n")
        merged = base.replace("return 1", "return 2")
        repo, old, new = self._repo(base, merged)
        units = lsh.hunk_units(repo, old, new)
        # Innermost symbol only: no extra class-level units.
        self.assertEqual([u.symbol for u in units], ["A.run", "B.run"])
        local = ("class A:\n    def run(self):\n        return 3\n\n\n"
                 "class B:\n    def run(self):\n        return 99\n")
        spliced = lsh.splice_unit(units[0], local)
        self.assertEqual(spliced, "class A:\n    def run(self):\n        return 3\n\n\n"
                                  "class B:\n    def run(self):\n        return 2\n")

    def test_repeated_names_in_one_scope_get_distinct_keys(self):
        base = "def f():\n    return 1\n\n\ndef f():\n    return 1\n"
        merged = base.replace("return 1", "return 2")
        repo, old, new = self._repo(base, merged)
        self.assertEqual([u.symbol for u in lsh.hunk_units(repo, old, new)], ["f", "f#2"])

    def test_module_level_unit_keeps_merged_symbols_it_does_not_own(self):
        base = "X = 1\n\n\ndef f():\n    return 1\n"
        merged = "X = 2\n\n\ndef f():\n    return 2\n"
        repo, old, new = self._repo(base, merged)
        module, func = lsh.hunk_units(repo, old, new)
        self.assertEqual((module.symbol, func.symbol), ("<module>", "f"))
        self.assertEqual(lsh.splice_unit(module, "X = 3\n\n\ndef f():\n    return 1\n"),
                         "X = 3\n\n\ndef f():\n    return 2\n")
        with self.assertRaisesRegex(lsh.SpliceError, "has no f"):
            lsh.splice_unit(module, "X = 3\n")

    def test_at_most_five_units(self):
        base = "".join(f"def f{i}():\n    return 0\n\n\n" for i in range(8))
        repo, old, new = self._repo(base, base.replace("return 0", "return 1"))
        self.assertEqual(len(lsh.hunk_units(repo, old, new)), lsh.MAX_HUNK_GROUPS)

    def test_overlap_is_one_for_identical_and_lower_otherwise(self):
        merged = lsh._change_set("a\nb\n", "a\nc\n")
        self.assertEqual(lsh.changed_line_overlap(merged, lsh._change_set("a\nb\n", "a\nc\n")), 1.0)
        self.assertEqual(lsh.changed_line_overlap(merged, lsh._change_set("a\nb\n", "a\nd\n")), 1 / 3)


# ---- scoring with a fake worker ---------------------------------------------

class ScoringTests(unittest.TestCase):
    def test_patch_identical_to_merged_scores_everything_on_a_multi_file_merge(self):
        fx = HunkFixture(self, multi=True)
        worker = FakeWorker({SHAPES: MULTI_SHAPES, UNITS: MERGED_UNITS})
        summary = fx.replay(worker)
        self.assertEqual(summary["status"], lsh.RUN_DONE)
        rows = fx.units()
        self.assertEqual(set(rows), {(SHAPES, "<module>"), (SHAPES, "area"), (SHAPES, "perimeter"),
                                     (UNITS, "scale")})
        for key, row in rows.items():
            self.assertEqual(row["status"], "scored", key)
            self.assertEqual((row["applied"], row["compiled"], row["has_tests"], row["tests_passed"]),
                             (1, 1, 1, 1), (key, row["error"]))
            self.assertEqual(row["overlap"], 1.0, key)
            self.assertEqual((row["prompt_tokens"], row["cached_tokens"], row["rounds"]), (100, 40, 1))
            self.assertEqual(row["model"], "fake-local-model")
        # The worker started from merged-except-this-unit and never saw merged tests.
        self.assertFalse(any(call["saw_merged_test"] for call in worker.calls))
        area_call = next(c for c in worker.calls if "change `area`" in c["task"])
        self.assertIn("def area(r):\n    return 0\n", area_call["shapes"])
        self.assertIn("def scale(x):", area_call["units"])  # the other merged file is there
        self.assertEqual(fx.leftover_worktrees(), [])

    def test_edits_to_other_files_do_not_leak_into_the_scored_tree(self):
        fx = HunkFixture(self, multi=True)
        broken_units = "FACTOR = 1\n\n\ndef scale(x):\n    return -999\n"
        fx.replay(FakeWorker({SHAPES: MULTI_SHAPES, UNITS: broken_units}))
        rows = fx.units()
        self.assertTrue(rows[(SHAPES, "area")]["tests_passed"])  # units.py is merged again
        self.assertFalse(rows[(UNITS, "scale")]["tests_passed"])

    def test_wrong_patch_does_not_pass(self):
        fx = HunkFixture(self)
        wrong = MERGED_SHAPES.replace("return math.pi * r * r", "return -1").replace(
            "return 2 * math.pi * r", "return -1")
        fx.replay(FakeWorker({SHAPES: wrong}))
        for key, row in fx.units().items():
            self.assertEqual((row["applied"], row["compiled"], row["tests_passed"]), (1, 1, 0), key)
            self.assertLess(row["overlap"], 1.0)

    def test_syntax_error_in_the_local_file_fails_the_unit(self):
        fx = HunkFixture(self)
        fx.replay(FakeWorker({SHAPES: "import math\n\n\ndef area(r):\n    return (\n"}))
        for key, row in fx.units().items():
            self.assertEqual((row["compiled"], row["tests_passed"]), (0, 0), key)
            self.assertIn("does not parse", row["error"])

    def test_renamed_symbol_fails_only_its_unit(self):
        fx = HunkFixture(self)
        fx.replay(FakeWorker({SHAPES: MERGED_SHAPES.replace("def area(", "def area2(")}))
        rows = fx.units()
        self.assertEqual((rows[(SHAPES, "area")]["compiled"], rows[(SHAPES, "area")]["tests_passed"]), (0, 0))
        self.assertIn("has no area", rows[(SHAPES, "area")]["error"])
        self.assertTrue(rows[(SHAPES, "perimeter")]["tests_passed"])

    def test_untouched_unit_is_not_applied(self):
        fx = HunkFixture(self)
        fx.replay(FakeWorker({}))
        for row in fx.units().values():
            self.assertEqual((row["applied"], row["tests_passed"], row["overlap"]), (0, 0, 0.0))

    def test_micro_task_is_small_and_names_title_goal_symbol_and_file(self):
        fx = HunkFixture(self)
        worker = FakeWorker({SHAPES: MERGED_SHAPES})
        fx.replay(worker)
        task = worker.calls[0]["task"]
        self.assertIn("Issue: GitHub Issue #42: shapes compute real values", task)
        self.assertIn("Goal: merged shapes (#42): change `area` in pkg/shapes.py", task)
        self.assertNotIn("long issue body", task)
        self.assertNotIn("math.pi", task)
        self.assertEqual(worker.calls[0]["timeout"], lsh.DEFAULT_UNIT_TIMEOUT_SECONDS)

    def test_production_factory_turns_splitting_off(self):
        worker = lsh.hunk_worker_factory(FakeClient())
        self.assertFalse(worker.split_tasks)

    def test_large_file_gives_imports_plus_enclosing_symbol(self):
        filler = "".join(f"def keep{i}():\n    return '{'x' * 200}'\n\n\n" for i in range(80))
        base = "import math\n\n\n" + filler + "def target():\n    return 0\n"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo, old, new = make_repo(tmp.name, {"pkg/big.py": base},
                                   {"pkg/big.py": base.replace("return 0", "return 1")})
        unit = lsh.hunk_units(repo, old, new)[0]
        text, reduced = lsh.worker_start_text(unit)
        self.assertTrue(reduced)
        self.assertEqual(text, "import math\n\n\ndef target():\n    return 0\n")
        self.assertIn("return 1", lsh.splice_unit(unit, "import math\n\n\ndef target():\n    return 1\n"))


# ---- failures, interruptions, reruns ----------------------------------------

class RobustnessTests(unittest.TestCase):
    def setUp(self):
        self.fx = HunkFixture(self)

    def test_timeout_is_stored_per_unit_and_counted(self):
        worker = FakeWorker({SHAPES: MERGED_SHAPES},
                            result=worker_result(False, error="local model call timed out", timed_out=True))
        self.fx.replay(worker)
        self.assertEqual({r["status"] for r in self.fx.units().values()}, {"timeout"})
        stats = lsh.unit_variant_summary(db_path=self.fx.db_path)[0]
        self.assertEqual((stats["units"], stats["completed"], stats["timeouts"], stats["completion_rate"]),
                         (2, 0, 2, 0.0))

    def test_worker_exception_is_stored_and_the_next_unit_still_runs(self):
        worker = FakeWorker({SHAPES: MERGED_SHAPES},
                            per_call=lambda n: RuntimeError("model exploded") if n == 1 else None)
        summary = self.fx.replay(worker)
        rows = self.fx.units()
        self.assertEqual(rows[(SHAPES, "area")]["status"], "error")
        self.assertIn("model exploded", rows[(SHAPES, "area")]["error"])
        self.assertTrue(rows[(SHAPES, "perimeter")]["tests_passed"])
        self.assertEqual(summary["status"], lsh.RUN_DONE)

    def test_second_call_reruns_nothing(self):
        self.fx.replay(FakeWorker({SHAPES: MERGED_SHAPES}))
        again = FakeWorker({SHAPES: MERGED_SHAPES})
        summary = self.fx.replay(again)
        self.assertEqual(again.calls, [])
        self.assertEqual(len(summary["units"]), 2)
        self.assertEqual(summary["ran_units"], 0)

    def test_crash_after_the_last_unit_but_before_the_run_was_closed(self):
        self.fx.replay(FakeWorker({SHAPES: MERGED_SHAPES}))
        conn = sqlite3.connect(self.fx.db_path)
        with conn:
            conn.execute("UPDATE local_replay_unit_runs SET status='running', finished_at=NULL")
        conn.close()
        again = FakeWorker({SHAPES: MERGED_SHAPES})
        summary = self.fx.replay(again)
        self.assertEqual(again.calls, [])
        self.assertEqual(summary["status"], lsh.RUN_DONE)

    def test_interrupted_unit_is_retried_once_then_stored_as_error(self):
        interrupt = FakeWorker(raises=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.fx.replay(interrupt)
        self.assertEqual(self.fx.units()[(SHAPES, "area")]["status"], "running")
        self.assertEqual(self.fx.run_row()["status"], "running")
        self.assertEqual(self.fx.leftover_worktrees(), [])
        conn = sqlite3.connect(self.fx.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM local_replay_lease").fetchone()[0], 0)
        conn.close()
        # In-progress units are not counted as finished.
        self.assertEqual(lsh.unit_variant_summary(db_path=self.fx.db_path)[0]["running"], 1)

        with self.assertRaises(KeyboardInterrupt):
            self.fx.replay(FakeWorker(raises=KeyboardInterrupt()))  # retry, interrupted again
        good = FakeWorker({SHAPES: MERGED_SHAPES})
        summary = self.fx.replay(good)
        rows = self.fx.units()
        self.assertEqual(rows[(SHAPES, "area")]["status"], "error")
        self.assertIn("interrupted 2 times", rows[(SHAPES, "area")]["error"])
        self.assertTrue(rows[(SHAPES, "perimeter")]["tests_passed"])
        self.assertEqual(len(good.calls), 1)
        self.assertEqual(summary["status"], lsh.RUN_DONE)

    def test_lease_is_renewed_for_every_unit_to_cover_unit_and_tests(self):
        db_path = self.fx.db_path
        seen = []

        def read_lease(_n):
            conn = sqlite3.connect(db_path)
            try:
                seen.append(datetime.fromisoformat(
                    conn.execute("SELECT lease_expires_at FROM local_replay_lease").fetchone()[0]))
            finally:
                conn.close()

        env = {lsh.ENV_UNIT_TIMEOUT: "4000", lsr.ENV_TEST_TIMEOUT: "3000",
               lsr.ENV_WORKER_TIMEOUT: "100", lsr.ENV_LEASE: "10"}
        with patch.dict(os.environ, env):
            before = datetime.now(timezone.utc)
            self.fx.replay(FakeWorker({SHAPES: MERGED_SHAPES}, per_call=read_lease))
        self.assertEqual(len(seen), 2)  # both units ran
        for expires in seen:
            self.assertGreater(expires, before + timedelta(seconds=4000 + 3000))
        self.assertGreater(lsh.unit_lease_seconds(), lsh.unit_timeout_seconds() + lsr.tests_timeout_seconds())

    def test_lost_lease_mid_loop_leaves_the_rest_resumable(self):
        answers = iter([True, False])
        worker = FakeWorker({SHAPES: MERGED_SHAPES})
        with patch.object(lsr, "renew_lease", side_effect=lambda *_a: next(answers)):
            self.fx.replay(worker)
        self.assertEqual(len(worker.calls), 1)
        rows = self.fx.units()
        self.assertEqual(len(rows), 1)  # the second unit was never claimed
        self.assertEqual(self.fx.run_row()["status"], "running")
        self.assertEqual(self.fx.leftover_worktrees(), [])
        conn = sqlite3.connect(self.fx.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM local_replay_lease").fetchone()[0], 0)
        conn.close()
        # The next replay picks up the remaining unit and finishes the run.
        again = FakeWorker({SHAPES: MERGED_SHAPES})
        summary = self.fx.replay(again)
        self.assertEqual(len(again.calls), 1)
        self.assertEqual(summary["status"], lsh.RUN_DONE)

    def test_interrupted_unit_resumes_and_scores(self):
        with self.assertRaises(KeyboardInterrupt):
            self.fx.replay(FakeWorker(raises=KeyboardInterrupt()))
        self.fx.replay(FakeWorker({SHAPES: MERGED_SHAPES}))
        rows = self.fx.units()
        self.assertEqual(rows[(SHAPES, "area")]["attempts"], 2)
        self.assertTrue(all(r["tests_passed"] for r in rows.values()))

    def test_sweep_scores_one_unit_per_tick_then_moves_on(self):
        worker = FakeWorker({SHAPES: MERGED_SHAPES})
        first = self.fx.sweep(worker)
        self.assertTrue(first["ran"])
        self.assertEqual((first["experiment_id"], first["replay"]["ran_units"], first["reason"]),
                         ("exp-1", 1, "running"))
        second_tick = self.fx.sweep(worker)
        self.assertEqual((second_tick["experiment_id"], second_tick["reason"]), ("exp-1", "done"))
        self.assertEqual(len(worker.calls), 2)
        third = self.fx.sweep(worker)
        self.assertFalse(third["ran"])
        self.assertEqual(third["reason"], "no merged paid experiment left to replay")

    def test_experiments_without_units_or_with_set_up_errors_do_not_stall_the_sweep(self):
        # A merge that changes only tests and docs.
        _write(self.fx.repo, "tests/test_extra.py", "def test_x():\n    assert True\n")
        _write(self.fx.repo, "README.md", "docs\n")
        _git(self.fx.repo, "add", "-A")
        _git(self.fx.repo, "commit", "-q", "-m", "tests only")
        tests_only = _git(self.fx.repo, "rev-parse", "HEAD")
        self.fx.add_experiment("exp-0-tests", merged=tests_only, base=self.fx.merged,
                               ended_at="2026-10-01T00:00:00+00:00")
        # A merged commit that is not in the repository.
        self.fx.add_experiment("exp-0-gone", merged="ab" * 20, base="cd" * 20,
                               ended_at="2026-10-02T00:00:00+00:00")
        worker = FakeWorker({SHAPES: MERGED_SHAPES})
        ticks = [self.fx.sweep(worker) for _ in range(4)]
        self.assertEqual([t["experiment_id"] for t in ticks], ["exp-0-tests", "exp-0-gone", "exp-1", "exp-1"])
        self.assertEqual([t["reason"] for t in ticks], ["no_units", "error", "running", "done"])
        self.assertIn("not available", self.fx.run_row("exp-0-gone")["error"])
        self.assertFalse(self.fx.sweep(worker)["ran"])
        stats = lsh.unit_variant_summary(db_path=self.fx.db_path)[0]
        self.assertEqual((stats["experiments_without_units"], stats["experiment_errors"]), (1, 1))

    def test_whole_and_hunk_modes_are_independent(self):
        self.fx.replay(FakeWorker({SHAPES: MERGED_SHAPES}))
        conn = sqlite3.connect(self.fx.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM local_replays").fetchone()[0], 0)
        conn.close()
        with self.assertRaisesRegex(ValueError, "unit must be"):
            lsr.replay("exp-1", repo_root=self.fx.repo, db_path=self.fx.db_path, unit="line")

    def test_schema_migration_adds_columns_to_an_old_units_table(self):
        path = str(self.fx.root / "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE local_replay_units (id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL, "
            "file TEXT NOT NULL, symbol TEXT NOT NULL, prompt_variant TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', "
            "applied INTEGER NOT NULL DEFAULT 0, compiled INTEGER NOT NULL DEFAULT 0, "
            "tests_passed INTEGER NOT NULL DEFAULT 0, overlap REAL NOT NULL DEFAULT 0, "
            "seconds REAL NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0, "
            "cached_tokens INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
            "UNIQUE(experiment_id, file, symbol, prompt_variant))")
        conn.close()
        init_db(path).close()
        init_db(path).close()  # idempotent
        conn = sqlite3.connect(path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(local_replay_units)")}
        conn.close()
        self.assertTrue({"status", "error", "has_tests", "attempts", "tests_total"} <= columns)


# ---- read side ---------------------------------------------------------------

class ReadSideTests(unittest.TestCase):
    def test_api_and_operation_report_per_variant_unit_aggregates(self):
        fx = HunkFixture(self)
        fx.replay(FakeWorker({SHAPES: MERGED_SHAPES}), variant="good")
        fx.replay(FakeWorker({SHAPES: MERGED_SHAPES.replace("return 2 * math.pi * r", "return 0")}),
                  variant="half")
        service = MagicMock()
        service.db_path = fx.db_path
        mailbox = MagicMock()
        mailbox.recover_after_restart.return_value = None
        api.app.dependency_overrides[api.get_experiment_service] = lambda: service
        api.app.dependency_overrides[api.get_codex_service] = lambda: MagicMock()
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        self.addCleanup(api.app.dependency_overrides.clear)
        response = TestClient(api.app).get("/local-replays", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        stats = {s["prompt_variant"]: s for s in response.json()["unit_variants"]}
        self.assertEqual((stats["good"]["units"], stats["good"]["tests_passed"], stats["good"]["unit_pass_rate"],
                          stats["good"]["completion_rate"], stats["good"]["average_overlap"]),
                         (2, 2, 1.0, 1.0, 1.0))
        self.assertEqual((stats["half"]["tests_passed"], stats["half"]["unit_pass_rate"]), (1, 0.5))
        self.assertEqual(stats["good"]["prompt_tokens"], 200)

        registry = OperationsRegistry(experiment_service=MagicMock(), db_path=fx.db_path)
        self.addCleanup(registry.close)
        text = "\n".join(registry.execute("local_replays_list", {"limit": 2}).output_lines)
        self.assertIn("hunk variant good: 2 units from 1 experiments, finished in time 2/2 (100%)", text)
        self.assertIn("tests passed 1/2 (50%)", text)


if __name__ == "__main__":
    unittest.main()
