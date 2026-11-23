"""Free resume of an interrupted experiment whose paid implementation finished (#783).

A service restart kills the experiment thread mid-flight. When the paid
implementation had already finished and its worktree still holds that work,
``mark_interrupted_experiments_failed`` parks the run with ``resume_pending=1``
instead of losing it. The nexus tick then calls :func:`resume_interrupted` -
behind the owner pause, the issue-approval sweep, the busy and the stale-service
guards, before planning. It runs the existing ``_verify`` in the same worktree
without calling a coding provider, so a finished implementation is not paid for
twice.

How the resume keeps the normal verification and adoption contract:

* The uncommitted publishable diff is committed - through a temporary index,
  with the publication exclusions of the GitHub checkpoint (no ``.env``, keys,
  ``state/``, ``logs/`` ...) - to ``refs/nexus/resume/<experiment id>``. HEAD and
  the index stay untouched, so ``_verify`` still sees the whole change.
* Work already committed ahead of the recorded base is exposed again as an
  uncommitted ``base..HEAD`` change (``git reset --mixed <base>``, the #718
  pattern), so the diff, the whitespace check and the quality gate judge the
  full experiment delta, not an empty HEAD diff.
* A GitHub-linked experiment is verified *with* its GitHub tracking: checkpoint
  publication, exact-commit CI lookup and the PR diagnostics run as in a normal
  run, so adoption later finds a matching verified checkpoint.
* No repair loop and no adoption start here: a pass becomes a normal
  ``succeeded`` run that the integration scheduler adopts (PR and review); a
  failure keeps the normal ``needs_review``/``failed`` result, which then
  counts as an attempt.

Crash safety: the claim, the ``interrupted -> verifying`` move and the
``experiment_resumed`` activity row are one transaction. ``resume_owner`` names
the process that runs the resume, so the API's startup reconciliation leaves a
live tick-owned resume alone and only reconciles orphaned ones. A resume whose
process died before verification started is parked again (bounded); one that
died during verification ends ``interrupted`` and is spent.
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from modules.github_work_tracking import GitHubWorkTrackingService
from modules.run_store import insert_activity

from .git_utils import _run_git
from .models import (
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_SUCCEEDED,
    STATUS_VERIFYING,
)
from .service_parts.core import _INTERRUPTED_BY_RESTART_ERROR
from .text_utils import _utcnow_iso

logger = logging.getLogger("modules.experiment_service.resume")

# The issue comment / activity message of a resumed run.
RESUME_MESSAGE = "resumed after restart, no new paid start"
# The activity event of a resume start, so status and measurement_health can count it.
RESUME_EVENT = "experiment_resumed"
# The outcome of a resume: to_status is the verification result.
RESUME_FINISHED_EVENT = "experiment_resume_finished"
# A pending resume that was not started (worktree gone, approval revoked ...).
RESUME_SKIPPED_EVENT = "experiment_resume_skipped"
# A resume whose process died before verification started, parked again.
RESUME_RECOVERED_EVENT = "experiment_resume_recovered"
# Where the committed resume snapshot is kept; never pushed.
RESUME_REF_PREFIX = "refs/nexus/resume/"
# Never more than one resumed verification per experiment.
_MAX_RESUME_COUNT = 1
# A resume that keeps dying before verification is not claimed forever.
_MAX_RESUME_CLAIMS = 3
_GIT_TIMEOUT_SECONDS = 60
_COMMIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Nexus",
    "GIT_AUTHOR_EMAIL": "nexus@agent-lab.local",
    "GIT_COMMITTER_NAME": "Nexus",
    "GIT_COMMITTER_EMAIL": "nexus@agent-lab.local",
}
_STATS_WINDOW_DAYS = 14


class ResumeStepError(RuntimeError):
    """A step before verification failed; the resume stops with a recorded failure."""


def _iso(now: Any) -> str:
    if isinstance(now, datetime):
        return now.isoformat()
    if now is None:
        return _utcnow_iso()
    return str(now)


# ---- resume ownership (#783 review: startup must not reconcile a live resume) ----

def _process_start_ticks(pid: int) -> str:
    """The kernel start time of *pid*, so a reused pid is not taken for the owner."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    fields = data.rsplit(")", 1)[-1].split()
    # Field 22 (starttime); the fields after ``comm`` start at field 3.
    return fields[19] if len(fields) > 19 else ""


