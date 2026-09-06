"""Generic restart-recovery evidence for the mailbox command channel.

A mailbox ``command`` message may run a shell command that intentionally
restarts Nexus (for example a self-refresh: pull the latest code, then
restart services). The detached runner process can be killed by that
restart before it writes its exit code, so a later poll only sees a dead
pid with no recorded outcome.

This module supplies operation-agnostic evidence for that situation. It
never inspects the command text: it only compares the target repository's
git state before/after the interruption, plus an optional, explicitly
*declared* set of systemd services the command expects to end up active.

Git convergence (a clean HEAD matching its upstream branch) proves only
that a git update step completed — not that the rest of a multi-step
command (such as a service restart after the pull) ever ran or succeeded.
So git convergence by itself is reported as ``unknown``, never
``succeeded``, unless the command's author has declared what "complete"
means for it via the message's ``recovery_services`` field:

- not declared at all -> git convergence alone stays ``unknown``; only
  git evidence of definite failure (HEAD moved but did not converge) is
  ever reported as ``failed``.
- ``recovery_services: none`` -> the author declares that reaching a
  clean, converged git state *is* the complete expected state for this
  command; git convergence alone is then reported as ``succeeded``.
- ``recovery_services: svc-a, svc-b`` -> the author declares that the
  command is only complete once git has converged *and* every listed
  systemd unit is active; any listed service found inactive is reported
  as a verified ``failed`` (this is exactly the "pull converged but the
  restart did not complete" case), not a false success.

Anything that cannot be distinguished (no git repository, no recorded
baseline, unchanged HEAD, or declared services whose state cannot be
determined) is reported as ``unknown`` rather than guessed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Sequence

from .core import _git, git_current_commit, git_is_clean

logger = logging.getLogger(__name__)

RECOVERY_FILE_NAME = "recovery.json"

RecoveryStatus = Literal["succeeded", "failed", "unknown"]

# Sentinel meta value meaning "git convergence alone is the complete
# expected state for this command" (as opposed to the field being absent,
# which means no expected-state declaration was made at all).
_NO_SERVICES_SENTINEL = "none"

ServiceChecker = Callable[[Sequence[str]], "bool | None"]


def _is_git_repo(repo_path: Path) -> bool:
    result = _git(["rev-parse", "--is-inside-work-tree"], repo_path, check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def parse_expected_services(meta: dict | None) -> list[str] | None:
    """Parse the optional ``recovery_services`` mailbox message field.

    Returns ``None`` when the command made no declaration (the safe
    default), ``[]`` when it explicitly declared git convergence as its
    complete expected state (``recovery_services: none``), or the list of
    systemd unit names it expects to be active otherwise.
    """
    if not meta:
        return None
    raw = meta.get("recovery_services")
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw or raw.lower() == _NO_SERVICES_SENTINEL:
        return []
    return [name.strip() for name in raw.split(",") if name.strip()]


def _systemctl_is_active(names: Sequence[str]) -> bool | None:
    """Best-effort check that every named systemd unit is active.

    Returns ``True``/``False`` when the state could be determined, or
    ``None`` when ``systemctl`` is unavailable (e.g. in a non-systemd/test
    environment) and the check is therefore undetermined rather than failed.
    """
    if shutil.which("systemctl") is None:
        return None
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", *names],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.returncode == 0


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


def write_recovery_state(
    state_dir: Path,
    repo_path: Path,
    expected_services: list[str] | None = None,
) -> None:
    """Persist the pre-execution snapshot next to the command's job state."""
    state = capture_pre_start_state(repo_path)
    state["repo_path"] = str(repo_path)
    state["expected_services"] = expected_services
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


def evaluate_interrupted_command(
    state_dir: Path,
    repo_path: Path,
    service_checker: ServiceChecker = _systemctl_is_active,
) -> RecoveryOutcome:
    """Decide whether an interrupted command reached a verifiable end state.

    Called once a command's runner process has been confirmed dead without
    ever writing an exit code (typically because it — or Nexus itself — was
    killed by a restart the command triggered).
    """
    recorded = _read_recovery_state(state_dir)
    pre_commit = recorded.get("pre_commit")
    expected_services = recorded.get("expected_services")

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
    git_converged = clean and upstream_commit is not None and current == upstream_commit

    if not git_converged:
        return RecoveryOutcome(
            "failed",
            f"Interrupted worker left no exit code; the repository moved from "
            f"{pre_commit} to {current} but did not reach a clean working tree "
            f"matching its upstream branch (clean={clean}, "
            f"upstream={upstream_commit or 'none'}) — treating the operation as "
            "failed rather than assuming success.",
        )

    # Git converged. That proves a git-update step completed, but not that a
    # multi-step command (e.g. refresh: pull, then restart services) finished
    # — so success requires the command to have declared what "complete"
    # means for it via recovery_services.
    if expected_services is None:
        return RecoveryOutcome(
            "unknown",
            "Interrupted worker left no exit code; the repository advanced "
            f"from {pre_commit} to {current}, matches its upstream branch, "
            "and the working tree is clean, but this command did not "
            "declare recovery_services, so git convergence alone cannot "
            "prove the full command completed (e.g. a restart step after "
            "the pull may not have run) — reporting unknown rather than "
            "assuming success.",
        )

    if not expected_services:
        return RecoveryOutcome(
            "succeeded",
            "Interrupted worker left no exit code, but the repository "
            f"advanced from {pre_commit} to {current}, matches its "
            "upstream branch, and the working tree is clean; this command "
            "declared recovery_services: none, i.e. git convergence is its "
            "complete expected state — the operation is verified to have "
            "completed.",
        )

    services_active = service_checker(expected_services)
    services_label = ", ".join(expected_services)
    if services_active is None:
        return RecoveryOutcome(
            "unknown",
            "Interrupted worker left no exit code; git converged as "
            f"expected, but the state of its declared recovery_services "
            f"({services_label}) could not be determined — reporting "
            "unknown rather than assuming success.",
        )
    if services_active:
        return RecoveryOutcome(
            "succeeded",
            "Interrupted worker left no exit code, but the repository "
            f"advanced from {pre_commit} to {current}, matches its "
            f"upstream branch, the working tree is clean, and all declared "
            f"recovery_services ({services_label}) are active — the "
            "operation is verified to have completed.",
        )
    return RecoveryOutcome(
        "failed",
        f"Interrupted worker left no exit code; git converged from "
        f"{pre_commit} to {current} matching its upstream branch, but at "
        f"least one declared recovery_service ({services_label}) is not "
        "active — the git update completed but the rest of the operation "
        "did not, so this is treated as a failure rather than a success.",
    )
