"""Free resume of an interrupted experiment whose paid implementation finished (#783).

A service restart kills the experiment thread mid-flight. When the paid
implementation had already finished and its worktree still holds that work,
``mark_interrupted_experiments_failed`` parks the run with ``resume_pending=1``
instead of losing it. The nexus tick then calls :func:`resume_interrupted` before
planning: it commits any uncommitted worktree diff and runs the existing
``_verify`` plus the normal adoption path in the same worktree, without calling a
coding provider - so a finished implementation is not paid for twice.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from modules.run_store import insert_activity

from .git_utils import _run_git
from .models import (
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_SUCCEEDED,
    STATUS_VERIFYING,
)
from .text_utils import _utcnow_iso

logger = logging.getLogger("modules.experiment_service.resume")

# The issue comment / activity message of a resumed run.
RESUME_MESSAGE = "resumed after restart, no new paid start"
# The activity event, so status and measurement_health can count it.
RESUME_EVENT = "experiment_resumed"
# Never more than one resume per experiment.
_MAX_RESUME_COUNT = 1


def _iso(now: Any) -> str:
    if isinstance(now, datetime):
        return now.isoformat()
    if now is None:
        return _utcnow_iso()
    return str(now)


def _claim_next_pending(service: Any) -> str | None:
    """Atomically claim the oldest resume-pending interrupted experiment.

    At most one row per call (the caller runs once per tick), and the conditional
    UPDATE makes the claim exactly once per experiment: ``resume_count`` is bumped
    and ``resume_pending`` cleared in the same transaction, so a second caller -
    or a second tick - can never pick the same experiment again.
    """
    with service._lock, service._conn:
        row = service._conn.execute(
            """
            SELECT id FROM experiments
            WHERE status = ? AND COALESCE(resume_pending, 0) = 1
              AND COALESCE(resume_count, 0) < ?
            ORDER BY created_at ASC, id ASC LIMIT 1
            """,
            (STATUS_INTERRUPTED, _MAX_RESUME_COUNT),
        ).fetchone()
        if row is None:
            return None
        experiment_id = str(row["id"])
        claimed = service._conn.execute(
            """
            UPDATE experiments
            SET resume_count = COALESCE(resume_count, 0) + 1,
                resume_pending = 0
            WHERE id = ? AND status = ? AND COALESCE(resume_pending, 0) = 1
              AND COALESCE(resume_count, 0) < ?
            """,
            (experiment_id, STATUS_INTERRUPTED, _MAX_RESUME_COUNT),
        )
        if claimed.rowcount != 1:
            return None
    return experiment_id


def _commit_uncommitted_work(experiment_id: str, worktree: str, elog: Any) -> bool:
    """Commit whatever the interrupted run left uncommitted in its worktree.

    The resumed verification and adoption must see one stable tree; committing
    first also means a crash mid-resume loses nothing. Best effort: a git failure
    is logged and verification still runs on the uncommitted diff.
    """
    try:
        add = _run_git(["add", "-A"], cwd=worktree, timeout=60, elog=elog, phase="verifying")
        if add.returncode != 0:
            elog.warning(
                "verifying", "resume_commit_failed",
                "Could not stage the uncommitted worktree diff for the resume",
                error=(add.stderr or "").strip()[:500],
            )
            return False
        pending = _run_git(
            ["diff", "--cached", "--quiet", "HEAD"], cwd=worktree, timeout=60,
            elog=elog, phase="verifying",
        )
        if pending.returncode == 0:
            return False  # nothing uncommitted
        if pending.returncode != 1:
            elog.warning(
                "verifying", "resume_commit_failed",
                "Could not inspect the staged diff for the resume",
                error=(pending.stderr or "").strip()[:500],
            )
            return False
        commit = _run_git(
            [
                "-c", "user.name=Nexus", "-c", "user.email=nexus@agent-lab.local",
                "commit", "--no-verify", "-m",
                f"Checkpoint resumed work of experiment {experiment_id[:8]} after service restart",
            ],
            cwd=worktree, timeout=60, elog=elog, phase="verifying",
        )
        if commit.returncode != 0:
            elog.warning(
                "verifying", "resume_commit_failed",
                "Could not commit the uncommitted worktree diff for the resume",
                error=(commit.stderr or "").strip()[:500],
            )
            return False
        elog.info(
            "verifying", "resume_committed_uncommitted_work",
            "Committed the uncommitted worktree diff before the resumed verification",
        )
        return True
    except Exception as exc:  # noqa: BLE001 - never block the resume on git
        elog.warning(
            "verifying", "resume_commit_failed",
            "Could not commit the uncommitted worktree diff for the resume",
            exc=exc,
        )
        return False


def resume_interrupted(
    service: Any,
    now: Any,
    *,
    github_work_tracking: Any | None = None,
) -> dict[str, Any] | None:
    """Resume at most one interrupted experiment with a finished implementation (#783).

    Called from the nexus tick before planning. ``service`` is the initiative service
    (or the experiment service itself); the experiment service is taken from
    ``service.experiment_service`` when present. One call resumes at most one
    experiment, and ``resume_count`` caps it at one resume per experiment ever.

    The uncommitted worktree diff is committed, then the existing ``_verify`` runs
    in the same worktree - no coding provider is called. A pass continues into the
    normal adoption path (PR and review) in that worktree; a failure keeps the
    normal needs_review/failed result and only then counts as an attempt.

    Returns a summary dict, or ``None`` when nothing was pending.
    """
    experiment_service = getattr(service, "experiment_service", None) or service
    adoption_service = getattr(service, "adoption_service", None)
    tracking = github_work_tracking or getattr(service, "github_work_tracking", None)

    experiment_id = _claim_next_pending(experiment_service)
    if experiment_id is None:
        return None
    stamp = _iso(now)
    elog = experiment_service.get_experiment_logger(experiment_id)
    elog.info(
        STATUS_INTERRUPTED, RESUME_EVENT,
        f"Resuming interrupted experiment {experiment_id} after restart; no new paid start",
    )
    with experiment_service._lock, experiment_service._conn:
        insert_activity(
            experiment_service._conn,
            timestamp=stamp,
            entity_type="experiment",
            entity_id=experiment_id,
            event=RESUME_EVENT,
            from_status=STATUS_INTERRUPTED,
            to_status=STATUS_VERIFYING,
            message=RESUME_MESSAGE,
        )
    if tracking is not None:
        # The Issue comment; informational only, never raises into the resume.
        try:
            tracking.post_progress(experiment_id, RESUME_EVENT)
        except Exception as exc:  # noqa: BLE001 - reporting is informational
            logger.warning("could not post %s comment for %s: %s", RESUME_EVENT, experiment_id, exc)

    summary: dict[str, Any] = {
        "experiment_id": experiment_id, "resumed": True, "resumed_at": stamp,
    }
    experiment = experiment_service.get_experiment(experiment_id)
    worktree = str(experiment.worktree_path or "").strip()
    if not worktree or not Path(worktree).is_dir():
        # The worktree vanished between startup and this tick: the run stays
        # terminal `interrupted` (today's behaviour), it is just not resumed.
        logger.warning(
            "experiment %s was pending resume but its worktree %s is gone; leaving it interrupted",
            experiment_id, worktree or "(none)",
        )
        elog.warning(
            STATUS_INTERRUPTED, "resume_worktree_missing",
            "The worktree of the pending resume is gone; the run stays interrupted",
            worktree_path=worktree or None,
        )
        summary.update(status=STATUS_INTERRUPTED, reason="worktree missing")
        return summary

    _commit_uncommitted_work(experiment_id, worktree, elog)
    if not experiment_service._transition(
        experiment_id, STATUS_INTERRUPTED, STATUS_VERIFYING,
        ended_at=None, error=None,
    ):
        # The row moved on between the claim and here; nothing to verify.
        summary.update(
            status=experiment_service.get_experiment(experiment_id).status,
            reason="status changed during resume",
        )
        return summary

    try:
        # Same worktree, existing verifier; soft quality findings go to the
        # reviewer at adoption (#519), exactly like the #718 re-verification.
        result = experiment_service._verify(
            experiment_id, worktree, elog, soft_findings_to_reviewer=True,
        )
    except Exception as exc:  # noqa: BLE001 - a crashed verifier is not a pass
        logger.warning("resume verification of %s failed: %s", experiment_id, exc)
        elog.error(
            "verifying", "resume_verification_error",
            "The resumed verification crashed; the run goes to review",
            exc=exc,
        )
        experiment_service._force_status(
            experiment_id, STATUS_NEEDS_REVIEW,
            ended_at=_utcnow_iso(),
            error=f"resume verification error: {type(exc).__name__}: {str(exc)[:1000]}",
        )
        result = experiment_service.get_experiment(experiment_id)

    summary["status"] = result.status
    if result.status == STATUS_SUCCEEDED and adoption_service is not None:
        # The normal PR and review flow, in the same worktree. start_adoption is
        # exactly-once per experiment, so a resumed run cannot cause a duplicate PR.
        try:
            adoption_service.start_adoption(experiment_id)
            summary["adoption"] = "queued"
        except Exception as exc:  # noqa: BLE001 - adoption retries on a later tick
            logger.warning("could not queue adoption of resumed %s: %s", experiment_id, exc)
            summary["adoption"] = f"not queued: {type(exc).__name__}"
    logger.info(
        "experiment %s resumed after restart without a new paid start: verification ended %s",
        experiment_id, result.status,
    )
    return summary
