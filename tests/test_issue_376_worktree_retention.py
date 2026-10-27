"""#376: worktrees of expired terminal experiments are garbage-collected.

Every experiment keeps its worktree after it ends, and nothing ever removed
it, so finished runs silently filled the disk. A retention pass now removes
the worktree directory of terminal experiments whose ``ended_at`` is older
than ``NEXUS_EXPERIMENT_WORKTREE_RETENTION_HOURS``, keeping the experiment
row, and never touches non-terminal or recent experiments.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import api
from modules.experiment_service import (
    DEFAULT_WORKTREE_RETENTION_HOURS,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_SUCCEEDED,
    STATUS_VERIFYING,
    WORKTREE_RETENTION_ENV_VAR,
    ExperimentService,
    worktree_retention_hours,
)
from modules.experiment_service import service as service_module

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(hours=200)).isoformat()
RECENT = (NOW - timedelta(hours=2)).isoformat()


class WorktreeRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worktrees = self.root / "worktrees"
        self.service = ExperimentService(
            selection_worker=MagicMock(),
            worker_factory=lambda _path: MagicMock(),
            plan_worker_factory=lambda _path: MagicMock(),
            db_path=str(self.root / "agent.db"),
            repo_root=str(self.root),
            worktrees_root=str(self.worktrees),
            experiment_log_dir=str(self.root / "logs"),
        )
        self.addCleanup(self.service.close)

    def _experiment(
        self, status: str, ended_at: str | None, *, create_dir: bool = True, path: Path | None = None
    ) -> tuple[str, Path]:
        experiment = self.service.create_experiment("goal")
        path = path or self.worktrees / experiment.id
        if create_dir:
            path.mkdir(parents=True)
            (path / "file.txt").write_text("content")
        with self.service._conn:
            self.service._conn.execute(
                "UPDATE experiments SET status = ?, ended_at = ?, worktree_path = ?, branch_name = ? "
                "WHERE id = ?",
                (status, ended_at, str(path), f"experiment/{experiment.id}", experiment.id),
            )
        return experiment.id, path

    def _adoption(self, experiment_id: str, status: str) -> None:
        with self.service._conn:
            self.service._conn.execute(
                "INSERT INTO experiment_adoptions (id, experiment_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"adopt-{experiment_id}", experiment_id, status, OLD, OLD),
            )

    def _events(self, experiment_id: str) -> list[str]:
        return [entry["event"] for entry in self.service.list_activity(experiment_id=experiment_id)]

    def _cleanup(self, hours: float = 168):
        return self.service.cleanup_expired_worktrees(hours, now=NOW)

    def test_expired_terminal_worktrees_are_removed_and_metadata_kept(self):
        ids = {status: self._experiment(status, OLD) for status in (
            STATUS_SUCCEEDED, STATUS_FAILED, STATUS_INTERRUPTED, STATUS_NEEDS_REVIEW, STATUS_CANCELLED,
        )}

        result = self._cleanup()

        self.assertEqual(set(result.removed), {exp_id for exp_id, _ in ids.values()})
        self.assertEqual(result.errors, {})
        for status, (exp_id, path) in ids.items():
            self.assertFalse(path.exists())
            experiment = self.service.get_experiment(exp_id)
            self.assertEqual(experiment.status, status)
            self.assertEqual(experiment.worktree_path, str(path))
            self.assertEqual(experiment.branch_name, f"experiment/{exp_id}")
            self.assertIn("worktree_removed", self._events(exp_id))
        self.assertTrue(self.worktrees.is_dir())

    def test_recent_terminal_worktree_is_preserved(self):
        exp_id, path = self._experiment(STATUS_SUCCEEDED, RECENT)

        result = self._cleanup()

        self.assertEqual(result.removed, [])
        self.assertTrue((path / "file.txt").exists())
        self.assertNotIn("worktree_removed", self._events(exp_id))

    def test_non_terminal_worktrees_are_preserved_even_with_old_timestamps(self):
        paths = [
            self._experiment(status, OLD)[1]
            for status in (STATUS_QUEUED, STATUS_IMPLEMENTING, STATUS_VERIFYING)
        ]

        result = self._cleanup()

        self.assertEqual(result.removed, [])
        for path in paths:
            self.assertTrue((path / "file.txt").exists())

    def test_terminal_without_ended_at_is_preserved(self):
        _, path = self._experiment(STATUS_FAILED, None)

        self.assertEqual(self._cleanup().removed, [])
        self.assertTrue(path.exists())

    def test_unfinished_adoption_keeps_worktree_and_finished_one_does_not(self):
        active_id, active_path = self._experiment(STATUS_SUCCEEDED, OLD)
        self._adoption(active_id, "adopting")
        done_id, done_path = self._experiment(STATUS_SUCCEEDED, OLD)
        self._adoption(done_id, "adopted")

        result = self._cleanup()

        self.assertEqual(result.removed, [done_id])
        self.assertTrue(active_path.exists())
        self.assertFalse(done_path.exists())

    def test_missing_directory_and_repeated_passes_are_noops(self):
        missing_id, _ = self._experiment(STATUS_FAILED, OLD, create_dir=False)
        exp_id, path = self._experiment(STATUS_FAILED, OLD)

        first = self._cleanup()
        second = self._cleanup()

        self.assertEqual(first.removed, [exp_id])
        self.assertEqual(first.errors, {})
        self.assertEqual(second.removed, [])
        self.assertEqual(second.errors, {})
        self.assertFalse(path.exists())
        self.assertEqual(self._events(exp_id).count("worktree_removed"), 1)
        self.assertFalse(
            {"worktree_removed", "worktree_cleanup_failed"} & set(self._events(missing_id))
        )

    def test_cleanup_error_is_recorded_and_other_worktrees_still_removed(self):
        bad_id, bad_path = self._experiment(STATUS_FAILED, OLD)
        good_id, good_path = self._experiment(STATUS_FAILED, OLD)
        real_rmtree = service_module.shutil.rmtree

        def rmtree(path, *args, **kwargs):
            if Path(path) == bad_path:
                raise PermissionError("permission denied")
            return real_rmtree(path, *args, **kwargs)

        with patch.object(service_module.shutil, "rmtree", side_effect=rmtree):
            result = self._cleanup()

        self.assertEqual(result.removed, [good_id])
        self.assertIn(bad_id, result.errors)
        self.assertIn("permission denied", result.errors[bad_id])
        self.assertTrue(bad_path.exists())
        self.assertFalse(good_path.exists())
        self.assertIn("worktree_cleanup_failed", self._events(bad_id))
        self.assertEqual(self.service.get_experiment(bad_id).status, STATUS_FAILED)

    def test_path_outside_worktrees_root_is_never_removed(self):
        outside = self.root / "elsewhere"
        exp_id, path = self._experiment(STATUS_FAILED, OLD, path=outside)

        result = self._cleanup()

        self.assertEqual(result.removed, [])
        self.assertIn(exp_id, result.errors)
        self.assertTrue((path / "file.txt").exists())

    def test_zero_retention_disables_cleanup(self):
        _, path = self._experiment(STATUS_FAILED, OLD)

        self.assertEqual(self._cleanup(0).removed, [])
        self.assertTrue(path.exists())

    def test_retention_threshold_comes_from_environment(self):
        _, path = self._experiment(STATUS_FAILED, OLD)  # 200 h old

        with patch.dict(os.environ, {WORKTREE_RETENTION_ENV_VAR: "300"}):
            self.assertEqual(self.service.cleanup_expired_worktrees(now=NOW).removed, [])
        self.assertTrue(path.exists())
        with patch.dict(os.environ, {WORKTREE_RETENTION_ENV_VAR: "100"}):
            self.assertEqual(len(self.service.cleanup_expired_worktrees(now=NOW).removed), 1)
        self.assertFalse(path.exists())

    def test_real_git_worktree_is_unregistered(self):
        repo = self.root / "repo"
        repo.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "init", "-q", str(repo)], check=True)
        (repo / "a.txt").write_text("a")
        subprocess.run([*git, "-C", str(repo), "add", "a.txt"], check=True)
        subprocess.run([*git, "-C", str(repo), "commit", "-qm", "init"], check=True)
        self.service.repo_root = str(repo)
        exp_id, path = self._experiment(STATUS_SUCCEEDED, OLD, create_dir=False)
        subprocess.run(
            [*git, "-C", str(repo), "worktree", "add", "-q", "-b", f"experiment/{exp_id}", str(path)],
            check=True,
        )

        self.assertEqual(self._cleanup().removed, [exp_id])

        self.assertFalse(path.exists())
        listed = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertNotIn(str(path), listed)
        branches = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", f"experiment/{exp_id}"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertIn(exp_id, branches)


class RetentionConfigTests(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(worktree_retention_hours(""), DEFAULT_WORKTREE_RETENTION_HOURS)
        self.assertEqual(worktree_retention_hours("48"), 48)
        self.assertEqual(worktree_retention_hours("1.5"), 1.5)
        for disabled in ("0", "-5", "soon", "inf", "nan"):
            self.assertEqual(worktree_retention_hours(disabled), 0, disabled)

    def test_unset_environment_uses_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(WORKTREE_RETENTION_ENV_VAR, None)
            self.assertEqual(worktree_retention_hours(), DEFAULT_WORKTREE_RETENTION_HOURS)


class StartupHookTests(unittest.TestCase):
    def _run_lifespan(self, experiments: MagicMock) -> MagicMock:
        mailbox = MagicMock()
        mailbox.github_work_tracking.readiness_error.return_value = None
        codex = MagicMock()
        codex.mark_interrupted_tasks_failed.return_value = []
        api.app.dependency_overrides[api.get_experiment_service] = lambda: experiments
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        api.app.dependency_overrides[api.get_codex_service] = lambda: codex
        api.app.dependency_overrides[api.get_experiment_adoption_service] = MagicMock
        api.app.dependency_overrides[api.get_operation_chain_service] = MagicMock
        self.addCleanup(api.app.dependency_overrides.clear)
        with patch.object(api, "_resume_blocked_capabilities_after_startup"), \
                patch.object(api, "MailboxTaskWorker"):
            with TestClient(api.app):
                pass
        return mailbox

    def test_cleanup_runs_after_interrupted_reconciliation(self):
        calls: list[str] = []
        experiments = MagicMock()
        experiments.mark_interrupted_experiments_failed.side_effect = lambda: calls.append("mark") or []
        experiments.cleanup_expired_worktrees.side_effect = lambda: calls.append("cleanup")

        self._run_lifespan(experiments)

        self.assertEqual(calls, ["mark", "cleanup"])

    def test_failing_cleanup_does_not_block_startup(self):
        experiments = MagicMock()
        experiments.mark_interrupted_experiments_failed.return_value = []
        experiments.cleanup_expired_worktrees.side_effect = RuntimeError("disk on fire")

        mailbox = self._run_lifespan(experiments)

        mailbox.recover_after_restart.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
