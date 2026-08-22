"""Resume an existing experiment without creating or reimplementing it."""

from __future__ import annotations

from pathlib import Path

from modules.experiment_service import ExperimentNotRunnableError, STATUS_VERIFYING

_TERMINAL = {"succeeded", "failed", "timed_out", "needs_review", "rejected"}


def resume_experiment(experiment_service, experiment_id: str):
    """Reuse a persisted experiment/worktree and continue with verification.

    This is deliberately separate from ``run_experiment``: resuming must never
    allocate another experiment id/worktree or invoke the implementation worker.
    """
    experiment = experiment_service.resolve_experiment(experiment_id)
    if experiment.status in _TERMINAL:
        return experiment
    if not experiment.worktree_path or not Path(experiment.worktree_path).is_dir():
        raise ExperimentNotRunnableError(
            f"experiment {experiment.id!r} has no existing worktree to resume"
        )

    elog = experiment_service.get_experiment_logger(experiment.id)
    elog.info(
        experiment.status,
        "resume_requested",
        f"Resuming experiment {experiment.id}; reusing existing worktree and verification state",
    )
    experiment_service._force_status(experiment.id, STATUS_VERIFYING)
    return experiment_service._verify(experiment.id, experiment.worktree_path, elog)
