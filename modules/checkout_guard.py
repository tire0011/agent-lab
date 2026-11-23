"""Keep coding agents out of the deployed checkout.

2026-11-22 a coding agent was given its experiment worktree but edited
``/agent/nexus_tick.py`` and ``/agent/modules/nexus_planner.py`` - the code
Nexus itself runs - which then ran unreviewed for ten hours. Not every agent
CLI has a file sandbox, so every run is guarded the same way, whatever the
provider:

- the prompt names the working directory and forbids the deployed checkout;
- the deployed checkout's ``git status`` is compared before and after the run;
- new changes fail the run, are moved into a git stash (recoverable, so the
  running code is the reviewed code again) and are recorded with what is
  needed to narrow down the cause: provider, files, and whether the prompt
  itself mentioned the checkout path.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

EVENT = "agent_wrote_outside_worktree"


def _porcelain(repo_root: str) -> set[str] | None:
    try:
        done = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return {line for line in done.stdout.splitlines() if line.strip()}


def _stash(repo_root: str, message: str) -> str | None:
    """Stash all changes (incl. untracked); the stash commit sha, or None."""
    try:
        done = subprocess.run(
            ["git", "-C", repo_root, "-c", "user.name=Nexus", "-c", "user.email=nexus@agent-lab.local",
             "stash", "push", "--include-untracked", "-m", message],
            capture_output=True, text=True, timeout=120,
        )
        if done.returncode != 0:
            return None
        sha = subprocess.run(["git", "-C", repo_root, "rev-parse", "stash@{0}"],
                             capture_output=True, text=True, timeout=30)
        return sha.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def hint(working_dir: str, repo_root: str) -> str:
    return (
        f"Work only inside your working directory {working_dir}. The deployed "
        f"checkout {repo_root} is off-limits: never read from or write to it, even "
        "where documentation names paths below it."
    )


class GuardedWorker:
    """Wraps a coding-agent worker; all other attributes pass through."""

    def __init__(self, worker: Any, repo_root: str) -> None:
        self._worker = worker
        self._repo_root = str(repo_root)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._worker, name)

    def run(self, task: str, working_dir: str, **kwargs: Any) -> Any:
        root = Path(self._repo_root)
        try:
            same = root.resolve() == Path(working_dir).resolve()
        except OSError:
            same = False
        if same or not (root / ".git").exists():
            return self._worker.run(task=task, working_dir=working_dir, **kwargs)
        prompt_mentions_root = self._repo_root in task
        guarded_task = task.rstrip() + "\n\n" + hint(working_dir, self._repo_root)
        before = _porcelain(self._repo_root)
        result = self._worker.run(task=guarded_task, working_dir=working_dir, **kwargs)
        after = _porcelain(self._repo_root)
        if before is None or after is None:
            return result
        new = sorted(after - before)
        if not new:
            return result
        provider = getattr(self._worker, "provider", None) or type(self._worker).__name__
        stash = None
        if not before:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            stash = _stash(self._repo_root, f"nexus guard {stamp}: {provider} wrote outside {working_dir}")
        files = [line[3:] for line in new]
        message = (
            f"coding agent {provider} wrote outside its worktree into {self._repo_root}: "
            f"{', '.join(files[:10])}"
            + (f" (moved to git stash {stash[:12]})" if stash else " (left in place: the checkout was not clean before)")
        )
        logger = kwargs.get("experiment_logger")
        if logger is not None:
            try:
                logger.error(
                    kwargs.get("phase") or "implementation", EVENT, message,
                    provider=str(provider), files=files, stash=stash or "",
                    prompt_mentions_repo_root=prompt_mentions_root,
                    working_dir=str(working_dir),
                )
            except Exception:  # noqa: BLE001 - logging must not hide the failure
                pass
        try:
            return replace(result, success=False, error=message)
        except TypeError:
            return result


def guard_factory(factory: Callable[..., Any], repo_root: str) -> Callable[..., Any]:
    def guarded(*args: Any, **kwargs: Any) -> Any:
        return GuardedWorker(factory(*args, **kwargs), repo_root)
    return guarded