def owner_token(pid: int | None = None) -> str:
    """Identify the process that runs a claimed resume: ``host:pid:start``."""
    pid = os.getpid() if pid is None else int(pid)
    return f"{socket.gethostname()}:{pid}:{_process_start_ticks(pid)}"


def owner_alive(token: str | None) -> bool:
    """Whether the process named by *token* still runs.

    Another host cannot be checked from here; it counts as alive (the stall
    sweep stays the backstop). A malformed token counts as dead.
    """
    if not token:
        return False
    try:
        host, pid_text, start = str(token).rsplit(":", 2)
        pid = int(pid_text)
    except ValueError:
        return False
    if host != socket.gethostname():
        return True
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    current = _process_start_ticks(pid)
    return not (start and current and current != start)


# ---- what counts as finished work -----------------------------------------------

def worktree_has_work(worktree: str, base: str | None) -> bool:
    """Whether *worktree* holds work: a commit ahead of *base* or a non-empty diff.

    Any git failure counts as "no work": a resumable verdict is never guessed.
    """
    if not worktree or not Path(worktree).is_dir():
        return False
    try:
        base = str(base or "").strip()
        if base:
            ahead = _run_git(
                ["rev-list", "--count", f"{base}..HEAD"], cwd=worktree, timeout=30,
            )
            if ahead.returncode == 0:
                try:
                    if int(ahead.stdout.strip() or "0") >= 1:
                        return True
                except ValueError:
                    pass
        status = _run_git(
            ["status", "--porcelain", "--untracked-files=all"], cwd=worktree, timeout=30,
        )
        return status.returncode == 0 and bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not inspect worktree %s for a resume: %s", worktree, exc)
        return False


def _claims(service: Any, experiment_id: str) -> int:
    with service._lock:
        row = service._conn.execute(
            "SELECT COUNT(*) FROM activity_log WHERE entity_type = 'experiment' "
            "AND entity_id = ? AND event = ?",
            (experiment_id, RESUME_EVENT),
        ).fetchone()
    return int(row[0] or 0) if row else 0


# ---- orphaned resumes -----------------------------------------------------------

def recover_orphaned_resumes(service: Any, now: Any = None) -> list[str]:
    """Reconcile claimed resumes whose owning process is gone.

    Before verification started (``resume_count`` still 0) nothing was judged,
    so the run is parked again for a later tick - at most ``_MAX_RESUME_CLAIMS``
    claims in total. After verification started the resume is spent: the run
    ends ``interrupted`` as before #783 and counts as an attempt. A resume whose
    owner still runs is never touched. Returns the reconciled ids.
    """
    stamp = _iso(now)
    with service._lock:
        rows = service._conn.execute(
            "SELECT id, resume_owner, COALESCE(resume_count, 0) AS resume_count "
            "FROM experiments WHERE status = ? AND resume_owner IS NOT NULL",
            (STATUS_VERIFYING,),
        ).fetchall()
    reconciled: list[str] = []
    for row in rows:
        token = str(row["resume_owner"])
        if owner_alive(token):
            continue
        experiment_id = str(row["id"])
        before_verification = int(row["resume_count"] or 0) < _MAX_RESUME_COUNT
        repark = before_verification and _claims(service, experiment_id) < _MAX_RESUME_CLAIMS
        if repark:
            error = _INTERRUPTED_BY_RESTART_ERROR
            event, message = (
                RESUME_RECOVERED_EVENT,
                "the resume process exited before verification started; parked again",
            )
        else:
            error = (
                f"{_INTERRUPTED_BY_RESTART_ERROR}; the resume was interrupted "
                + ("during verification" if not before_verification else "before verification too often")
            )
            event, message = RESUME_FINISHED_EVENT, f"{RESUME_MESSAGE}; {error}"
        with service._lock, service._conn:
            moved = service._conn.execute(
                "UPDATE experiments SET status = ?, resume_pending = ?, resume_owner = NULL, "
                "ended_at = ?, error = ?, worker_pgid = NULL "
                "WHERE id = ? AND status = ? AND resume_owner = ?",
                (STATUS_INTERRUPTED, 1 if repark else 0, stamp, error,
                 experiment_id, STATUS_VERIFYING, token),
            ).rowcount == 1
            if moved:
                insert_activity(
                    service._conn, timestamp=stamp, entity_type="experiment",
                    entity_id=experiment_id, event=event,
                    from_status=STATUS_VERIFYING, to_status=STATUS_INTERRUPTED,
                    message=message,
                )
        if moved:
            logger.warning("experiment %s: orphaned resume reconciled (%s)", experiment_id, message)
            reconciled.append(experiment_id)
    return reconciled


