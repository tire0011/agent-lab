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
# The parked run's evidence. A re-verification that does not pass restores it,
# so the experiment keeps its original error and stays eligible on the next
# main commit; the new verdict lives only in ``reverifications`` (#718 review).
_EVIDENCE_COLUMNS = (
    "status", "error", "ended_at", "worktree_path", "base_commit_sha",
    "test_command_json", "test_exit_code", "test_output", "git_status",
    "diff_stat", "diff_text", "quality_evaluation_json", "failure_phase",
    "failure_type", "baseline_test_classifications_json",
)
# Verdicts that did not really run the branch; they do not count toward the
# per-tick limit, so closed issues can never starve open ones.
SKIPPED_PREFIX = "skipped:"
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


def _snapshot(service: Any, experiment_id: str) -> dict[str, Any]:
    with service._lock:
        row = service._conn.execute(
            f"SELECT {', '.join(_EVIDENCE_COLUMNS)} FROM experiments WHERE id=?",
            (experiment_id,),
        ).fetchone()
    return dict(row) if row is not None else {}


def _restore(service: Any, experiment_id: str, snapshot: dict[str, Any]) -> None:
    if not snapshot:
        return
    columns = list(snapshot)
    with service._lock, service._conn:
        service._conn.execute(
            f"UPDATE experiments SET {', '.join(f'{c}=?' for c in columns)} WHERE id=?",
            (*(snapshot[c] for c in columns), experiment_id),
        )


def _has_origin(repo: str) -> bool:
    return _run_git(["remote", "get-url", "origin"], cwd=repo).returncode == 0


def current_main(repo: str) -> str:
    """The commit to rebase onto: ``origin/main`` after a fetch.

    On the VM merges land through GitHub, so local ``main`` can be stale. A
    repository without an ``origin`` remote (local use) falls back to ``main``.
    """
    if _has_origin(repo):
        fetch = _run_git(["fetch", "--quiet", "origin", "main"], cwd=repo, timeout=120)
        if fetch.returncode:
            raise RuntimeError(fetch.stderr.strip() or "git fetch origin main failed")
        ref = "refs/remotes/origin/main"
    else:
        ref = "main"
    main = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo)
    if main.returncode:
        raise RuntimeError(main.stderr.strip() or f"cannot resolve {ref}")
    return main.stdout.strip()


def _branch_sha(repo: str, branch: str) -> str | None:
    """The published branch head (fetched from origin when there is one)."""
    if _has_origin(repo):
        fetch = _run_git(
            ["fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            cwd=repo, timeout=120,
        )
        if fetch.returncode == 0:
            ref = _run_git(["rev-parse", "--verify", f"refs/remotes/origin/{branch}"], cwd=repo)
            return ref.stdout.strip() if ref.returncode == 0 else None
    ref = _run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo)
    return ref.stdout.strip() if ref.returncode == 0 else None


def _record(service: Any, experiment_id: str, issue_number: int | None, main_sha: str,
            branch: str | None, old_sha: str | None, new_sha: str | None,
            verdict: str, reason: str | None, started: str) -> dict[str, Any]:
    with service._lock, service._conn:
        service._conn.execute(
            """INSERT OR IGNORE INTO reverifications
               (experiment_id, issue_number, main_commit_sha, branch_name,
                old_branch_sha, new_branch_sha, verdict, reason, created_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (experiment_id, issue_number, main_sha, branch or None, old_sha, new_sha,
             verdict, reason, started, _now()),
        )
    return {"experiment_id": experiment_id, "issue_number": issue_number,
            "main_commit_sha": main_sha, "verdict": verdict, "reason": reason}


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
    """Newest eligible experiments not yet checked for this exact main tree.

    Closed or unresolvable issues get a ``skipped:`` row for the main commit,
    so they drop out here and older parked work with open issues is reached.
    """
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
    main_sha: str | None = None,
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
    if main_sha is None:
        main_sha = current_main(repo)
    with service._lock:
        if service._conn.execute(
            "SELECT 1 FROM reverifications WHERE experiment_id=? AND main_commit_sha=?",
            (experiment_id, main_sha),
        ).fetchone():
            return None
    issue_number = _issue_number(service, experiment)
    # Re-verification must never resurrect work for a closed (or unresolvable)
    # issue.  Without an openness check (no GitHub access) nothing is recorded,
    # so a later tick with access can try; a known closed or unresolvable issue
    # is recorded as skipped for this main commit so it stops being picked.
    if issue_is_open is None:
        return None
    started = _now()
    if issue_number is None or not issue_is_open(issue_number):
        verdict = f"{SKIPPED_PREFIX}{'no_issue' if issue_number is None else 'issue_closed'}"
        return _record(service, experiment_id, issue_number, main_sha,
                       experiment.branch_name, None, None, verdict, None, started)
    branch = str(experiment.branch_name or "").strip()
    snapshot = _snapshot(service, experiment_id)
    old_sha: str | None = None
    new_sha: str | None = None
    reason: str | None = None
    verdict = "branch_missing"
    temp_root = Path(tempfile.mkdtemp(prefix="nexus-reverify-"))
    worktree = temp_root / "worktree"
    added = False
    keep_worktree = False
    try:
        old_sha = _branch_sha(repo, branch) if branch else None
        if old_sha is None:
            reason = "branch does not exist locally or on origin"
        else:
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
                    # Same gate as the original GitHub-tracked run: soft quality
                    # findings go to the reviewer at adoption (#519), not to a
                    # hard needs_review stop.
                    result = service._verify(
                        experiment_id, str(worktree), soft_findings_to_reviewer=True,
                    )
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
                        # Adoption validates and integrates the exact worktree
                        # which verification passed.  Keep this registered
                        # temporary worktree alive until the normal adoption
                        # path consumes it; deleting it here would make the
                        # freshly succeeded experiment impossible to adopt.
                        keep_worktree = True
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
                    # A deterministic failure found by this maintenance pass
                    # is still parked work, not a new failed coding attempt.
                    # Keeping the terminal state as needs_review also makes the
                    # verdict and experiment lifecycle agree.
                    service._force_status(
                        experiment_id, STATUS_NEEDS_REVIEW, error=reason, ended_at=_now(),
                    )
    except Exception as exc:  # every attempted run gets a durable verdict
        reason = f"reverification error: {type(exc).__name__}: {str(exc)[:1000]}"
        verdict = f"still_blocked:{reason}"
        current = service.get_experiment(experiment_id)
        if current.status == STATUS_VERIFYING:
            service._force_status(
                experiment_id, STATUS_NEEDS_REVIEW, error=reason, ended_at=_now(),
            )
    finally:
        if added and not keep_worktree:
            _run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo)
        if not keep_worktree:
            shutil.rmtree(temp_root, ignore_errors=True)

    if verdict != "passes_now":
        # Not a pass: the parked run's evidence and error stand unchanged, so
        # the experiment stays eligible and nothing points at a deleted path.
        _restore(service, experiment_id, snapshot)
    return _record(service, experiment_id, issue_number, main_sha, branch,
                   old_sha, new_sha, verdict, reason, started)
