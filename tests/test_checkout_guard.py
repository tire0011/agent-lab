"""Coding agents must stay out of the deployed checkout (2026-11-22: an agent
edited /agent/nexus_tick.py instead of its worktree; it ran for ten hours)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from modules.checkout_guard import EVENT, GuardedWorker
from modules.coding_worker import WorkerResult
from modules.experiment_resume import resume_experiment
from modules.experiment_service import ExperimentNotRunnableError


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def _result(**kw):
    base = dict(success=True, exit_code=0, stdout="", stderr="", runtime_ms=1, timed_out=False,
                stdout_truncated=False, stderr_truncated=False, thread_id=None, final_message="ok",
                usage=None, task="", working_dir="", sandbox="workspace-write", error=None)
    base.update(kw)
    return WorkerResult(**base)


class FakeWorker:
    provider = "mistral"

    def __init__(self, write_to=None):
        self.write_to = write_to
        self.task = None

    def run(self, task, working_dir, **kwargs):
        self.task = task
        (Path(working_dir) / "inside.txt").write_text("ok")
        if self.write_to is not None:
            (Path(self.write_to) / "nexus_tick.py").write_text("changed by the agent\n")
        return _result(task=task, working_dir=working_dir)


class GuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "agent"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        (self.repo / "nexus_tick.py").write_text("original\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.wt = Path(tmp.name) / "wt"
        self.wt.mkdir()
        self.elog = mock.Mock()

    def run_guarded(self, worker):
        return GuardedWorker(worker, str(self.repo)).run(
            "Fix it", str(self.wt), experiment_logger=self.elog, phase="implementation")

    def test_a_run_inside_its_worktree_passes_and_gets_the_hint(self):
        worker = FakeWorker()
        result = self.run_guarded(worker)
        self.assertTrue(result.success)
        self.assertIn(f"The deployed checkout {self.repo} is off-limits", worker.task)
        self.elog.error.assert_not_called()

    def test_writing_into_the_deployed_checkout_fails_the_run_and_is_stashed(self):
        result = self.run_guarded(FakeWorker(write_to=self.repo))
        self.assertFalse(result.success)
        self.assertIn("wrote outside its worktree", result.error)
        self.assertIn("nexus_tick.py", result.error)
        self.assertEqual((self.repo / "nexus_tick.py").read_text(), "original\n")
        stash = subprocess.run(["git", "-C", str(self.repo), "stash", "list"],
                               capture_output=True, text=True).stdout
        self.assertIn("mistral wrote outside", stash)
        args, kwargs = self.elog.error.call_args
        self.assertEqual(args[1], EVENT)
        self.assertEqual(kwargs["provider"], "mistral")
        self.assertEqual(kwargs["files"], ["nexus_tick.py"])
        self.assertFalse(kwargs["prompt_mentions_repo_root"])

    def test_attributes_pass_through_to_the_wrapped_worker(self):
        self.assertEqual(GuardedWorker(FakeWorker(), str(self.repo)).provider, "mistral")


class ResumeWithoutChangesTests(unittest.TestCase):
    def test_an_experiment_without_changes_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _git(tmp, "init", "-q")
            Path(tmp, "a.txt").write_text("a")
            _git(tmp, "add", ".")
            _git(tmp, "commit", "-q", "-m", "init")
            head = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            experiment = SimpleNamespace(id="e1", status="needs_review", worktree_path=tmp,
                                         base_commit_sha=head)
            service = mock.Mock()
            service.resolve_experiment.return_value = experiment
            with self.assertRaisesRegex(ExperimentNotRunnableError, "produced no changes"):
                resume_experiment(service, "e1")
            service._verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ProductionSnapshotTests(unittest.TestCase):
    def test_agents_get_a_copy_of_the_production_db_never_the_live_file(self):
        """Owner 2026-11-26: measurements may read real data; writes must not reach it."""
        import os
        import sqlite3

        from modules import coding_worker, verification_env

        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "agent.db"
            with sqlite3.connect(live) as conn:
                conn.execute("CREATE TABLE experiments (id TEXT)")
                conn.execute("INSERT INTO experiments VALUES ('e1')")
            with mock.patch.dict(os.environ, {"AGENT_DB_PATH": str(live)}), \
                    mock.patch.object(verification_env.tempfile, "gettempdir", return_value=tmp):
                env = coding_worker.filtered_env()
            snapshot = env[coding_worker.PROD_DB_SNAPSHOT_ENV]
            self.assertNotEqual(Path(snapshot), live)
            self.assertTrue(snapshot.startswith(env["TMPDIR"]))
            with sqlite3.connect(snapshot) as conn:
                self.assertEqual(conn.execute("SELECT id FROM experiments").fetchall(), [("e1",)])
                conn.execute("DELETE FROM experiments")
            with sqlite3.connect(live) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0], 1)

    def test_no_snapshot_without_a_database(self):
        import os

        from modules import coding_worker

        with mock.patch.dict(os.environ, {"AGENT_DB_PATH": "/nonexistent/agent.db"}):
            self.assertNotIn(coding_worker.PROD_DB_SNAPSHOT_ENV, coding_worker.filtered_env())