# ---- picking, skipping and claiming ---------------------------------------------

def _pending_ids(service: Any) -> list[str]:
    with service._lock:
        rows = service._conn.execute(
            """
            SELECT id FROM experiments
            WHERE status = ? AND COALESCE(resume_pending, 0) = 1
              AND COALESCE(resume_count, 0) < ?
            ORDER BY created_at ASC, id ASC
            """,
            (STATUS_INTERRUPTED, _MAX_RESUME_COUNT),
        ).fetchall()
    return [str(row["id"]) for row in rows]


def _skip_reason(
    service: Any, experiment_id: str, approval_block: Callable[[str], str | None] | None,
) -> str | None:
    """Why a pending resume must not start; checked before touching the worktree."""
    if approval_block is not None:
        blocked = approval_block(experiment_id)
        if blocked:
            return str(blocked)
    experiment = service.get_experiment(experiment_id)
    worktree = str(experiment.worktree_path or "").strip()
    if not worktree or not Path(worktree).is_dir():
        return "worktree missing"
    if not worktree_has_work(worktree, experiment.base_commit_sha):
        return "worktree no longer holds the finished work"
    if _claims(service, experiment_id) >= _MAX_RESUME_CLAIMS:
        return "resume stopped before verification too often"
    return None


def _skip(service: Any, experiment_id: str, reason: str, stamp: str) -> bool:
    """Truthful fallback: the run stays ``interrupted`` (today's behaviour), not resumed."""
    with service._lock, service._conn:
        moved = service._conn.execute(
            "UPDATE experiments SET resume_pending = 0 "
            "WHERE id = ? AND status = ? AND COALESCE(resume_pending, 0) = 1",
            (experiment_id, STATUS_INTERRUPTED),
        ).rowcount == 1
        if moved:
            insert_activity(
                service._conn, timestamp=stamp, entity_type="experiment",
                entity_id=experiment_id, event=RESUME_SKIPPED_EVENT,
                from_status=STATUS_INTERRUPTED, to_status=STATUS_INTERRUPTED,
                message=f"not resumed: {reason}"[:1000],
            )
    if moved:
        logger.warning("experiment %s: pending resume not started: %s", experiment_id, reason)
        service.get_experiment_logger(experiment_id).warning(
            STATUS_INTERRUPTED, RESUME_SKIPPED_EVENT,
            f"Pending resume not started, the run stays interrupted: {reason}",
        )
    return moved


