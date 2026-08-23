from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.experiment_resume import resume_experiment
from modules.experiment_service import STATUS_VERIFYING


class ExperimentResumeTests(unittest.TestCase):
    def test_timed_out_verification_reuses_id_and_worktree_without_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "existing-worktree"
            worktree.mkdir()
            experiment = SimpleNamespace(
                id="bd9720b26ec74f15a2693e5641adf141",
                status="timed_out",
                worktree_path=str(worktree),
            )
            final = SimpleNamespace(id=experiment.id, status="succeeded")
            service = MagicMock()
            service.resolve_experiment.return_value = experiment
            logger = MagicMock()
            service.get_experiment_logger.return_value = logger
            service._verify.return_value = final

            result = resume_experiment(service, experiment.id)

            self.assertIs(result, final)
            service.resolve_experiment.assert_called_once_with(experiment.id)
            service._force_status.assert_called_once_with(experiment.id, STATUS_VERIFYING)
            service._verify.assert_called_once_with(experiment.id, str(worktree), logger)
            service.run_experiment.assert_not_called()
            self.assertFalse(service.create_experiment.called)

    def test_queued_resume_starts_same_experiment(self):
        experiment = SimpleNamespace(
            id="queued-1",
            status="queued",
            worktree_path=None,
        )
        final = SimpleNamespace(id="queued-1", status="succeeded")
        service = MagicMock()
        service.resolve_experiment.return_value = experiment
        service.run_experiment.return_value = final

        result = resume_experiment(service, "queued-1")

        self.assertIs(result, final)
        service.run_experiment.assert_called_once_with("queued-1")
        self.assertFalse(service.create_experiment.called)


if __name__ == "__main__":
    unittest.main()
