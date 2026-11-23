"""Free resume of an interrupted experiment whose paid implementation finished (#783).

A service restart kills the experiment thread mid-flight. When the paid
implementation had already finished and its worktree still holds that work,
``mark_interrupted_experiments_failed`` parks the run with ``resume_pending=1``
instead of losing it. The nexus tick then calls :func:`resume_interrupted` -
behind the owner pause, the issue-approval sweep, the busy and the stale-service
guards, before planning. It runs the existing ``_verify`` in the same worktree
without calling a coding provider, so a finished implementation is not paid for
twice.

The resume prepares an immutable snapshot while preserving the original
branch, index, and files. Verification and adoption use the same snapshot.
The process claim and verification start are separate durable transitions.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from modules.redaction import sanitized_reason
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
RESUME_CLAIMED_EVENT = "experiment_resume_claimed"
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
_ACTIVE_RESUMES: set[str] = set()


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
    return fields[19] if len(fields) > 19 and fields[0] != "Z" else ""


def owner_token(pid: int | None = None) -> str:
    """Identify the process that runs a claimed resume: ``host:pid:start``."""
    pid = os.getpid() if pid is None else int(pid)
    return f"{socket.gethostname()}:{pid}:{_process_start_ticks(pid)}"


def owner_alive(token: str | None) -> bool:
    """Whether the process named by *token* still runs.

    This store is shared by the API and tick on the same host. An owner
    on another host is treated conservatively as alive.
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
    start, _, invocation = start.partition("/")
    if pid == os.getpid() and invocation:
        return token in _ACTIVE_RESUMES
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    current = _process_start_ticks(pid)
    return not start or current == start


# ---- what counts as finished work -----------------------------------------------

def worktree_has_work(worktree: str, base: str | None) -> bool:
    """Whether *worktree* holds work: a commit ahead of *base* or a non-empty diff.

    Any git failure counts as "no work": a resumable verdict is never guessed.
    """
    if not worktree or not Path(worktree).is_dir():
        return False
    try:
        root = _run_git(["rev-parse", "--show-toplevel"], cwd=worktree, timeout=30)
        if root.returncode or Path(root.stdout.strip()).resolve() != Path(worktree).resolve():
            return False
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
            ["--no-optional-locks", "status", "--porcelain", "--untracked-files=all"], cwd=worktree, timeout=30,
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
            (experiment_id, RESUME_CLAIMED_EVENT),
        ).fetchone()
    return int(row[0] or 0) if row else 0


# ---- orphaned resumes -----------------------------------------------------------

def _scratch_path(token: str) -> Path | None:
    _, separator, invocation = token.partition("/")
    if separator and len(invocation) == 32 and all(c in "0123456789abcdef" for c in invocation):
        return Path(tempfile.gettempdir()) / f"nexus-resume-{invocation}"
    return None


def _remove_scratch(service: Any, token: str) -> None:
    scratch = _scratch_path(token)
    if scratch is None or not scratch.is_dir() or scratch.is_symlink():
        return
    checkout = scratch / "worktree"
    try:
        if checkout.exists():
            proc = _run_git(
                ["worktree", "remove", "--force", str(checkout)],
                cwd=service.repo_root, timeout=_GIT_TIMEOUT_SECONDS,
            )
            if proc.returncode and (checkout / ".git").exists():
                logger.warning("could not unregister resume checkout %s", checkout)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not remove resume checkout: %s", exc)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


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
            "SELECT id, status, resume_owner, resume_count, worker_pgid FROM experiments "
            "WHERE resume_owner IS NOT NULL",
        ).fetchall()
    reconciled: list[str] = []
    for row in rows:
        token = str(row["resume_owner"])
        if owner_alive(token):
            continue
        experiment_id = str(row["id"])
        if row["status"] != STATUS_VERIFYING:
            _finish(service, experiment_id, token, "reconciled after resume owner exited")
            _remove_scratch(service, token)
            reconciled.append(experiment_id)
            continue
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
            event = RESUME_SKIPPED_EVENT if before_verification else RESUME_FINISHED_EVENT
            message = error
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
            if row["worker_pgid"]:
                service._stop_stalled_worker(experiment_id, row["worker_pgid"])
            _remove_scratch(service, token)
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
    """Reserve the experiment without announcing or spending verification."""
    with service._lock, service._conn:
        moved = service._conn.execute(
            """
            UPDATE experiments
            SET status = ?, resume_pending = 0, resume_owner = ?, ended_at = NULL,
                error = NULL
            WHERE id = ? AND status = ? AND COALESCE(resume_pending, 0) = 1
              AND COALESCE(resume_count, 0) < ?
              AND NOT EXISTS (SELECT 1 FROM experiments WHERE resume_owner IS NOT NULL)
            """,
            (STATUS_VERIFYING, token, experiment_id, STATUS_INTERRUPTED, _MAX_RESUME_COUNT),
        ).rowcount == 1
        if not moved:
            return False
        insert_activity(
            service._conn, timestamp=stamp, entity_type="experiment",
            entity_id=experiment_id, event=RESUME_CLAIMED_EVENT,
            from_status=STATUS_INTERRUPTED, to_status=STATUS_VERIFYING,
        )
    return True