def _claim(service: Any, experiment_id: str, token: str, stamp: str) -> bool:
    """Claim, move to ``verifying`` and record ``experiment_resumed`` atomically.

    One transaction: either all three happen or none, so a failed activity
    write leaves the run pending for the next tick instead of half-claimed.
    """
    with service._lock, service._conn:
        moved = service._conn.execute(
            """
            UPDATE experiments
            SET status = ?, resume_pending = 0, resume_owner = ?, ended_at = NULL,
                error = NULL
            WHERE id = ? AND status = ? AND COALESCE(resume_pending, 0) = 1
              AND COALESCE(resume_count, 0) < ?
            """,
            (STATUS_VERIFYING, token, experiment_id, STATUS_INTERRUPTED, _MAX_RESUME_COUNT),
        ).rowcount == 1
        if not moved:
            return False
        insert_activity(
            service._conn, timestamp=stamp, entity_type="experiment",
            entity_id=experiment_id, event=RESUME_EVENT,
            from_status=STATUS_INTERRUPTED, to_status=STATUS_VERIFYING,
            message=RESUME_MESSAGE,
        )
    return True


def _start_verification(service: Any, experiment_id: str, token: str) -> bool:
    """Spend the resume right before ``_verify`` runs; False when the run moved on."""
    with service._lock, service._conn:
        return service._conn.execute(
            "UPDATE experiments SET resume_count = COALESCE(resume_count, 0) + 1 "
            "WHERE id = ? AND status = ? AND resume_owner = ?",
            (experiment_id, STATUS_VERIFYING, token),
        ).rowcount == 1


def _finish(service: Any, experiment_id: str, token: str, detail: str) -> str:
    """Release the claim and record the resume's outcome in one transaction."""
    with service._lock, service._conn:
        row = service._conn.execute(
            "SELECT status FROM experiments WHERE id = ?", (experiment_id,),
        ).fetchone()
        status = str(row["status"]) if row is not None else "unknown"
        service._conn.execute(
            "UPDATE experiments SET resume_owner = NULL WHERE id = ? AND resume_owner = ?",
            (experiment_id, token),
        )
        insert_activity(
            service._conn, timestamp=_utcnow_iso(), entity_type="experiment",
            entity_id=experiment_id, event=RESUME_FINISHED_EVENT,
            from_status=STATUS_VERIFYING, to_status=status,
            message=f"{RESUME_MESSAGE}; {detail}"[:1000],
        )
    return status


# ---- worktree preparation -------------------------------------------------------

