"""Take back a Nexus merge that turned main red: when the tip of main is a Nexus
merge (``Nexus-Experiment:`` trailer) whose CI failed, revert it, reopen its issue,
and let the tick report it - a merge that broke main must not stay while later
experiments build on it (#410). Only Nexus's own merge at the tip is reverted; a
human commit, or a Nexus merge something else already built on, is reported, not
undone.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from typing import Any, Callable

logger = logging.getLogger(__name__)

NEXUS_TRAILER = "Nexus-Experiment:"
_WORKFLOW = "tests.yml"

Runner = Callable[..., subprocess.CompletedProcess]


def _run(runner: Runner, argv: list[str], cwd: str) -> subprocess.CompletedProcess:
    return runner(argv, cwd=cwd, capture_output=True, text=True, timeout=120, check=False)


def _gh_json(runner: Runner, repo_root: str, path: str) -> Any:
    proc = _run(runner, ["gh", "api", path], repo_root)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300] or "gh api failed")
    return json.loads(proc.stdout or "null")


def guard_main(repo_root: str, runner: Runner = subprocess.run) -> str | None:
    """Revert a red Nexus merge at the tip of main; a message for the owner, or None."""
    try:
        head = _gh_json(runner, repo_root, "repos/{owner}/{repo}/commits/main")
        sha = str(head["sha"])
        message = str(head["commit"]["message"])
        if NEXUS_TRAILER not in message:
            return None
        runs = _gh_json(
            runner, repo_root,
            f"repos/{{owner}}/{{repo}}/actions/workflows/{_WORKFLOW}/runs?head_sha={sha}&per_page=5",
        ).get("workflow_runs") or []
    except (RuntimeError, KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        logger.warning("main guard: lookup failed: %s", exc)
        return None
    completed = [r for r in runs if r.get("head_sha") == sha and r.get("status") == "completed"]
    if not completed or not any(r.get("conclusion") == "failure" for r in completed):
        return None

    title = message.splitlines()[0][:120]
    if not _revert(runner, repo_root, sha):
        return f"main is red after Nexus merge {sha[:8]} ({title}); the automatic revert failed"
    issue = re.search(r"\bCloses #(\d+)", message)
    if issue:
        number = issue.group(1)
        _run(runner, ["gh", "issue", "reopen", number], repo_root)
        _run(runner, ["gh", "issue", "comment", number, "--body",
                      f"Reopened: merging this ({sha[:8]}) turned CI on main red, so it was reverted automatically."],
             repo_root)
    return f"Reverted Nexus merge {sha[:8]} ({title}): CI on main failed after it"


def _revert(runner: Runner, repo_root: str, sha: str) -> bool:
    """Revert *sha* on top of origin/main in a scratch worktree and push it."""
    with tempfile.TemporaryDirectory(prefix="nexus-revert-") as scratch:
        worktree = f"{scratch}/main"
        steps = [
            (["git", "fetch", "origin", "main"], repo_root),
            (["git", "worktree", "add", "--detach", worktree, "origin/main"], repo_root),
            (["git", "revert", "--no-edit", sha], worktree),
            (["git", "push", "origin", "HEAD:main"], worktree),
        ]
        ok = True
        for argv, cwd in steps:
            proc = _run(runner, argv, cwd)
            if proc.returncode != 0:
                logger.warning("main guard: %s failed: %s", " ".join(argv[:3]), proc.stderr.strip()[:300])
                ok = False
                break
        _run(runner, ["git", "worktree", "remove", "--force", worktree], repo_root)
        return ok


__all__ = ["guard_main", "NEXUS_TRAILER"]