def _start_verification(
    service: Any, experiment_id: str, token: str, stamp: str | None = None, *,
    snapshot: str | None = None, baseline: str | None = None,
) -> bool:
    """Spend the resume and record its start atomically."""
    with service._lock, service._conn:
        moved = service._conn.execute(
            "UPDATE experiments SET resume_count = 1, resume_snapshot_sha = ?, implementation_base_sha = ? "
            "WHERE id = ? AND status = ? AND resume_owner = ? AND resume_count = 0",
            (snapshot, baseline, experiment_id, STATUS_VERIFYING, token),
        ).rowcount == 1
        if moved:
            insert_activity(
                service._conn, timestamp=stamp or _utcnow_iso(), entity_type="experiment",
                entity_id=experiment_id, event=RESUME_EVENT,
                from_status=STATUS_INTERRUPTED, to_status=STATUS_VERIFYING, message=RESUME_MESSAGE,
            )
    return moved


def _release_preparation(
    service: Any, experiment_id: str, token: str, reason: str, *, repark: bool,
) -> None:
    stamp = _utcnow_iso()
    with service._lock, service._conn:
        moved = service._conn.execute(
            "UPDATE experiments SET status = ?, resume_pending = ?, resume_owner = NULL, "
            "ended_at = ?, error = ? WHERE id = ? AND status = ? AND resume_owner = ? "
            "AND resume_count = 0",
            (STATUS_INTERRUPTED, int(repark), stamp,
             f"{_INTERRUPTED_BY_RESTART_ERROR}; resume stopped before verification: {reason}"[:2000],
             experiment_id, STATUS_VERIFYING, token),
        ).rowcount == 1
        if moved:
            insert_activity(
                service._conn, timestamp=stamp, entity_type="experiment",
                entity_id=experiment_id, event=RESUME_RECOVERED_EVENT if repark else RESUME_SKIPPED_EVENT,
                from_status=STATUS_VERIFYING, to_status=STATUS_INTERRUPTED, message=reason[:1000],
            )


def _finish(service: Any, experiment_id: str, token: str, detail: str) -> str:
    """Release a terminal claim and record its outcome once."""
    with service._lock, service._conn:
        row = service._conn.execute(
            "SELECT status, resume_count FROM experiments WHERE id = ?", (experiment_id,),
        ).fetchone()
        status = str(row["status"])
        if status == STATUS_VERIFYING:
            return status
        released = service._conn.execute(
            "UPDATE experiments SET resume_owner = NULL WHERE id = ? AND resume_owner = ?",
            (experiment_id, token),
        ).rowcount == 1
        if released and row["resume_count"]:
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


def _implementation_baseline(experiment: Any, worktree: str) -> str:
    if experiment.implementation_base_sha:
        return experiment.implementation_base_sha
    base = experiment.base_commit_sha
    if not base:
        raise ResumeStepError("verification baseline is missing")
    # Older rows recorded the issue-branch tip before Nexus merged main.
    # Recognize the merge that Nexus itself created as the first new commit.
    raw = _git_or_raise(
        ["log", "--first-parent", "--reverse", "--format=%H%x00%P%x00%ce%x00%s",
         f"{base}..HEAD", "--"],
        worktree, None, "could not inspect the legacy implementation baseline",
    )
    if raw:
        sha, parents, committer, subject = raw.splitlines()[0].split("\0", 3)
        parent_shas = parents.split()
        if (
            len(parent_shas) == 2 and parent_shas[0] == base
            and committer == _COMMIT_IDENTITY["GIT_COMMITTER_EMAIL"]
            and subject.startswith(("Merge branch 'main' of ", "Merge main; conflicts left for the coding agent"))
        ):
            return sha
    # A fast-forward does not have a merge commit. The worktree reflog
    # identifies the exact move from the recorded tip when it is retained.
    reflog = _git_or_raise(
        ["reflog", "show", "--format=%H%x00%ge%x00%gs", "HEAD"],
        worktree, None, "could not inspect the legacy worktree reflog",
    )
    entries = [line.split("\0", 2) for line in reflog.splitlines()]
    for (sha, email, subject), (previous, _, _) in zip(entries, entries[1:]):
        if (
            previous == base and email == _COMMIT_IDENTITY["GIT_COMMITTER_EMAIL"]
            and subject.startswith("merge ") and "FETCH_HEAD" in subject
            and subject.endswith(": Fast-forward")
        ):
            return sha
    return base


