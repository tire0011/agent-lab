"""How far an experiment's branch has drifted from main (#489).

Read-only: the only thing that changes is the local ``origin/main``
remote-tracking ref (``git fetch origin main``). The working tree, the index
and every local branch stay untouched.

The planner uses ``commits_behind`` and ``overlap_files`` to see, before it
spends an attempt, that resuming or adopting a stale branch would likely end
in an ``Integrationskonflikt``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

MAIN_BRANCH = "main"
MAIN_REF = f"origin/{MAIN_BRANCH}"
_GIT_TIMEOUT_SECONDS = 30
_FETCH_TIMEOUT_SECONDS = 60

STATUS_OK = "ok"
STATUS_UNKNOWN = "unknown"

Runner = Callable[..., subprocess.CompletedProcess]


def _unknown(branch: str | None, reason: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": STATUS_UNKNOWN,
        "reason": reason,
        "branch": branch,
        "main_ref": MAIN_REF,
        "merge_base": None,
        "commits_behind": None,
        "commits_ahead": None,
        "files_changed_on_branch": [],
        "files_changed_on_main_since_base": [],
        "overlap_files": [],
    }
    result.update(extra)
    return result


def _git(runner: Runner, args: list[str], cwd: str, timeout: int = _GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess | None:
    """Run one git command; ``None`` when it could not run at all."""
    try:
        return runner(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ok(proc: subprocess.CompletedProcess | None) -> bool:
    return proc is not None and proc.returncode == 0


def _lines(proc: subprocess.CompletedProcess | None) -> list[str]:
    if not _ok(proc):
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _is_git_dir(path: str | None, runner: Runner) -> bool:
    if not path or not Path(path).is_dir():
        return False
    proc = _git(runner, ["rev-parse", "--git-dir"], cwd=path, timeout=10)
    return _ok(proc)


def fetch_main(repo_root: str, *, runner: Runner = subprocess.run) -> tuple[bool, str | None]:
    """``git fetch origin main``; returns (fetched, error)."""
    proc = _git(runner, ["fetch", "--quiet", "origin", MAIN_BRANCH], cwd=repo_root, timeout=_FETCH_TIMEOUT_SECONDS)
    if proc is None:
        return False, "git fetch could not run"
    if proc.returncode != 0:
        return False, " ".join((proc.stderr or proc.stdout or "git fetch failed").split())[:200]
    return True, None


def _resolve_branch_ref(branch: str, cwd: str, runner: Runner) -> str | None:
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        if _ok(_git(runner, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=cwd, timeout=10)):
            return ref
    return None


def branch_drift(
    branch: str | None,
    *,
    repo_root: str,
    worktree_path: str | None = None,
    fetch: bool = True,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Drift of ``branch`` against ``origin/main``; never raises.

    ``files_changed_on_branch`` also counts uncommitted and untracked work in
    the experiment's worktree when that still exists: experiments often leave
    their change uncommitted until adoption.
    """
    branch = (branch or "").strip() or None
    worktree_exists = bool(worktree_path) and Path(str(worktree_path)).is_dir()
    extra = {"worktree_path": worktree_path, "worktree_exists": worktree_exists, "fetched": False, "fetch_error": None}
    if branch is None:
        return _unknown(None, "experiment has no branch", **extra)
    cwd = repo_root if _is_git_dir(repo_root, runner) else None
    if cwd is None and worktree_exists and _is_git_dir(worktree_path, runner):
        cwd = str(worktree_path)
    if cwd is None:
        return _unknown(branch, "repository or worktree not found", **extra)

    if fetch:
        extra["fetched"], extra["fetch_error"] = fetch_main(cwd, runner=runner)
    if not _ok(_git(runner, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{MAIN_REF}^{{commit}}"], cwd=cwd, timeout=10)):
        return _unknown(branch, f"{MAIN_REF} not found", **extra)
    branch_ref = _resolve_branch_ref(branch, cwd, runner)
    if branch_ref is None:
        return _unknown(branch, f"branch {branch} not found", **extra)

    base_proc = _git(runner, ["merge-base", MAIN_REF, branch_ref], cwd=cwd)
    merge_base = (base_proc.stdout or "").strip() if _ok(base_proc) else ""
    if not merge_base:
        return _unknown(branch, f"no merge-base between {branch} and {MAIN_REF}", **extra)

    behind = _git(runner, ["rev-list", "--count", f"{branch_ref}..{MAIN_REF}"], cwd=cwd)
    ahead = _git(runner, ["rev-list", "--count", f"{MAIN_REF}..{branch_ref}"], cwd=cwd)
    try:
        commits_behind = int((behind.stdout or "").strip()) if _ok(behind) else None
        commits_ahead = int((ahead.stdout or "").strip()) if _ok(ahead) else None
    except ValueError:
        commits_behind = commits_ahead = None
    if commits_behind is None:
        return _unknown(branch, "commit count failed", merge_base=merge_base, **extra)

    on_main = set(_lines(_git(runner, ["diff", "--name-only", merge_base, MAIN_REF], cwd=cwd)))
    on_branch = set(_lines(_git(runner, ["diff", "--name-only", merge_base, branch_ref], cwd=cwd)))
    if worktree_exists and _is_git_dir(worktree_path, runner):
        # Only when that worktree really has this branch checked out.
        head = _git(runner, ["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(worktree_path), timeout=10)
        if _ok(head) and (head.stdout or "").strip() == branch:
            on_branch.update(_lines(_git(runner, ["diff", "--name-only", merge_base], cwd=str(worktree_path))))
            on_branch.update(_lines(_git(runner, ["ls-files", "--others", "--exclude-standard"], cwd=str(worktree_path))))

    return {
        "status": STATUS_OK,
        "reason": None,
        "branch": branch,
        "main_ref": MAIN_REF,
        "merge_base": merge_base,
        "commits_behind": commits_behind,
        "commits_ahead": commits_ahead,
        "files_changed_on_branch": sorted(on_branch),
        "files_changed_on_main_since_base": sorted(on_main),
        "overlap_files": sorted(on_branch & on_main),
        **extra,
    }


def experiment_drift(
    experiment_service: Any,
    experiment_id: str,
    *,
    fetch: bool = True,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """``branch_drift`` for one experiment; unknown ids raise ``ExperimentNotFoundError``."""
    experiment = experiment_service.resolve_experiment(experiment_id)
    result = branch_drift(
        getattr(experiment, "branch_name", None),
        repo_root=str(getattr(experiment_service, "repo_root", "") or ""),
        worktree_path=getattr(experiment, "worktree_path", None),
        fetch=fetch,
        runner=runner,
    )
    return {"experiment_id": experiment.id, **result}


def drift_summary(drift: dict[str, Any] | None, *, max_files: int = 5) -> str:
    """Short planner text: ``behind main: 12 commits; overlap: a.py, b.py``."""
    if not drift:
        return ""
    if drift.get("status") != STATUS_OK:
        return f"drift: unknown ({drift.get('reason') or 'not available'})"
    overlap = list(drift.get("overlap_files") or [])
    shown = ", ".join(overlap[:max_files]) + (f" +{len(overlap) - max_files} more" if len(overlap) > max_files else "")
    return f"commits_behind={drift.get('commits_behind')}; overlap_files=[{shown}]"
