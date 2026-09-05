"""Generic restart-recovery evidence for the mailbox command channel.

A mailbox ``command`` message may run a shell command that intentionally
restarts Nexus (for example a self-refresh: pull the latest code, then
restart services). The detached runner process can be killed by that
restart before it writes its exit code, so a later poll only sees a dead
pid with no recorded outcome.

This module supplies operation-agnostic evidence for that situation. It
never inspects the command text and is not specific to ``refresh.sh``: it
only compares the target repository's git state before/after the
interruption. Any command that leaves the repository at a clean commit
matching its upstream branch is treated as verified success; a command
that leaves it at a different, non-upstream commit or with uncommitted
changes is treated as a verified failure; anything that cannot be
distinguished (no git repository, no recorded baseline, no upstream
branch) is reported as unknown rather than guessed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .core import _git, git_current_commit, git_is_clean

logger = logging.getLogger(__name__)

RECOVERY_FILE_NAME = "recovery.json"

RecoveryStatus = Literal["succeeded", "failed", "unknown"]


def _is_git_repo(repo_path: Path) -> bool:
    result = _git(["rev-parse", "--is-inside-work-tree"], repo_path, check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def capture_pre_start_state(repo_path: Path) -> dict:
    """Best-effort, local-only snapshot taken before a command runs.

    Only runs ``git rev-parse HEAD`` in *repo_path* — no network access, so
    this stays cheap on the hot path where a command finishes normally and
    recovery evidence is never consulted. Returns ``{"pre_commit": None}``
    when *repo_path* is not a git repository so the command channel keeps
    working for arbitrary non-git working directories.
    """
    if not _is_git_repo(repo_path):
        return {"pre_commit": None}
    return {"pre_commit": git_current_commit(repo_path)}


def write_recovery_state(state_dir: Path, repo_path: Path) -> None:
    """Persist the pre-execution snapshot next to the command's job state."""
    state = capture_pre_start_state(repo_path)
    state["repo_path"] = str(repo_path)
    try:
        (state_dir / RECOVERY_FILE_NAME).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        logger.warning("Failed to persist mailbox command recovery state in %s", state_dir)


def _read_recovery_state(state_dir: Path) -> dict:
    path = state_dir / RECOVERY_FILE_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class RecoveryOutcome:
    status: RecoveryStatus
    evidence: str


def evaluate_interrupted_command(state_dir: Path, repo_path: Path) -> RecoveryOutcome:
    """Decide whether an interrupted command reached a verifiable end state.

    Called once a command's runner process has been confirmed dead without
    ever writing an exit code (typically because it — or Nexus itself — was
    killed by a restart the command triggered).
    """
    recorded = _read_recovery_state(state_dir)
    pre_commit = recorded.get("pre_commit")

    if not _is_git_repo(repo_path):
        return RecoveryOutcome(
            "unknown",
            "Interrupted worker left no exit code and the working directory "
            "is not a git repository, so no recovery evidence is available.",
        )

    current = git_current_commit(repo_path)
    if current == "unknown":
        return RecoveryOutcome(
            "unknown",
            "Interrupted worker left no exit code and the current git HEAD "
            "could not be determined.",
        )

    if pre_commit is None:
        return RecoveryOutcome(
            "unknown",
            "Interrupted worker left no exit code and no pre-execution git "
            "state was recorded, so the outcome cannot be reconstructed.",
        )

    if current == pre_commit:
        return RecoveryOutcome(
            "unknown",
            "Interrupted worker left no exit code; the repository HEAD "
            f"({current}) is unchanged since the command started, so no "
            "recovery evidence distinguishes success from failure.",
        )

    upstream = _git(["rev-parse", "@{u}"], repo_path, check=False)
    upstream_commit = upstream.stdout.strip() if upstream.returncode == 0 else None
    clean = git_is_clean(repo_path)

    if clean and upstream_commit is not None and current == upstream_commit:
        return RecoveryOutcome(
            "succeeded",
            "Interrupted worker left no exit code, but the repository "
            f"advanced from {pre_commit} to {current}, matches its "
            "upstream branch, and the working tree is clean — the "
            "operation is verified to have completed.",
        )

    return RecoveryOutcome(
        "failed",
        f"Interrupted worker left no exit code; the repository moved from "
        f"{pre_commit} to {current} but did not reach a clean working tree "
        f"matching its upstream branch (clean={clean}, "
        f"upstream={upstream_commit or 'none'}) — treating the operation as "
        "failed rather than assuming success.",
    )
