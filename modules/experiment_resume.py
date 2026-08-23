"""Resume an existing experiment without allocating a replacement experiment."""

from __future__ import annotations

from pathlib import Path

from modules.experiment_service import (
    ExperimentNotRunnableError,
    STATUS_QUEUED,
    STATUS_VERIFYING,
)

_VERIFICATION_RESUMABLE = {"verifying", "timed_out", "needs_review"}
_UNSAFE_TO_GUESS = {"selecting", "implementing", "failed", "rejected"}


def resume_experiment(experiment_service, experiment_id: str):
    """Continue a persisted experiment from a safe, explicit lifecycle stage.

    - queued: start the normal pipeline using the SAME experiment id;
    - verifying/timed_out/needs_review: reuse the existing worktree and rerun
      verification without invoking implementation;
    - succeeded: already complete, return unchanged;
    - selecting/implementing/failed/rejected: refuse to guess a recovery stage.

    The helper never creates an experiment or a second worktree itself.
    """
    experiment = experiment_service.resolve_experiment(experiment_id)

    if experiment.status == "succeeded":
        return experiment

    if experiment.status == STATUS_QUEUED:
        return experiment_service.run_experiment(experiment.id)

    if experiment.status in _UNSAFE_TO_GUESS:
        raise ExperimentNotRunnableError(
            f"experiment {experiment.id!r} cannot be safely resumed from status "
            f"{experiment.status!r}; inspect state and choose an explicit recovery action"
        )

    if experiment.status not in _VERIFICATION_RESUMABLE:
        raise ExperimentNotRunnableError(
            f"experiment {experiment.id!r} has unsupported resume status {experiment.status!r}"
        )

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
