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
        # #811: the hint forbids the deployed checkout but must not name its
        # absolute path - a path named in the prompt is a path the agent can
        # write to (run 1b9c8642/#797 wrote into the checkout the docs named).
        self.assertIn(f"Work only inside your working directory {self.wt}", worker.task)
        self.assertIn("off-limits", worker.task)
        self.assertNotIn(str(self.repo), worker.task)
        self.elog.error.assert_not_called()

    def test_absolute_paths_of_the_deployed_checkout_are_scrubbed_from_the_prompt(self):
        worker = FakeWorker()
        task = (
            f"Fix the bug described in {self.repo}/OPERATIONS.md; see also "
            f"{self.repo}/modules/nexus_tick.py and the checkout at {self.repo}."
        )
        result = GuardedWorker(worker, str(self.repo)).run(
            task, str(self.wt), experiment_logger=self.elog, phase="implementation")
        self.assertTrue(result.success)
        self.assertNotIn(str(self.repo), worker.task)
        self.assertIn("<deployed-checkout>/OPERATIONS.md", worker.task)
        self.assertIn("<deployed-checkout>/modules/nexus_tick.py", worker.task)
        self.assertIn("the checkout at <deployed-checkout>.", worker.task)

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
        # #811: the event carries the recordable cause so the weekly review can
        # count these runs without re-deriving them from the message text.
        self.assertEqual(kwargs["no_changes_cause"], "wrote_outside_worktree")

    def test_a_prompt_that_named_the_checkout_is_recorded_when_the_guard_fires(self):
        worker = FakeWorker(write_to=self.repo)
        task = f"As documented in {self.repo}/README.md, edit {self.repo}/nexus_tick.py."
        GuardedWorker(worker, str(self.repo)).run(
            task, str(self.wt), experiment_logger=self.elog, phase="implementation")
        args, kwargs = self.elog.error.call_args
        self.assertTrue(kwargs["prompt_mentions_repo_root"])
        self.assertGreater(kwargs["repo_root_mentions_scrubbed"], 0)
        self.assertNotIn(str(self.repo), worker.task)

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
