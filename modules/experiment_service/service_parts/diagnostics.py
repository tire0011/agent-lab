"""Logging and diagnostics methods of :class:`ExperimentService`."""

from __future__ import annotations

from pathlib import Path

from modules.central_logging import ExperimentLogger


class DiagnosticsMixin:
    """Logging and diagnostics methods of :class:`ExperimentService`."""

    def get_experiment_logger(
        self, experiment_id: str, mailbox_task_id: str | None = None
    ) -> ExperimentLogger:
        """Build the (cheap, stateless) structured JSONL logger for one experiment.

        Every pipeline caller goes through this single factory, so all log entries for an
        experiment land in exactly one file regardless of which thread writes.
        """
        if mailbox_task_id is None:
            mailbox_task_id = self.mailbox_task_id_for_experiment(experiment_id)
        return ExperimentLogger(
            experiment_id,
            mailbox_task_id=mailbox_task_id,
            log_dir=self.experiment_log_dir,
        )


    def experiment_log_path(self, experiment_id: str) -> Path:
        """Path to an experiment's JSONL log file (may not exist yet)."""
        return Path(self.experiment_log_dir) / f"{experiment_id}.jsonl"

