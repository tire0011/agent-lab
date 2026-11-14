"""#637: local shadow replay of merged paid experiments.

A temporary git repository holds a base commit and a "merged" commit that
changes source files and tests. Fake workers stand in for the local model;
the merged tests run for real with pytest in the throwaway worktree.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api
import nexus_mcp
import nexus_tick
from modules import local_shadow_replay as lsr
from modules.coding_worker import WorkerResult
from modules.nexus_mcp_tools import READ_ONLY_TOOLS, WRITE_TOOLS
from modules.operations_registry import OperationsRegistry
from modules.run_store import init_db

HEADERS = {"x-api-key": "test-key"}

BASE_CALC = "VERSION = 1\n\n\ndef add(a, b):\n    return 0\n"
FULL_CALC = (
    "VERSION = 2\n\n\ndef add(a, b):\n    return a + b\n\n\n"
    "def sub(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
)
PARTIAL_CALC = "VERSION = 1\n\n\ndef add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
MERGED_TEST_CALC = (
    "from pkg import calc\n\n\n"
    "def test_add():\n    assert calc.add(2, 3) == 5\n\n\n"
    "def test_sub():\n    assert calc.sub(5, 3) == 2\n\n\n"
    "def test_mul():\n    assert calc.mul(2, 3) == 6\n"
)
RENAMED_TEST = "from pkg import calc\n\n\ndef test_version():\n    assert calc.VERSION == 2\n"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _result(success, *, error=None, usage=None, working_dir="/tmp"):
    return WorkerResult(
        success=success, exit_code=0 if success else 1, stdout="", stderr="", runtime_ms=1,
        timed_out=False, stdout_truncated=False, stderr_truncated=False, thread_id=None,
        final_message=None, usage=usage, task="t", working_dir=working_dir,
        sandbox="workspace-write", error=error, provider="local",
    )


class FakeClient:
    model = "fake-local-model"

    def __init__(self, *, available=True, busy=0, raises=False):
        self.available, self.busy, self.raises = available, busy, raises

    async def is_available(self):
        if self.raises:
            raise ConnectionError("refused")
        return self.available

    async def busy_slots(self):
        return self.busy


class FakeWorker:
    """Writes *calc* (or nothing) and records what it saw."""

    def __init__(self, calc=None, *, raises=False, rounds=1):
        self.calc, self.raises, self.rounds = calc, raises, rounds
        self.calls = []

    def run(self, *, task, working_dir, sandbox, experiment_logger=None, **_):
        root = Path(working_dir)
        self.calls.append({
            "task": task, "working_dir": working_dir, "sandbox": sandbox,
            # The merged tests must not be visible while the model works.
            "saw_merged_test": (root / "tests" / "test_calc.py").exists(),
            "head": _git(working_dir, "rev-parse", "HEAD"),
        })
        for number in range(1, self.rounds + 1):
            experiment_logger.info("local_shadow_replay", "local_patch_round", f"round {number}", round=number)
        if self.raises:
            raise RuntimeError("model exploded")
        if self.calc is None:
            return _result(False, error="local model patch failed after 3 rounds", working_dir=working_dir)
        _write(root, "pkg/calc.py", self.calc)
        return _result(True, usage={"total_tokens": 123}, working_dir=working_dir)


class ReplayFixture:
    def __init__(self, case: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = str(self.root / "repo")
        self.worktrees = str(self.root / "worktrees")
        self.db_path = str(self.root / "agent.db")
        env = patch.dict(os.environ, {lsr.ENV_WORKTREE_DIR: self.worktrees}, clear=False)
        env.start()
        case.addCleanup(env.stop)
        for name in (lsr.ENV_VARIANT, lsr.ENV_ENABLED):
            os.environ.pop(name, None)

        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _write(self.repo, "pkg/__init__.py", "")
        _write(self.repo, "pkg/calc.py", BASE_CALC)
        _write(self.repo, "tests/test_old.py", RENAMED_TEST)
        _write(self.repo, "tests/test_gone.py", "def test_gone():\n    assert False\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")

        _write(self.repo, "pkg/calc.py", FULL_CALC)
        _write(self.repo, "pkg/extra.py", "SECRET_SOURCE = True\n")
        _write(self.repo, "tests/test_calc.py", MERGED_TEST_CALC)
        _write(self.repo, "tests/helpers.py", "HELPER = 1\n")
        _git(self.repo, "mv", "tests/test_old.py", "tests/test_renamed.py")
        _git(self.repo, "rm", "-q", "tests/test_gone.py")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "merged (#42)")
        self.merged = _git(self.repo, "rev-parse", "HEAD")

        _write(self.repo, "pkg/calc.py", FULL_CALC + "\n# source only\n")
        _git(self.repo, "commit", "-q", "-am", "source only")
        self.source_only = _git(self.repo, "rev-parse", "HEAD")

        init_db(self.db_path).close()
        self.add_experiment("exp-1", merged=self.merged, base=self.base)

    def add_experiment(self, experiment_id, *, merged, base, provider="codex", adoption="adopted",
                       ended_at="2026-11-01T00:00:00+00:00"):
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at, implementation_provider, "
                "base_commit_sha) VALUES (?, ?, 'succeeded', ?, ?, ?)",
                (experiment_id, f"GitHub Issue #42: fix calc ({experiment_id})", ended_at, provider, base),
            )
            conn.execute(
                "INSERT INTO experiment_adoptions (id, experiment_id, status, ended_at, "
                "integrated_commit_sha, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"ad-{experiment_id}", experiment_id, adoption, ended_at, merged, ended_at, ended_at),
            )
        conn.close()

    def rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM local_replays ORDER BY id")]
        finally:
            conn.close()

    def replay_worktrees(self):
        listing = _git(self.repo, "worktree", "list", "--porcelain")
        registered = [l for l in listing.splitlines() if lsr.WORKTREE_PREFIX in l]
        on_disk = list(Path(self.worktrees).glob(lsr.WORKTREE_PREFIX + "*")) if Path(self.worktrees).exists() else []
        return registered + [str(p) for p in on_disk]

    def replay(self, worker, *, variant="baseline", experiment_id="exp-1", test_runner=None):
        return lsr.replay(
            experiment_id, repo_root=self.repo, db_path=self.db_path, variant=variant,
            client=FakeClient(), worker_factory=lambda _client: worker, test_runner=test_runner,
        )


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.fx = ReplayFixture(self)

    def test_only_merged_test_files_come_in_never_source_files(self):
        seen = {}

        def inspecting_runner(worktree, tests, timeout):
            root = Path(worktree)
            seen["tests"] = list(tests)
            seen["calc"] = (root / "pkg/calc.py").read_text()
            seen["extra_exists"] = (root / "pkg/extra.py").exists()
            seen["test_calc"] = (root / "tests/test_calc.py").read_text()
            seen["helpers"] = (root / "tests/helpers.py").exists()
            seen["old"] = (root / "tests/test_old.py").exists()
            seen["gone"] = (root / "tests/test_gone.py").exists()
            seen["renamed"] = (root / "tests/test_renamed.py").exists()
            return lsr.run_merged_tests(worktree, tests, timeout)

        worker = FakeWorker(None)
        row = self.fx.replay(worker, test_runner=inspecting_runner)

        call = worker.calls[0]
        self.assertFalse(call["saw_merged_test"])
        self.assertEqual(call["head"], self.fx.base)
        self.assertEqual(call["sandbox"], "workspace-write")
        self.assertIn("Task: GitHub Issue #42: fix calc (exp-1)", call["task"])
        self.assertIn("Implement the following task in this repository.", call["task"])
        # Source files of the merged diff never reach the worktree.
        self.assertEqual(seen["calc"], BASE_CALC)
        self.assertFalse(seen["extra_exists"])
        # Test files do: added, renamed (old path gone), deleted, and helpers.
        self.assertEqual(seen["test_calc"], MERGED_TEST_CALC)
        self.assertTrue(seen["helpers"])
        self.assertTrue(seen["renamed"])
        self.assertFalse(seen["old"])
        self.assertFalse(seen["gone"])
        self.assertEqual(seen["tests"], ["tests/test_calc.py", "tests/test_renamed.py"])
        self.assertEqual(row["merged_commit"], self.fx.merged)
        self.assertEqual(self.fx.replay_worktrees(), [])
        # Main is untouched.
        self.assertEqual(_git(self.fx.repo, "rev-parse", "HEAD"), self.fx.source_only)
        self.assertEqual(_git(self.fx.repo, "status", "--porcelain"), "")

    def test_test_change_parser_never_returns_paths_outside_tests(self):
        changes = lsr.merged_test_changes(self.fx.repo, self.fx.base, self.fx.merged)
        self.assertEqual(
            [(c.path, c.delete) for c in changes],
            [("tests/helpers.py", False), ("tests/test_calc.py", False), ("tests/test_gone.py", True),
             ("tests/test_old.py", True), ("tests/test_renamed.py", False)],
        )
        self.assertFalse(lsr._safe_test_path("tests/../pkg/calc.py"))
        self.assertFalse(lsr._safe_test_path("/tests/x.py"))
        self.assertFalse(lsr._safe_test_path("pkg/tests/test_x.py"))

    def test_worktree_is_removed_when_the_worker_raises(self):
        row = self.fx.replay(FakeWorker(None, raises=True, rounds=2))
        self.assertEqual(row["status"], lsr.STATUS_ERROR)
        self.assertIn("local worker raised RuntimeError: model exploded", row["error"])
        self.assertEqual(row["rounds"], 2)
        self.assertEqual(self.fx.replay_worktrees(), [])
        self.assertFalse(row["full_pass"])

    def test_interruption_releases_lease_claim_and_worktree(self):
        class InterruptedWorker(FakeWorker):
            def run(self, **kwargs):
                super().run(**kwargs)
                raise KeyboardInterrupt("stopped")

        with self.assertRaises(KeyboardInterrupt):
            self.fx.replay(InterruptedWorker(FULL_CALC))
        self.assertEqual(self.fx.replay_worktrees(), [])
        self.assertEqual(self.fx.rows(), [])
        conn = sqlite3.connect(self.fx.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM local_replay_lease").fetchone()[0], 0)
        finally:
            conn.close()

    def test_worktree_is_removed_when_the_test_runner_raises(self):
        def broken(*_args):
            raise OSError("disk full")

        row = self.fx.replay(FakeWorker(FULL_CALC), test_runner=broken)
        self.assertEqual(row["status"], lsr.STATUS_ERROR)
        self.assertIn("disk full", row["error"])
        self.assertEqual(self.fx.replay_worktrees(), [])

    def test_rows_for_pass_partial_and_fail(self):
        full = self.fx.replay(FakeWorker(FULL_CALC, rounds=2), variant="full")
        partial = self.fx.replay(FakeWorker(PARTIAL_CALC), variant="partial")
        failed = self.fx.replay(FakeWorker(None, rounds=3), variant="fail")

        self.assertEqual((full["status"], full["tests_total"], full["tests_passed"]), ("scored", 4, 4))
        self.assertTrue(full["full_pass"])
        self.assertTrue(full["patch_applied"])
        self.assertTrue(full["compiled"])
        self.assertEqual(full["rounds"], 2)
        self.assertEqual(full["tokens"], 123)
        self.assertEqual(full["issue"], 42)
        self.assertEqual(full["model"], "fake-local-model")
        self.assertEqual(full["prompt_variant"], "full")
        self.assertIsNone(full["error"])
        self.assertGreaterEqual(full["seconds"], 0)

        self.assertEqual((partial["status"], partial["tests_total"], partial["tests_passed"]), ("scored", 4, 2))
        self.assertFalse(partial["full_pass"])
        self.assertTrue(partial["patch_applied"])

        self.assertEqual((failed["status"], failed["tests_total"], failed["tests_passed"]), ("scored", 4, 0))
        self.assertFalse(failed["patch_applied"])
        self.assertFalse(failed["compiled"])
        self.assertEqual(failed["rounds"], 3)
        self.assertIn("failed after 3 rounds", failed["error"])
        self.assertEqual(len(self.fx.rows()), 3)
        self.assertEqual(self.fx.replay_worktrees(), [])

        summary = lsr.variant_summary(db_path=self.fx.db_path)
        stats = {s["prompt_variant"]: s for s in summary["variants"]}
        self.assertEqual(stats["full"]["test_pass_rate"], 1.0)
        self.assertEqual(stats["partial"]["test_pass_rate"], 0.5)
        self.assertEqual(stats["fail"]["full_pass_rate"], 0.0)
        self.assertEqual(summary["common_experiments"], 1)

    def test_a_broken_python_file_is_not_compiled(self):
        row = self.fx.replay(FakeWorker("def broken(:\n"))
        self.assertTrue(row["patch_applied"])
        self.assertFalse(row["compiled"])
        self.assertEqual(row["tests_passed"], 0)

    def test_second_replay_with_the_same_variant_is_skipped_other_variant_runs(self):
        first = FakeWorker(FULL_CALC)
        self.assertIsNotNone(self.fx.replay(first, variant="v1"))
        again = FakeWorker(FULL_CALC)
        self.assertIsNone(self.fx.replay(again, variant="v1"))
        self.assertEqual(again.calls, [])
        conn = sqlite3.connect(self.fx.db_path)
        try:
            self.assertIsNone(lsr.next_candidate(conn, "v1"))
            self.assertEqual(lsr.next_candidate(conn, "v2"), "exp-1")
        finally:
            conn.close()
        self.assertIsNotNone(self.fx.replay(FakeWorker(FULL_CALC), variant="v2"))
        self.assertEqual(len(self.fx.rows()), 2)

    def test_live_claim_blocks_and_stale_claim_is_taken_over(self):
        conn = sqlite3.connect(self.fx.db_path)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with conn:
            conn.execute(
                "INSERT INTO local_replays (experiment_id, prompt_variant, status, created_at, "
                "claim_token, lease_expires_at) VALUES ('exp-1', 'baseline', 'pending', 'x', 'other', ?)",
                (future,),
            )
        worker = FakeWorker(FULL_CALC)
        self.assertIsNone(self.fx.replay(worker))
        self.assertEqual(worker.calls, [])
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with conn:
            conn.execute("UPDATE local_replays SET lease_expires_at=?", (past,))
        conn.close()
        row = self.fx.replay(worker)
        self.assertEqual(row["status"], "scored")
        self.assertEqual(len(self.fx.rows()), 1)

    def test_merged_diff_without_tests_is_stored_as_no_tests(self):
        self.fx.add_experiment("exp-src", merged=self.fx.source_only, base=self.fx.merged)
        worker = FakeWorker(FULL_CALC)
        row = self.fx.replay(worker, experiment_id="exp-src")
        self.assertEqual(row["status"], lsr.STATUS_NO_TESTS)
        self.assertEqual(worker.calls, [])
        self.assertFalse(row["full_pass"])

    def test_missing_merged_commit_is_an_error_row(self):
        self.fx.add_experiment("exp-lost", merged="f" * 40, base=self.fx.base)
        row = self.fx.replay(FakeWorker(FULL_CALC), experiment_id="exp-lost")
        self.assertEqual(row["status"], lsr.STATUS_ERROR)
        self.assertIn("not available", row["error"])

    def test_only_merged_paid_experiments_are_replayed(self):
        self.fx.add_experiment("exp-local", merged=self.fx.merged, base=self.fx.base, provider="local")
        self.fx.add_experiment("exp-open", merged=self.fx.merged, base=self.fx.base, adoption="needs_review")
        for experiment_id in ("exp-local", "exp-open", "nope"):
            with self.assertRaises(ValueError):
                self.fx.replay(FakeWorker(FULL_CALC), experiment_id=experiment_id)
        conn = sqlite3.connect(self.fx.db_path)
        try:
            self.assertEqual(lsr.next_candidate(conn, "baseline"), "exp-1")
        finally:
            conn.close()

    def test_default_worker_is_local_without_free_tier(self):
        worker = lsr.default_worker_factory(FakeClient())
        self.assertEqual(worker.provider, "local")
        self.assertIsNone(worker.planner("any prompt"))


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.fx = ReplayFixture(self)
        self.built = []

    def sweep(self, client, **kwargs):
        def factory(_client):
            worker = FakeWorker(FULL_CALC)
            self.built.append(worker)
            return worker

        return lsr.replay_next(repo_root=self.fx.repo, db_path=self.fx.db_path, client=client,
                               worker_factory=factory, **kwargs)

    def test_nothing_runs_when_the_server_cannot_be_reached(self):
        for client in (FakeClient(available=False), FakeClient(raises=True)):
            outcome = self.sweep(client)
            self.assertFalse(outcome["ran"])
            self.assertIn("unreachable", outcome["reason"])
        self.assertEqual(self.built, [])
        self.assertEqual(self.fx.rows(), [])

    def test_nothing_runs_when_the_server_is_busy(self):
        outcome = self.sweep(FakeClient(busy=1))
        self.assertFalse(outcome["ran"])
        self.assertIn("busy", outcome["reason"])

        conn = sqlite3.connect(self.fx.db_path)
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS local_llm_jobs (id TEXT, status TEXT)")
            conn.execute("INSERT INTO local_llm_jobs VALUES ('j1', 'running')")
        outcome = self.sweep(FakeClient())
        self.assertFalse(outcome["ran"])
        self.assertIn("local model jobs", outcome["reason"])
        with conn:
            conn.execute("DELETE FROM local_llm_jobs")
            conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at, implementation_provider) "
                "VALUES ('exp-run', 'g', 'implementing', 'now', 'local')"
            )
        conn.close()
        outcome = self.sweep(FakeClient())
        self.assertFalse(outcome["ran"])
        self.assertIn("local experiment", outcome["reason"])
        self.assertEqual(self.built, [])
        self.assertEqual(self.fx.rows(), [])

    def test_one_replay_per_sweep_then_nothing_left(self):
        self.fx.add_experiment("exp-2", merged=self.fx.merged, base=self.fx.base,
                               ended_at="2026-11-02T00:00:00+00:00")
        first = self.sweep(FakeClient())
        self.assertTrue(first["ran"])
        self.assertEqual(first["experiment_id"], "exp-1")  # oldest first
        self.assertEqual(len(self.built), 1)
        second = self.sweep(FakeClient())
        self.assertEqual(second["experiment_id"], "exp-2")
        third = self.sweep(FakeClient())
        self.assertFalse(third["ran"])
        self.assertIn("no merged paid experiment", third["reason"])
        self.assertEqual(len(self.built), 2)

    def test_variant_comes_from_the_environment(self):
        # #694 names the SR-grammar arm ``+sr+gbnf``; this test covers the ``+sr`` arm.
        with patch.dict(os.environ, {lsr.ENV_VARIANT: "  ctx  v2 ", "NEXUS_LOCAL_SR_GRAMMAR": "0"}):
            self.assertEqual(lsr.prompt_variant(), "ctx v2+sr")
            outcome = self.sweep(FakeClient())
        self.assertEqual(outcome["replay"]["prompt_variant"], "ctx v2+sr")
        with patch.dict(os.environ, {"NEXUS_LOCAL_SR_GRAMMAR": "0"}):
            self.assertEqual(lsr.prompt_variant(), "baseline+sr")

    def test_disabled_by_setting_env_or_pause(self):
        with patch.dict(os.environ, {lsr.ENV_ENABLED: "0"}):
            self.assertEqual(self.sweep(FakeClient())["reason"], "disabled")
        for key in (lsr.SETTING_ENABLED, "nexus.paused"):
            value = "0" if key == lsr.SETTING_ENABLED else "1"
            conn = sqlite3.connect(self.fx.db_path)
            with conn:
                conn.execute("DELETE FROM service_settings")
                conn.execute("INSERT INTO service_settings (key, value, updated_at) VALUES (?, ?, 'now')",
                             (key, value))
            conn.close()
            self.assertEqual(self.sweep(FakeClient())["reason"], "disabled")
        self.assertEqual(self.built, [])

    def test_unowned_replay_named_worktree_is_never_guessed_stale(self):
        os.makedirs(self.fx.worktrees, exist_ok=True)
        leftover = os.path.join(self.fx.worktrees, lsr.WORKTREE_PREFIX + "leftover")
        _git(self.fx.repo, "worktree", "add", "--detach", leftover, self.fx.base)
        self.assertTrue(self.sweep(FakeClient())["ran"])
        self.assertTrue(Path(leftover).exists())

    def test_concurrent_sweeps_run_only_one_worker_and_preserve_its_worktree(self):
        entered, release = threading.Event(), threading.Event()
        results = []

        class BlockingWorker(FakeWorker):
            def run(self, **kwargs):
                entered.set()
                self.assert_release = release.wait(5)
                return super().run(**kwargs)

        def first():
            results.append(lsr.replay_next(
                repo_root=self.fx.repo, db_path=self.fx.db_path, client=FakeClient(),
                worker_factory=lambda _client: BlockingWorker(FULL_CALC),
            ))

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(entered.wait(5))
        live = self.fx.replay_worktrees()
        self.assertTrue(live)
        second = self.sweep(FakeClient())
        self.assertFalse(second["ran"])
        self.assertIn("another local replay", second["reason"])
        self.assertEqual(self.fx.replay_worktrees(), live)
        release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]["ran"])
        self.assertEqual(len(self.fx.rows()), 1)
        self.assertEqual(self.fx.replay_worktrees(), [])

    def test_expired_live_lease_is_not_reclaimed_or_cleaned(self):
        os.makedirs(self.fx.worktrees, exist_ok=True)
        live = os.path.join(self.fx.worktrees, lsr.WORKTREE_PREFIX + "live")
        _git(self.fx.repo, "worktree", "add", "--detach", live, self.fx.base)
        conn = sqlite3.connect(self.fx.db_path)
        with conn:
            conn.execute(
                "INSERT INTO local_replay_lease VALUES (1, 'live', ?, ?, ?, 'old', ?, ?)",
                (lsr.socket.gethostname(), os.getpid(), lsr._process_started(os.getpid()),
                 (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), live),
            )
        conn.close()
        outcome = self.sweep(FakeClient())
        self.assertFalse(outcome["ran"])
        self.assertTrue(Path(live).exists())

    def test_expired_dead_lease_is_recovered_and_only_its_worktree_removed(self):
        os.makedirs(self.fx.worktrees, exist_ok=True)
        orphan = os.path.join(self.fx.worktrees, lsr.WORKTREE_PREFIX + "orphan")
        unrelated = os.path.join(self.fx.worktrees, lsr.WORKTREE_PREFIX + "unrelated")
        _git(self.fx.repo, "worktree", "add", "--detach", orphan, self.fx.base)
        _git(self.fx.repo, "worktree", "add", "--detach", unrelated, self.fx.base)
        conn = sqlite3.connect(self.fx.db_path)
        with conn:
            conn.execute(
                "INSERT INTO local_replay_lease VALUES (1, 'dead', ?, ?, 'impossible', 'old', ?, ?)",
                (lsr.socket.gethostname(), os.getpid(),
                 (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), orphan),
            )
        conn.close()
        self.assertTrue(self.sweep(FakeClient())["ran"])
        self.assertFalse(Path(orphan).exists())
        self.assertTrue(Path(unrelated).exists())

    def test_tick_never_fails_because_of_the_replay(self):
        with patch.object(lsr, "replay_next", side_effect=RuntimeError("boom")):
            nexus_tick._shadow_replay(self.fx.repo, self.fx.db_path)  # no exception


class AggregateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "agent.db")
        conn = init_db(self.db_path)
        rows = [
            # experiment, variant, status, total, passed, error
            ("e1", "baseline", "scored", 4, 4, None),
            ("e2", "baseline", "scored", 4, 2, None),
            ("e3", "baseline", "scored", 0, 0, None),             # zero tests: never a full pass
            ("e4", "baseline", "scored", 2, 2, "worker failed"),  # errored: never a full pass
            ("e5", "baseline", "no_tests", 0, 0, "no tests"),
            ("e6", "baseline", "error", 0, 0, "setup"),
            ("e1", "ctx", "scored", 4, 2, None),
            ("e2", "ctx", "scored", 4, 4, None),
            ("e7", "ctx", "scored", 3, 3, None),
            ("e8", "ctx", "pending", 0, 0, None),
        ]
        with conn:
            for i, (exp, variant, status, total, passed, error) in enumerate(rows):
                conn.execute(
                    "INSERT INTO local_replays (experiment_id, prompt_variant, status, tests_total, "
                    "tests_passed, error, created_at, finished_at, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'm')",
                    (exp, variant, status, total, passed, error, f"2026-11-0{i % 9 + 1}", f"2026-11-0{i % 9 + 1}"),
                )
            # #649 hunk units: one passed, one wrong, one timed out, one still running.
            for symbol, status, passed, overlap in (("add", "scored", 1, 1.0), ("sub", "scored", 0, 0.25),
                                                    ("mul", "timeout", 0, 0.0), ("div", "running", 0, 0.0)):
                conn.execute(
                    "INSERT INTO local_replay_units "
                    "(experiment_id,file,symbol,prompt_variant,model,status,applied,compiled,has_tests,"
                    "tests_passed,overlap,seconds,prompt_tokens,cached_tokens,created_at) "
                    "VALUES ('e1','pkg/calc.py',?,'baseline','m',?,1,1,1,?,?,2,100,25,'2026-11-01')",
                    (symbol, status, passed, overlap),
                )
        conn.close()

    def test_pass_rates_per_variant(self):
        summary = lsr.variant_summary(db_path=self.db_path)
        stats = {s["prompt_variant"]: s for s in summary["variants"]}
        base = stats["baseline"]
        self.assertEqual((base["replays"], base["scored"], base["no_tests"], base["errors"]), (6, 4, 1, 1))
        self.assertEqual((base["tests_passed"], base["tests_total"]), (8, 10))
        self.assertEqual(base["full_passes"], 1)
        self.assertEqual(base["full_pass_rate"], 0.25)
        ctx = stats["ctx"]
        self.assertEqual(ctx["replays"], 3)  # the pending claim is no replay
        self.assertEqual((ctx["tests_passed"], ctx["tests_total"], ctx["full_passes"]), (9, 11, 2))
        self.assertEqual(summary["common_experiments"], 2)  # e1 and e2
        common = {s["prompt_variant"]: s for s in summary["common"]}
        self.assertEqual((common["baseline"]["tests_passed"], common["baseline"]["tests_total"]), (6, 8))
        self.assertEqual((common["ctx"]["tests_passed"], common["ctx"]["tests_total"]), (6, 8))

    def test_missing_table_reads_as_empty(self):
        empty = str(Path(self.db_path).with_name("empty.db"))
        sqlite3.connect(empty).close()
        self.assertEqual(lsr.list_replays(5, db_path=empty), [])
        self.assertEqual(lsr.variant_summary(db_path=empty)["variants"], [])


class ReadToolTests(unittest.TestCase):
    def setUp(self):
        AggregateTests.setUp(self)  # same rows
        self.service = MagicMock()
        self.service.db_path = self.db_path
        mailbox = MagicMock()
        mailbox.recover_after_restart.return_value = None
        api.app.dependency_overrides[api.get_experiment_service] = lambda: self.service
        api.app.dependency_overrides[api.get_codex_service] = lambda: MagicMock()
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)

    def test_401_without_or_with_wrong_key(self):
        for headers in ({}, {"x-api-key": "wrong"}):
            self.assertEqual(self.client.get("/local-replays", headers=headers).status_code, 401)

    def test_lists_replays_and_variant_rates(self):
        response = self.client.get("/local-replays?limit=2", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertNotIn("e8", [r["experiment_id"] for r in body["replays"]])
        variants = {v["prompt_variant"]: v for v in body["variants"]}
        self.assertEqual(variants["baseline"]["full_pass_rate"], 0.25)
        self.assertEqual(body["common_experiments"], 2)
        units = body["unit_variants"][0]
        self.assertEqual(units["prompt_variant"], "baseline")
        self.assertEqual((units["units"], units["running"], units["completed"], units["timeouts"]),
                         (3, 1, 2, 1))
        self.assertEqual((units["tests_passed"], units["unit_pass_rate"], units["completion_rate"]),
                         (1, 0.3333, 0.6667))
        self.assertEqual(self.client.get("/local-replays?limit=0", headers=HEADERS).status_code, 422)

    def test_mcp_tool_is_read_only_and_calls_the_api(self):
        self.assertIn("local_replays_list", READ_ONLY_TOOLS)
        self.assertNotIn("local_replays_list", WRITE_TOOLS)

        class FakeMcp:
            def __init__(self):
                self.tools = {}
                self.session_manager = object()

            def tool(self, **_kwargs):
                def decorate(function):
                    self.tools[function.__name__] = function
                    return function
                return decorate

            def streamable_http_app(self, **_kwargs):
                return FastAPI()

        fake = FakeMcp()
        with (
            patch.object(nexus_mcp, "MCPServer", return_value=fake),
            patch.object(api, "_resolve_experiment_service", return_value=self.service),
        ):
            nexus_mcp._build_mount_app()
            listed = fake.tools["local_replays_list"](limit=3)
            with self.assertRaisesRegex(ValueError, "invalid limit"):
                fake.tools["local_replays_list"](limit=0)
        self.assertEqual(listed["count"], 3)
        self.assertIn("variants", listed)

    def test_operations_catalog_entry(self):
        registry = OperationsRegistry(experiment_service=MagicMock(), db_path=self.db_path)
        self.addCleanup(registry.close)
        specs = {spec.name: spec for spec in registry.catalog()}
        self.assertFalse(specs["local_replays_list"].mutates_state)
        result = registry.execute("local_replays_list", {"limit": 2})
        self.assertTrue(result.success)
        text = "\n".join(result.output_lines)
        self.assertIn("variant baseline: 6 replays, 4 scored, tests 8/10 (80%)", text)
        self.assertIn("all tests passed in 1/4 (25%)", text)
        self.assertIn("hunk variant baseline: 3 units from 1 experiments, finished in time 2/3 (67%), "
                      "timeouts 1", text)


if __name__ == "__main__":
    unittest.main()
