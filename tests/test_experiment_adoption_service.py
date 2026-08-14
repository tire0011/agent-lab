from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules.experiment_adoption_service import (
    STATUS_ADOPTED,
    STATUS_ADOPTING,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_STARTED,
    STATUS_QUEUED,
    ExperimentAdoptionError,
    ExperimentAdoptionService,
)
from modules.experiment_service import ExperimentService


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


class AdoptionRepoFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.origin = root / "origin.git"
        self.worktrees = root / "worktrees"
        self.logs = root / "logs"
        self.db_path = str(root / "agent.db")
        self.lock_path = str(root / "adoption.lock")
        self.repo.mkdir()
        self.worktrees.mkdir()
        self.logs.mkdir()
        git(self.repo.parent, "init", "--bare", str(self.origin))
        git(self.repo.parent, "init", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_app.py").write_text(
            "import unittest\n\n"
            "class AppTests(unittest.TestCase):\n"
            "    def test_true(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.repo / "communication" / "outbox").mkdir(parents=True)
        (self.repo / "communication" / "outbox" / ".gitkeep").write_text("", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "init")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-u", "origin", "main")

        self.experiment_service = ExperimentService(
            selection_worker=MagicMock(),
            worker_factory=lambda _: MagicMock(),
            plan_worker_factory=lambda _: MagicMock(),
            db_path=self.db_path,
            repo_root=str(self.repo),
            worktrees_root=str(self.worktrees),
            test_python_executable=sys.executable,
            experiment_log_dir=str(self.logs),
        )
        self.adoption_service = ExperimentAdoptionService(
            experiment_service=self.experiment_service,
            db_path=self.db_path,
            repo_root=str(self.repo),
            worktrees_root=str(self.worktrees),
            lock_path=self.lock_path,
        )

    def close(self) -> None:
        self.adoption_service.close()
        self.experiment_service.close()
        self.tmp.cleanup()

    def create_experiment(
        self,
        *,
        experiment_id: str,
        changed_files: list[str],
        status: str = "succeeded",
        quality_outcome: str = "pass",
        mutate: dict[str, str] | None = None,
        extra_untracked: dict[str, str] | None = None,
    ) -> str:
        worktree = self.worktrees / f"exp-{experiment_id[:8]}"
        git(self.repo, "worktree", "add", "-b", f"exp-{experiment_id[:8]}", str(worktree), "HEAD")
        for rel_path, content in (mutate or {"app.py": f"VALUE = '{experiment_id[:8]}'\n"}).items():
            target = worktree / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for rel_path, content in (extra_untracked or {}).items():
            target = worktree / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        snapshot = self.adoption_service._capture_snapshot(
            str(worktree),
            self.experiment_service.get_experiment_logger(experiment_id),
            phase="test",
        )
        plan = {
            "title": "Adoptable change",
            "affected_files": changed_files,
            "change_kind": "implementation",
            "required_artifacts": changed_files,
            "acceptance_criteria": ["tests pass"],
        }
        quality = {
            "outcome": quality_outcome,
            "reason": "ok" if quality_outcome == "pass" else "no",
            "checks": [],
            "expected_files": changed_files,
            "changed_files": changed_files,
            "missing_artifacts": [],
        }
        with self.experiment_service._lock, self.experiment_service._conn:
            self.experiment_service._conn.execute(
                """
                INSERT INTO experiments (
                    id, goal, status, created_at, ended_at, plan_json, worktree_path,
                    branch_name, test_command_json, test_exit_code, git_status,
                    diff_stat, diff_text, quality_evaluation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    "Implement something small.",
                    status,
                    "2026-08-14T12:00:00Z",
                    "2026-08-14T12:10:00Z",
                    json.dumps(plan),
                    str(worktree),
                    f"exp-{experiment_id[:8]}",
                    json.dumps([sys.executable, "-m", "unittest", "discover", "-s", "tests"]),
                    0,
                    snapshot["git_status"],
                    snapshot["diff_stat"],
                    snapshot["diff_text"],
                    json.dumps(quality),
                ),
            )
        return experiment_id

    def wait_terminal(self, experiment_id: str, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.adoption_service.wait_for_thread(experiment_id, timeout=0.1)
            adoption = self.adoption_service.get(experiment_id)
            if adoption.status not in {STATUS_QUEUED, STATUS_ADOPTING}:
                return adoption
            time.sleep(0.05)
        raise AssertionError(f"adoption did not finish for {experiment_id}")


class ExperimentAdoptionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)

    def test_adopts_valid_successful_experiment(self):
        experiment_id = "a" * 32
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py", "feature.py"],
            extra_untracked={"feature.py": "FEATURE = True\n"},
        )

        recorded: list[list[str]] = []
        original_run_git = __import__("modules.experiment_adoption_service", fromlist=["_run_git"])._run_git

        def spy(args, **kwargs):
            recorded.append(list(args))
            return original_run_git(args, **kwargs)

        with patch("modules.experiment_adoption_service._run_git", side_effect=spy):
            started = self.fx.adoption_service.start_adoption(experiment_id)
            self.assertIn(started.status, {STATUS_QUEUED, STATUS_ADOPTING})
            adoption = self.fx.wait_terminal(experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.test_exit_code, 0)
        self.assertEqual(adoption.actual_files, ["app.py", "feature.py"])
        remote_files = git(self.fx.repo, "show", "origin/main:feature.py").stdout
        self.assertIn("FEATURE = True", remote_files)
        push_commands = [cmd for cmd in recorded if cmd[:2] == ["push", "origin"]]
        self.assertEqual(len(push_commands), 1)
        self.assertNotIn("--force", push_commands[0])
        self.assertNotIn("-f", push_commands[0])
        self.assertNotIn("--force-with-lease", push_commands[0])
        self.assertFalse((Path(adoption.diagnostic_worktree_path) if adoption.diagnostic_worktree_path else Path(self.fx.worktrees / "missing")).exists())

    def test_background_thread_runs_after_start_returns(self):
        experiment_id = "b" * 32
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        gate = threading.Event()
        ready = threading.Event()
        original_run = subprocess.run

        def blocking_run(args, **kwargs):
            if isinstance(args, list) and args[:3] == [sys.executable, "-m", "unittest"]:
                ready.set()
                gate.wait(5)
            return original_run(args, **kwargs)

        with patch("modules.experiment_adoption_service.subprocess.run", side_effect=blocking_run):
            started = self.fx.adoption_service.start_adoption(experiment_id)
            self.assertIn(started.status, {STATUS_QUEUED, STATUS_ADOPTING})
            self.assertTrue(ready.wait(5))
            self.assertTrue(self.fx.adoption_service._threads[experiment_id].is_alive())
            gate.set()
            adoption = self.fx.wait_terminal(experiment_id)
        self.assertEqual(adoption.status, STATUS_ADOPTED)

    def test_idempotent_double_adopt_does_not_create_second_push(self):
        experiment_id = "c" * 32
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        first = self.fx.adoption_service.start_adoption(experiment_id)
        self.assertIn(first.status, {STATUS_QUEUED, STATUS_ADOPTING})
        adopted = self.fx.wait_terminal(experiment_id)
        before = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()
        second = self.fx.adoption_service.start_adoption(experiment_id)
        after = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()
        self.assertEqual(second.status, STATUS_ADOPTED)
        self.assertEqual(adopted.integrated_commit_sha, second.integrated_commit_sha)
        self.assertEqual(before, after)

    def test_rejects_when_quality_gate_failed(self):
        experiment_id = "d" * 32
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"], quality_outcome="needs_review")
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service.start_adoption(experiment_id)
        self.assertEqual(self.fx.adoption_service.get(experiment_id).status, STATUS_NOT_STARTED)

    def test_rejects_failed_needs_review_and_running_experiment_statuses(self):
        for prefix, status in (("e", "failed"), ("f", "needs_review"), ("1", "implementing")):
            experiment_id = prefix * 32
            self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"], status=status)
            with self.assertRaises(ExperimentAdoptionError):
                self.fx.adoption_service.start_adoption(experiment_id)
            self.assertEqual(self.fx.adoption_service.get(experiment_id).status, STATUS_NOT_STARTED)

    def test_rejects_missing_worktree(self):
        experiment_id = "7" * 32
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        worktree = self.fx.experiment_service.get_experiment(experiment_id).worktree_path
        shutil.rmtree(worktree)
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service.start_adoption(experiment_id)

    def test_rejects_changed_worktree_since_verification(self):
        experiment_id = "8" * 32
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        worktree = Path(self.fx.experiment_service.get_experiment(experiment_id).worktree_path)
        (worktree / "later.py").write_text("x = 1\n", encoding="utf-8")
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service.start_adoption(experiment_id)

    def test_outbox_file_never_staged_and_marked_for_review(self):
        experiment_id = "9" * 32
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py", "communication/outbox/blocked.md"],
            extra_untracked={"communication/outbox/blocked.md": "forbidden\n"},
        )
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service.start_adoption(experiment_id)
        adoption = self.fx.adoption_service.get(experiment_id)
        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertIn("communication/outbox", adoption.reason)
        log = git(self.fx.repo, "log", "--oneline", "origin/main").stdout
        self.assertNotIn("blocked", log)

    def test_unexpected_files_require_review(self):
        experiment_id = "a1" * 16
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py"],
            extra_untracked={"unexpected.py": "SURPRISE = True\n"},
        )
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service.start_adoption(experiment_id)
        self.assertEqual(self.fx.adoption_service.get(experiment_id).status, STATUS_NEEDS_REVIEW)

    def test_cherry_pick_conflict_keeps_diagnostic_worktree_and_skips_push(self):
        experiment_id = "b1" * 16
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py"],
            mutate={"app.py": "VALUE = 'experiment'\n"},
        )
        (self.fx.repo / "app.py").write_text("VALUE = 'main-change'\n", encoding="utf-8")
        git(self.fx.repo, "commit", "-am", "main change")
        git(self.fx.repo, "push", "origin", "main")

        self.fx.adoption_service.start_adoption(experiment_id)
        adoption = self.fx.wait_terminal(experiment_id)
        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertIn("Cherry-Pick", adoption.reason)
        self.assertTrue(Path(adoption.diagnostic_worktree_path).exists())

    def test_integration_test_failure_prevents_push(self):
        experiment_id = "c1" * 16
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["tests/test_app.py"],
            mutate={
                "tests/test_app.py": (
                    "import unittest\n\n"
                    "class BrokenTests(unittest.TestCase):\n"
                    "    def test_fail(self):\n"
                    "        self.assertEqual(1, 2)\n"
                )
            },
        )
        before = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()
        self.fx.adoption_service.start_adoption(experiment_id)
        adoption = self.fx.wait_terminal(experiment_id)
        after = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()
        self.assertEqual(adoption.status, STATUS_FAILED)
        self.assertEqual(before, after)
        self.assertTrue(Path(adoption.diagnostic_worktree_path).exists())

    def test_integrated_quality_evaluation_failure_prevents_push(self):
        experiment_id = "d1" * 16
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "UPDATE experiments SET plan_json = ? WHERE id = ?",
                (
                    json.dumps({
                        "title": "Mismatch plan",
                        "affected_files": ["planned.py"],
                        "change_kind": "implementation",
                        "required_artifacts": ["planned.py"],
                        "acceptance_criteria": ["tests pass"],
                    }),
                    experiment_id,
                ),
            )
        self.fx.adoption_service.start_adoption(experiment_id)
        adoption = self.fx.wait_terminal(experiment_id)
        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertIn("Qualitäts-Gate", adoption.reason)

    def test_remote_main_changes_during_push_sets_needs_review(self):
        experiment_id = "e1" * 16
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        original_run_git = __import__("modules.experiment_adoption_service", fromlist=["_run_git"])._run_git
        triggered = {"done": False}

        def side_effect(args, **kwargs):
            if args[:2] == ["push", "origin"] and not triggered["done"]:
                triggered["done"] = True
                other = Path(self.fx.tmp.name) / "other"
                git(other.parent, "clone", str(self.fx.origin), str(other))
                git(other, "config", "user.email", "other@example.com")
                git(other, "config", "user.name", "Other User")
                git(other, "checkout", "main")
                (other / "remote_change.txt").write_text("remote\n", encoding="utf-8")
                git(other, "add", "remote_change.txt")
                git(other, "commit", "-m", "remote")
                git(other, "push", "origin", "main")
            return original_run_git(args, **kwargs)

        with patch("modules.experiment_adoption_service._run_git", side_effect=side_effect):
            self.fx.adoption_service.start_adoption(experiment_id)
            adoption = self.fx.wait_terminal(experiment_id)
        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertIn("origin/main", adoption.reason)

    def test_recover_after_restart_marks_interrupted(self):
        experiment_id = "f1" * 16
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, created_at, updated_at, target_branch
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("row1", experiment_id, STATUS_ADOPTING, "2026-08-14T12:00:00Z", "2026-08-14T12:00:00Z", "main"),
            )
        restarted = ExperimentAdoptionService(
            experiment_service=self.fx.experiment_service,
            db_path=self.fx.db_path,
            repo_root=str(self.fx.repo),
            worktrees_root=str(self.fx.worktrees),
            lock_path=self.fx.lock_path,
        )
        self.addCleanup(restarted.close)
        marked = restarted.recover_after_restart()
        self.assertEqual(marked, [experiment_id])
        self.assertEqual(restarted.get(experiment_id).status, STATUS_INTERRUPTED)

    def test_parallel_adoption_is_prevented(self):
        first_id = "01" * 16
        second_id = "02" * 16
        self.fx.create_experiment(experiment_id=first_id, changed_files=["app.py"])
        self.fx.create_experiment(experiment_id=second_id, changed_files=["app.py"])
        gate = threading.Event()
        ready = threading.Event()
        original_run = subprocess.run

        def blocking_run(args, **kwargs):
            if isinstance(args, list) and args[:3] == [sys.executable, "-m", "unittest"]:
                ready.set()
                gate.wait(5)
            return original_run(args, **kwargs)

        with patch("modules.experiment_adoption_service.subprocess.run", side_effect=blocking_run):
            self.fx.adoption_service.start_adoption(first_id)
            self.assertTrue(ready.wait(5))
            with self.assertRaises(ExperimentAdoptionError):
                self.fx.adoption_service.start_adoption(second_id)
            gate.set()
            adoption = self.fx.wait_terminal(first_id)
        self.assertEqual(adoption.status, STATUS_ADOPTED)


if __name__ == "__main__":
    unittest.main()
