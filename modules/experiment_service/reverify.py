"""Free re-verification of timing-check experiments parked for human review."""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .git_utils import _run_git
from .models import STATUS_NEEDS_REVIEW, STATUS_SUCCEEDED, STATUS_VERIFYING

_ALLOWED_ERROR_PREFIXES = ("timing check failed closed",)
_ISSUE_RE = re.compile(r"\bGitHub\s+Issue\s+#(\d+)\b", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _issue_number(service: Any, experiment: Any) -> int | None:
    try:
        with service._lock:
            row = service._conn.execute(
                "SELECT issue_number FROM github_experiment_links WHERE experiment_id=?",
                (experiment.id,),
            ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        return int(row[0])
    match = _ISSUE_RE.search(experiment.goal or "")
    return int(match.group(1)) if match else None


def list_reverifications(service: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Return recent durable re-verification verdicts, newest first."""
    if limit <= 0:
        return []
    with service._lock:
        rows = service._conn.execute(
            "SELECT * FROM reverifications ORDER BY created_at DESC, id DESC LIMIT ?",
            (min(limit, 200),),
        ).fetchall()
    return [dict(row) for row in rows]


def parked_candidates(service: Any, main_commit_sha: str, limit: int = 3) -> list[str]:
    """Newest eligible experiments not yet checked for this exact main tree."""
    with service._lock:
        rows = service._conn.execute(
            """SELECT e.id FROM experiments e
               WHERE e.status=? AND e.error LIKE 'timing check failed closed%'
                 AND NOT EXISTS (
                   SELECT 1 FROM reverifications r
                   WHERE r.experiment_id=e.id AND r.main_commit_sha=?
                 )
               ORDER BY e.created_at DESC, e.id DESC LIMIT ?""",
            (STATUS_NEEDS_REVIEW, main_commit_sha, max(0, limit)),
        ).fetchall()
    return [str(row[0]) for row in rows]


def reverify_parked(
    experiment_id: str,
    *,
    service: Any,
    issue_is_open: Callable[[int], bool] | None = None,
) -> dict[str, Any] | None:
    """Rebase and run the normal verifier without calling a coding model.

    ``None`` means ineligible or already checked at the current main commit.
    A failed/conflicting rebase is performed in a detached temporary worktree,
    so the published branch remains byte-for-byte untouched.
    """
    experiment = service.get_experiment(experiment_id)
    if experiment.status != STATUS_NEEDS_REVIEW or not str(experiment.error or "").startswith(
        _ALLOWED_ERROR_PREFIXES
    ):
        return None
    repo = str(service.repo_root)
    main = _run_git(["rev-parse", "main"], cwd=repo)
    if main.returncode:
        raise RuntimeError(main.stderr.strip() or "cannot resolve main")
    main_sha = main.stdout.strip()
    with service._lock:
        if service._conn.execute(
            "SELECT 1 FROM reverifications WHERE experiment_id=? AND main_commit_sha=?",
            (experiment_id, main_sha),
        ).fetchone():
            return None
    issue_number = _issue_number(service, experiment)
    if issue_number is not None and issue_is_open is not None and not issue_is_open(issue_number):
        return None
    branch = str(experiment.branch_name or "").strip()
    started = _now()
    old_sha: str | None = None
    new_sha: str | None = None
    reason: str | None = None
    verdict = "branch_missing"
    temp_root = Path(tempfile.mkdtemp(prefix="nexus-reverify-"))
    worktree = temp_root / "worktree"
    added = False
    try:
        branch_ref = _run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo)
        if branch_ref.returncode:
            branch_ref = _run_git(["rev-parse", "--verify", f"refs/remotes/origin/{branch}"], cwd=repo)
        if not branch or branch_ref.returncode:
            reason = "branch does not exist locally or on origin"
        else:
            old_sha = branch_ref.stdout.strip()
            add = _run_git(["worktree", "add", "--detach", str(worktree), old_sha], cwd=repo)
            if add.returncode:
                raise RuntimeError(add.stderr.strip() or "could not create re-verification worktree")
            added = True
            rebase = _run_git(["rebase", main_sha], cwd=str(worktree), timeout=300)
            if rebase.returncode:
                verdict = "rebase_conflict"
                reason = (rebase.stderr or rebase.stdout).strip()[:2000]
                _run_git(["rebase", "--abort"], cwd=str(worktree))
            else:
                new_sha = _run_git(["rev-parse", "HEAD"], cwd=str(worktree)).stdout.strip()
                # The normal verifier evaluates the implementation from the
                # worktree/index. Re-expose the rebased commit as an uncommitted
                # main..branch change, exactly like the original coding run.
                reset = _run_git(["reset", "--mixed", main_sha], cwd=str(worktree))
                if reset.returncode:
                    raise RuntimeError(reset.stderr.strip() or "could not prepare verification diff")
                service._force_status(
                    experiment_id, STATUS_VERIFYING, worktree_path=str(worktree),
                    base_commit_sha=main_sha, error=None, ended_at=None,
                )
                try:
                    result = service._verify(experiment_id, str(worktree))
                except Exception as exc:  # a crashed verifier is not a pass
                    reason = f"verification error: {type(exc).__name__}: {str(exc)[:1000]}"
                    verdict = f"still_blocked:{reason}"
                    service._force_status(
                        experiment_id, STATUS_NEEDS_REVIEW, error=reason, ended_at=_now(),
                    )
                    result = None
                if result is not None and result.status == STATUS_SUCCEEDED:
                    restore = _run_git(["reset", "--hard", new_sha], cwd=str(worktree))
                    if restore.returncode:
                        reason = restore.stderr.strip() or "could not restore verified commit"
                        verdict = f"still_blocked:{reason}"
                        service._force_status(
                            experiment_id, STATUS_NEEDS_REVIEW, error=reason, ended_at=_now(),
                        )
                        result = None
                if result is not None and result.status == STATUS_SUCCEEDED:
                    push = _run_git(
                        ["push", "origin", f"HEAD:refs/heads/{branch}",
                         f"--force-with-lease=refs/heads/{branch}:{old_sha}"],
                        cwd=str(worktree), timeout=300,
                    )
                    if push.returncode:
                        verdict = "still_blocked:push failed"
                        reason = (push.stderr or push.stdout).strip()[:2000]
                        service._force_status(experiment_id, STATUS_NEEDS_REVIEW, error=reason, ended_at=_now())
                    else:
                        verdict = "passes_now"
                        # Adoption and its mandatory review/CI checks must inspect
                        # the rebased commit, never the stale pre-rebase checkpoint.
                        try:
                            with service._lock, service._conn:
                                service._conn.execute(
                                    "UPDATE github_experiment_links SET checkpoint_sha=?, updated_at=? "
                                    "WHERE experiment_id=?",
                                    (new_sha, _now(), experiment_id),
                                )
                        except sqlite3.OperationalError:
                            pass
                elif result is not None:
                    reason = str(result.error or f"verification ended {result.status}")
                    verdict = f"still_blocked:{reason}"
    except Exception as exc:  # every attempted run gets a durable verdict
        reason = f"reverification error: {type(exc).__name__}: {str(exc)[:1000]}"
        verdict = f"still_blocked:{reason}"
        current = service.get_experiment(experiment_id)
        if current.status == STATUS_VERIFYING:
            service._force_status(
                experiment_id, STATUS_NEEDS_REVIEW, error=reason, ended_at=_now(),
            )
    finally:
        if added:
            _run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo)
        shutil.rmtree(temp_root, ignore_errors=True)

    finished = _now()
    with service._lock, service._conn:
        service._conn.execute(
            """INSERT OR IGNORE INTO reverifications
               (experiment_id, issue_number, main_commit_sha, branch_name,
                old_branch_sha, new_branch_sha, verdict, reason, created_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (experiment_id, issue_number, main_sha, branch or None, old_sha, new_sha,
             verdict, reason, started, finished),
        )
    return {"experiment_id": experiment_id, "issue_number": issue_number,
            "main_commit_sha": main_sha, "verdict": verdict, "reason": reason}
