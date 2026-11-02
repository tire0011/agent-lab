"""Resume an existing experiment without allocating a replacement experiment."""

from __future__ import annotations

import subprocess
from pathlib import Path

from modules.experiment_service import (
    ExperimentNotRunnableError,
    STATUS_QUEUED,
    STATUS_VERIFYING,
)
from modules.experiment_service.git_utils import _run_git

_VERIFICATION_RESUMABLE = {"verifying", "timed_out", "needs_review"}
_UNSAFE_TO_GUESS = {"selecting", "implementing", "failed", "rejected"}


def resume_experiment(
    experiment_service,
    experiment_id: str,
    *,
    github_work_tracking=None,
):
    """Continue a persisted experiment from a safe, explicit lifecycle stage.

    - queued: start the normal pipeline using the SAME experiment id;
    - verifying/timed_out/needs_review: reuse the existing worktree and rerun
      verification without invoking implementation; with GitHub work
      tracking, today's main is merged in first (#486);
    - succeeded: already complete, return unchanged;
    - selecting/implementing/failed/rejected: refuse to guess a recovery stage.

    The helper never creates an experiment or a second worktree itself.
    """
    experiment = experiment_service.resolve_experiment(experiment_id)

    if experiment.status == "succeeded":
        return experiment

    if experiment.status == STATUS_QUEUED:
        if github_work_tracking is None:
            return experiment_service.run_experiment(experiment.id)
        return experiment_service.run_experiment(
            experiment.id, github_work_tracking=github_work_tracking
        )

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
    if github_work_tracking is not None:
        _merge_current_main_for_resume(
            experiment_service, experiment, github_work_tracking, elog
        )
    experiment_service._force_status(experiment.id, STATUS_VERIFYING)
    if github_work_tracking is None:
        return experiment_service._verify(
            experiment.id, experiment.worktree_path, elog
        )
    return experiment_service._verify(
        experiment.id,
        experiment.worktree_path,
        elog,
        github_work_tracking=github_work_tracking,
    )


def _merge_current_main_for_resume(
    experiment_service, experiment, github_work_tracking, elog
) -> None:
    """Bring today's main into a resumed worktree before it is verified (#486).

    Without this, a resume re-verifies the tree it was built on, and a green
    result says nothing about the main it will be adopted into. Unlike
    ``_merge_current_main`` no coding agent runs afterwards, so a conflict is
    never left as markers: the merge is aborted, the worktree is put back as
    it was, and the resume is refused so a new experiment resolves it.

    The worktree's own changes are published as a checkpoint first and HEAD
    moved onto it, so the merge cannot touch or lose them, and the merged
    HEAD descends from the Issue branch for the next checkpoint to push.
    """
    worktree_path = experiment.worktree_path

    def git(*args: str) -> subprocess.CompletedProcess:
        return _run_git(
            ["-c", "user.name=Nexus", "-c", "user.email=nexus@agent-lab.local", *args],
            cwd=worktree_path, timeout=120, elog=elog, phase="verifying",
        )

    def refuse(message: str) -> None:
        experiment_service._force_status(experiment.id, experiment.status, error=message)
        raise ExperimentNotRunnableError(message)

    original_head = git("rev-parse", "HEAD").stdout.strip()
    if git("fetch", "--no-tags", "origin", "main").returncode != 0 or not original_head:
        refuse(
            f"experiment {experiment.id!r} cannot be resumed: fetching current main "
            "failed, so verification would not cover today's main"
        )
    if git("merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD").returncode == 0:
        return
    main_sha = git("rev-parse", "FETCH_HEAD").stdout.strip()

    if git("status", "--porcelain").stdout.strip():
        try:
            ref = github_work_tracking.checkpoint_experiment(experiment.id, worktree_path)
        except Exception as exc:  # noqa: BLE001 - never merge over unsaved work
            refuse(
                f"experiment {experiment.id!r} cannot be resumed: its uncommitted "
                f"changes could not be checkpointed before merging main: {str(exc)[:1000]}"
            )
        if git("reset", "-q", ref.checkpoint_sha).returncode != 0:
            refuse(
                f"experiment {experiment.id!r} cannot be resumed: could not move the "
                f"worktree onto its checkpoint {ref.checkpoint_sha}"
            )

    if git("merge", "--no-edit", main_sha).returncode == 0:
        elog.info(
            "verifying", "resume_base_merged",
            "Merged current main into the resumed worktree before verification",
            main_sha=main_sha,
        )
        return

    conflicted = git("diff", "--name-only", "--diff-filter=U").stdout.split()
    git("merge", "--abort")
    git("reset", "-q", original_head)
    if not conflicted:
        refuse(
            f"experiment {experiment.id!r} cannot be resumed: merging current main "
            "could not run; the worktree is unchanged"
        )
    elog.warning(
        "verifying", "resume_base_conflict",
        f"Merging current main conflicts in: {', '.join(conflicted[:20])}",
        paths=conflicted[:20],
        main_sha=main_sha,
    )
    refuse(
        f"experiment {experiment.id!r} cannot be resumed: merging current main "
        f"conflicts in {', '.join(conflicted[:20])}; start a new experiment so the "
        "coding agent can resolve it"
    )