def _git_or_raise(args: list[str], worktree: str, elog: Any, what: str, **kwargs: Any) -> str:
    try:
        proc = _run_git(
            args, cwd=worktree, timeout=_GIT_TIMEOUT_SECONDS, elog=elog, phase="verifying", **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ResumeStepError(f"{what}: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise ResumeStepError(f"{what}: {detail or f'git exited {proc.returncode}'}")
    return proc.stdout


def commit_publishable_work(experiment_id: str, worktree: str, elog: Any) -> str | None:
    """Commit the uncommitted publishable diff to ``refs/nexus/resume/<id>``.

    Only paths the GitHub checkpoint would publish are staged (no secrets,
    runtime state or caches); excluded paths stay where they are and are
    logged. Uses a temporary index and ``commit-tree``, so HEAD and the index
    of the worktree are untouched and ``_verify`` still sees the whole change.
    Returns the commit, ``None`` when nothing publishable was uncommitted.
    Raises :class:`ResumeStepError` when staging or committing fails.
    """
    raw = _git_or_raise(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"], worktree, elog,
        "could not list the uncommitted worktree changes",
    )
    try:
        paths = GitHubWorkTrackingService._status_paths(raw)
    except Exception as exc:  # noqa: BLE001 - an unreadable status is a failed step
        raise ResumeStepError(f"could not parse the worktree status: {exc}") from exc
    publishable = [path for path in paths if GitHubWorkTrackingService._publishable_path(path)]
    excluded = sorted(set(paths) - set(publishable))
    if excluded:
        elog.warning(
            "verifying", "resume_paths_excluded",
            "Runtime or secret paths are left out of the resume commit",
            excluded=excluded[:20],
        )
    if not publishable:
        return None
    head = _git_or_raise(["rev-parse", "HEAD"], worktree, elog, "could not resolve HEAD").strip()
    with tempfile.TemporaryDirectory(prefix="nexus-resume-index-") as index_dir:
        env = dict(os.environ)
        env.update(_COMMIT_IDENTITY)
        env["GIT_INDEX_FILE"] = str(Path(index_dir) / "index")
        _git_or_raise(["read-tree", "HEAD"], worktree, elog, "could not stage the resume commit", env=env)
        _git_or_raise(
            ["add", "-A", "--", *publishable], worktree, elog,
            "could not stage the resume commit", env=env,
        )
        tree = _git_or_raise(
            ["write-tree"], worktree, elog, "could not write the resume tree", env=env,
        ).strip()
        commit = _git_or_raise(
            [
                "commit-tree", tree, "-p", head, "-m",
                f"Resume snapshot of experiment {experiment_id[:8]} after a service restart",
            ],
            worktree, elog, "could not commit the resume snapshot", env=env,
        ).strip()
    _git_or_raise(
        ["update-ref", f"{RESUME_REF_PREFIX}{experiment_id}", commit], worktree, elog,
        "could not record the resume commit",
    )
    elog.info(
        "verifying", "resume_committed_uncommitted_work",
        "Committed the uncommitted publishable diff before the resumed verification",
        commit_sha=commit, paths=publishable[:30],
    )
    return commit


def expose_full_delta(worktree: str, base: str | None, elog: Any) -> bool:
    """Make the whole ``base..worktree`` change visible to ``_verify``.

    ``_verify`` (and adoption after it) read the change relative to HEAD; work
    committed ahead of the recorded base would look empty. ``git reset --mixed``
    moves HEAD back to the base and keeps every file, as the #718
    re-verification does. Returns whether HEAD moved.
    """
    base = str(base or "").strip()
    if not base:
        return False
    head = _git_or_raise(["rev-parse", "HEAD"], worktree, elog, "could not resolve HEAD").strip()
    if head == base:
        return False
    try:
        ancestor = _run_git(
            ["merge-base", "--is-ancestor", base, head], cwd=worktree,
            timeout=_GIT_TIMEOUT_SECONDS, elog=elog, phase="verifying",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ResumeStepError(f"could not compare HEAD with the recorded base: {exc}") from exc
    if ancestor.returncode != 0:
        raise ResumeStepError(
            f"the recorded base {base[:12]} is not an ancestor of the worktree HEAD {head[:12]}"
        )
    _git_or_raise(
        ["reset", "--mixed", "--quiet", base], worktree, elog,
        "could not expose the committed work as the experiment change",
    )
    elog.info(
        "verifying", "resume_full_delta_exposed",
        "Committed work is verified as the full change since the recorded base",
        base_commit=base, previous_head=head,
    )
    return True


def _tracking_for(tracking: Any, experiment_id: str) -> Any | None:
    """The GitHub tracking, when this experiment is linked to an Issue."""
    if tracking is None:
        return None
    lookup = getattr(tracking, "for_experiment", None)
    if lookup is None:
        return None
    try:
        return tracking if lookup(experiment_id) is not None else None
    except Exception as exc:  # noqa: BLE001 - an unreadable link is a failed step
        raise ResumeStepError(f"could not read the GitHub link: {exc}") from exc


def _report(tracking: Any, experiment_id: str, state: str, detail: str | None = None) -> None:
    post = getattr(tracking, "post_progress", None)
    if post is None:
        return
    try:
        post(experiment_id, state, detail=detail)
    except Exception as exc:  # noqa: BLE001 - reporting is informational
        logger.warning("could not post %s for %s: %s", state, experiment_id, exc)


# ---- the resume -----------------------------------------------------------------

def resume_interrupted(
    service: Any,
    now: Any,
    *,
    github_work_tracking: Any | None = None,
    approval_block: Callable[[str], str | None] | None = None,
) -> dict[str, Any] | None:
    """Resume at most one interrupted experiment with a finished implementation (#783).

    ``service`` is the experiment service (or an object carrying it as
    ``experiment_service``). ``approval_block(experiment_id)`` returns why the
    owner revoked or rejected the issue, checked before the worktree is touched.
    Returns a summary dict, or ``None`` when nothing was resumed or skipped.
    """
    experiment_service = getattr(service, "experiment_service", None) or service
    tracking = (
        github_work_tracking if github_work_tracking is not None
        else getattr(service, "github_work_tracking", None)
    )
    stamp = _iso(now)
    recover_orphaned_resumes(experiment_service, stamp)

    skipped: list[dict[str, str]] = []
    chosen: str | None = None
    for experiment_id in _pending_ids(experiment_service):
        reason = _skip_reason(experiment_service, experiment_id, approval_block)
        if reason is None:
            chosen = experiment_id
            break
        if _skip(experiment_service, experiment_id, reason, stamp):
            skipped.append({"experiment_id": experiment_id, "reason": reason})
    if chosen is None:
        return {"resumed": False, "skipped": skipped} if skipped else None

    token = owner_token()
    if not _claim(experiment_service, chosen, token, stamp):
        return {"resumed": False, "skipped": skipped} if skipped else None
    summary: dict[str, Any] = {
        "experiment_id": chosen, "resumed": True, "resumed_at": stamp, "skipped": skipped,
    }
    elog = experiment_service.get_experiment_logger(chosen)
    elog.info(
        STATUS_VERIFYING, RESUME_EVENT,
        f"Resuming interrupted experiment {chosen} after restart; no new paid start",
    )
    _report(tracking, chosen, RESUME_EVENT)

    verification_started = False
    detail = "verification did not run"
    try:
        experiment = experiment_service.get_experiment(chosen)
        worktree = str(experiment.worktree_path or "").strip()
        if not worktree or not Path(worktree).is_dir():
            raise ResumeStepError("worktree missing")
        verify_tracking = _tracking_for(tracking, chosen)
        summary["resume_commit_sha"] = commit_publishable_work(chosen, worktree, elog)
        summary["head_reset_to_base"] = expose_full_delta(worktree, experiment.base_commit_sha, elog)
        if not _start_verification(experiment_service, chosen, token):
            detail = "the run moved on before verification started"
        else:
            verification_started = True
            try:
                result = experiment_service._verify(
                    chosen, worktree, elog, github_work_tracking=verify_tracking,
                )
            except Exception as exc:  # noqa: BLE001 - a crashed verifier is not a pass
                logger.warning("resume verification of %s failed: %s", chosen, exc)
                elog.error(
                    "verifying", "resume_verification_error",
                    "The resumed verification crashed; the run goes to review", exc=exc,
                )
                experiment_service._force_status(
                    chosen, STATUS_NEEDS_REVIEW, ended_at=_utcnow_iso(),
                    error=f"resume verification error: {type(exc).__name__}: {str(exc)[:1000]}",
                )
                result = experiment_service.get_experiment(chosen)
            detail = f"verification ended {result.status}"
            if result.error:
                detail += f": {str(result.error)[:300]}"
            # Same Issue lifecycle comments as a normal run; no repair loop, which
            # would call a coding provider.
            from .service_parts.implementation import _final_issue_progress

            for state, progress in _final_issue_progress(result):
                _report(tracking, chosen, state, progress)
    except Exception as exc:  # noqa: BLE001 - every claimed resume gets an outcome
        if verification_started:
            # Only reporting runs after _verify; its result already stands.
            logger.warning("resume of %s: error after verification: %s", chosen, exc)
            return summary
        reason = str(exc) if isinstance(exc, ResumeStepError) else f"{type(exc).__name__}: {exc}"
        logger.warning("resume of %s stopped before verification: %s", chosen, reason)
        elog.error(
            "verifying", "resume_stopped",
            f"The resume stopped before verification: {reason}",
        )
        # Recorded failure with today's behaviour: interrupted, the attempt counts.
        experiment_service._transition(
            chosen, STATUS_VERIFYING, STATUS_INTERRUPTED,
            ended_at=_utcnow_iso(),
            error=f"{_INTERRUPTED_BY_RESTART_ERROR}; resume stopped before verification: {reason}"[:2000],
        )
        detail = f"stopped before verification: {reason}"
    finally:
        summary["status"] = _finish(experiment_service, chosen, token, detail)
    logger.info(
        "experiment %s resumed after restart without a new paid start: %s",
        chosen, summary["status"],
    )
    return summary


# ---- measurement ----------------------------------------------------------------

def resume_stats(conn: sqlite3.Connection, now: datetime, days: int = _STATS_WINDOW_DAYS) -> dict[str, Any]:
    """Resume starts and their outcomes over the last *days* days (#783 measure).

    ``saved_paid_starts`` is the number of resumed runs whose verification
    passed - each one is a finished implementation that needed no new paid
    start. Read-only; an old store without ``activity_log`` yields zeros.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    result: dict[str, Any] = {
        "days": days, "resumed": 0, "resume_events": 0, "outcomes": {},
        "saved_paid_starts": 0, "in_flight": 0, "skipped": 0,
    }
    try:
        started = conn.execute(
            "SELECT entity_id, COUNT(*) FROM activity_log WHERE entity_type = 'experiment' "
            "AND event = ? AND timestamp >= ? GROUP BY entity_id",
            (RESUME_EVENT, since),
        ).fetchall()
        finished = conn.execute(
            "SELECT entity_id, to_status FROM activity_log WHERE entity_type = 'experiment' "
            "AND event = ? AND timestamp >= ? ORDER BY timestamp ASC, id ASC",
            (RESUME_FINISHED_EVENT, since),
        ).fetchall()
        skipped = conn.execute(
            "SELECT COUNT(DISTINCT entity_id) FROM activity_log WHERE entity_type = 'experiment' "
            "AND event = ? AND timestamp >= ?",
            (RESUME_SKIPPED_EVENT, since),
        ).fetchone()
    except sqlite3.Error:
        return result
    resumed = {str(row[0]) for row in started}
    last_outcome: dict[str, str] = {}
    for entity_id, to_status in finished:
        if str(entity_id) in resumed:
            last_outcome[str(entity_id)] = str(to_status or "unknown")
    outcomes: dict[str, int] = {}
    for status in last_outcome.values():
        outcomes[status] = outcomes.get(status, 0) + 1
    result.update(
        resumed=len(resumed),
        resume_events=sum(int(row[1]) for row in started),
        outcomes=dict(sorted(outcomes.items())),
        saved_paid_starts=outcomes.get(STATUS_SUCCEEDED, 0),
        in_flight=len(resumed) - len(last_outcome),
        skipped=int(skipped[0] or 0) if skipped else 0,
    )
    return result


def resume_stats_summary(stats: dict[str, Any]) -> str:
    """One line for the status issue and measurement_health."""
    outcomes = ", ".join(f"{status} {count}" for status, count in stats.get("outcomes", {}).items())
    line = (
        f"{stats.get('resumed', 0)} interrupted run(s) resumed in the last {stats.get('days')} days, "
        f"{stats.get('saved_paid_starts', 0)} passed verification (saved paid starts)"
    )
    if outcomes:
        line += f"; outcomes: {outcomes}"
    if stats.get("in_flight"):
        line += f"; {stats['in_flight']} still running"
    if stats.get("skipped"):
        line += f"; {stats['skipped']} not resumed"
    return line
