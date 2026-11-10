"""A pipeline crash records the phase and the exception type (#410).

Experiment #350 failed with only "GitHub HTTP 422: Reference already exists";
nothing said which phase ran or what raised.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from modules.experiment_service import ExperimentService
from modules.experiment_service.models import PIPELINE_CRASH_ERROR_PREFIX


class GitHubWorkTrackingError(RuntimeError):
    pass


class FailurePhaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db_path = str(self.root / "agent.db")
        self.service = ExperimentService(
            selection_worker=MagicMock(), plan_worker_factory=lambda p: MagicMock(),
            worker_factory=lambda p: MagicMock(), db_path=self.db_path,
            repo_root=str(self.root), worktrees_root=str(self.root / "wt"),
            experiment_log_dir=str(self.root / "logs"),
        )
        self.addCleanup(self.service.close)

    def _crash(self, *phases, exc):
        exp = self.service.create_experiment("GitHub Issue #350: something")

        def pipeline(experiment_id, **_kw):
            elog = self.service.get_experiment_logger(experiment_id)
            for phase in phases:
                elog.info(phase, "step", f"in {phase}")
            raise exc

        self.service._run_pipeline = pipeline
        return self.service.run_experiment(exp.id)

    def test_a_crash_in_the_github_branch_phase_names_phase_and_type(self):
        result = self._crash("worktree", "implementation", "github_branch",
                             exc=GitHubWorkTrackingError("GitHub HTTP 422: Reference already exists"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_phase, "github_branch")
        self.assertEqual(result.failure_type, "GitHubWorkTrackingError")
        self.assertTrue(result.error.startswith(PIPELINE_CRASH_ERROR_PREFIX))  # still a crash (#467)
        self.assertIn("GitHubWorkTrackingError in phase github_branch: GitHub HTTP 422", result.error)

    def test_a_crash_in_an_earlier_phase_records_that_phase(self):
        result = self._crash("worktree", exc=OSError("disk full"))
        self.assertEqual((result.failure_phase, result.failure_type), ("worktree", "OSError"))

    def test_the_fields_are_returned_by_to_dict(self):
        result = self._crash("worktree", exc=OSError("disk full"))
        self.assertEqual(result.to_dict()["failure_phase"], "worktree")
        self.assertEqual(result.to_dict()["failure_type"], "OSError")

    def test_a_non_crashed_experiment_leaves_both_fields_empty(self):
        exp = self.service.create_experiment("GitHub Issue #1: fine")
        stored = self.service.get_experiment(exp.id)
        self.assertIsNone(stored.failure_phase)
        self.assertIsNone(stored.failure_type)

    def test_an_old_database_gains_both_columns_without_data_loss(self):
        exp = self.service.create_experiment("GitHub Issue #2: old goal")
        self.service.close()
        with sqlite3.connect(self.db_path) as conn:  # the schema before #410
            conn.execute("ALTER TABLE experiments DROP COLUMN failure_phase")
            conn.execute("ALTER TABLE experiments DROP COLUMN failure_type")
        svc = ExperimentService(
            selection_worker=MagicMock(), plan_worker_factory=lambda p: MagicMock(),
            worker_factory=lambda p: MagicMock(), db_path=self.db_path,
            repo_root=str(self.root), worktrees_root=str(self.root / "wt"),
            experiment_log_dir=str(self.root / "logs"),
        )
        self.addCleanup(svc.close)
        kept = svc.get_experiment(exp.id)
        self.assertEqual((kept.goal, kept.failure_phase), ("GitHub Issue #2: old goal", None))
        with sqlite3.connect(self.db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(experiments)")}
        self.assertTrue({"failure_phase", "failure_type"} <= columns)

if __name__ == "__main__":
    unittest.main()