def snapshot_work(experiment_id: str, worktree: str, index_path: Path, elog: Any) -> tuple[str, str | None]:
    """Capture committed, staged, and unstaged work in a private Git ref.

    The staged version wins on a partially staged path. Both versions remain
    available in the original index and working tree.
    """
    head = _git_or_raise(["rev-parse", "HEAD"], worktree, elog, "could not resolve HEAD").strip()
    raw = _git_or_raise(
        ["--no-optional-locks", "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all"],
        worktree, elog, "could not list the uncommitted worktree changes",
    )
    staged = []
    unstaged = []
    excluded = []
    for record in raw.split("\0"):
        if not record:
            continue
        code, path = record[:2], record[3:]
        if "U" in code or code in {"AA", "DD"}:
            raise ResumeStepError("worktree index has unresolved conflicts")
        if not GitHubWorkTrackingService._publishable_path(path):
            excluded.append(path)
            continue
        if code[0] not in " ?":
            staged.append(path)
        else:
            unstaged.append(path)
    staged_paths = set(staged)
    unstaged = list(dict.fromkeys(path for path in unstaged if path not in staged_paths))
    commit = head
    if staged or unstaged:
        env = dict(os.environ, **_COMMIT_IDENTITY)
        env["GIT_INDEX_FILE"] = str(index_path)
        _git_or_raise(["read-tree", head], worktree, elog, "could not initialize the snapshot", env=env)
        if staged:
            # Copy the actual index entries via a binary patch, including
            # mode changes and deletions. Keep blob contents out of logs.
            try:
                patch = subprocess.run(
                    ["git", "diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv",
                     "--binary", head, "--", *GitHubWorkTrackingService._publication_pathspecs(staged, excluded)],
                    cwd=worktree, capture_output=True, check=True, timeout=_GIT_TIMEOUT_SECONDS,
                ).stdout
                if patch:
                    subprocess.run(
                        ["git", "apply", "--cached", "--binary", "--whitespace=nowarn", "-"],
                        cwd=worktree, env=env, input=patch, capture_output=True,
                        check=True, timeout=_GIT_TIMEOUT_SECONDS,
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ResumeStepError("could not copy staged changes into the resume snapshot") from exc
        if unstaged:
            _git_or_raise(
                ["add", "-A", "--", *GitHubWorkTrackingService._publication_pathspecs(unstaged, excluded)],
                worktree, elog,
                "could not stage the resume snapshot", env=env,
            )
        paths = _git_or_raise(
            ["diff", "--cached", "--name-only", "-z", "--no-renames", head, "--"],
            worktree, elog, "could not audit staged snapshot paths", env=env,
        )
        if any(not GitHubWorkTrackingService._publishable_path(path) for path in paths.split("\0") if path):
            raise ResumeStepError("resume snapshot staged excluded publication paths")
        tree = _git_or_raise(
            ["write-tree"], worktree, elog, "could not write the resume tree", env=env,
        ).strip()
        message = f"Resume snapshot of experiment {experiment_id[:8]}\n\nNexus-Experiment: {experiment_id}"
        commit = _git_or_raise(
            ["commit-tree", tree, "-p", head, "-m", message],
            worktree, elog, "could not commit the resume snapshot", env=env,
        ).strip()
    _git_or_raise(
        ["update-ref", f"{RESUME_REF_PREFIX}{experiment_id}", commit], worktree, elog,
        "could not record the resume snapshot",
    )
    return commit, commit if commit != head else None


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

    token = f"{owner_token()}/{uuid.uuid4().hex}"
    _ACTIVE_RESUMES.add(token)
    try:
        if not _claim(experiment_service, chosen, token, stamp):
            return {"resumed": False, "skipped": skipped} if skipped else None
        return _run_claimed_resume(experiment_service, chosen, token, stamp, skipped, tracking, approval_block)
    finally:
        try:
            _remove_scratch(experiment_service, token)
        finally:
            _ACTIVE_RESUMES.discard(token)


def _run_claimed_resume(
    experiment_service: Any, chosen: str, token: str, stamp: str,
    skipped: list[dict[str, str]], tracking: Any,
    approval_block: Callable[[str], str | None] | None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {"experiment_id": chosen, "resumed": False, "skipped": skipped}
    worktree = None
    try:
        elog = experiment_service.get_experiment_logger(chosen)
        elog.info(STATUS_VERIFYING, RESUME_CLAIMED_EVENT, "Preparing interrupted work for verification")
        if approval_block is not None and (blocked := approval_block(chosen)):
            raise ResumeStepError(blocked)
        experiment = experiment_service.get_experiment(chosen)
        worktree = str(experiment.worktree_path or "").strip()
        if not worktree or not Path(worktree).is_dir():
            raise ResumeStepError("worktree missing")
        root = _git_or_raise(
            ["rev-parse", "--show-toplevel"], worktree, elog, "could not resolve worktree root",
        )
        if Path(root.strip()).resolve() != Path(worktree).resolve():
            raise ResumeStepError("worktree missing")
        verify_tracking = _tracking_for(tracking, chosen)
        base = _implementation_baseline(experiment, worktree)
        scratch = _scratch_path(token)
        if scratch is None:
            raise ResumeStepError("invalid resume owner token")
        scratch.mkdir(mode=0o700)
        snapshot, new_commit = snapshot_work(chosen, worktree, scratch / "index", elog)
        _git_or_raise(
            ["merge-base", "--is-ancestor", base, snapshot], worktree, elog,
            "snapshot does not descend from the implementation baseline",
        )
        paths = _git_or_raise(
            ["diff", "--name-only", "-z", "--no-renames", base, snapshot, "--"], worktree, elog,
            "could not inspect snapshot paths",
        )
        if not paths:
            raise ResumeStepError("implementation produced no publishable changes")
        if any(not GitHubWorkTrackingService._publishable_path(path) for path in paths.split("\0") if path):
            raise ResumeStepError("committed implementation contains excluded publication paths")
        # Run local tests against the exact commit in a detached checkout.
        # Keep the original HEAD, index, and working files available for inspection.
        files = scratch / "worktree"
        _git_or_raise(
            ["worktree", "add", "--detach", "--quiet", str(files), snapshot],
            worktree, elog, "could not prepare snapshot files",
        )
        if not Path(worktree).is_dir():
            raise ResumeStepError("worktree missing")
        summary["resume_commit_sha"] = new_commit
        started = _start_verification(
            experiment_service, chosen, token, stamp, snapshot=snapshot, baseline=base,
        )
    except Exception as exc:
        # Git preparation errors are terminal. A failed logger or database
        # operation can be retried because verification has not been spent.
        missing = worktree is not None and not Path(worktree).is_dir()
        reason = "worktree missing" if missing else sanitized_reason(str(exc)) or type(exc).__name__
        repark = (
            reason != "worktree missing"
            and not isinstance(exc, ResumeStepError)
            and _claims(experiment_service, chosen) < _MAX_RESUME_CLAIMS
        )
        _release_preparation(experiment_service, chosen, token, reason, repark=repark)
        _finish(experiment_service, chosen, token, reason)
        summary.update(status=STATUS_INTERRUPTED, skipped=[*skipped, {"experiment_id": chosen, "reason": reason}])
        if repark:
            raise
        return summary

    if not started:
        summary["status"] = _finish(experiment_service, chosen, token, "status changed before verification")
        return summary

    summary.update(resumed=True, resumed_at=stamp)
    try:
        _report(tracking, chosen, RESUME_EVENT)
        experiment_service._verify(
            chosen, worktree, elog, github_work_tracking=verify_tracking,
            snapshot_sha=snapshot, verification_base_sha=base, snapshot_test_path=str(files),
        )
    except Exception as exc:
        logger.warning("resume verification of %s failed: %s", chosen, exc)
        experiment_service._transition(
            chosen, STATUS_VERIFYING, STATUS_NEEDS_REVIEW, ended_at=_utcnow_iso(),
            error=f"resume verification error: {type(exc).__name__}: {sanitized_reason(str(exc))}",
        )
    finally:
        summary["status"] = _finish(experiment_service, chosen, token, "verification ended")

    from .service_parts.implementation import _final_issue_progress

    for state, progress in _final_issue_progress(experiment_service.get_experiment(chosen)):
        _report(tracking, chosen, state, progress)
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
