"""Generic restart-recovery evidence for the mailbox command channel. A ``command``
message may restart Nexus mid-run, killing the detached runner before it writes an
exit code. This module never inspects the command text: it only compares the repo's
git state before/after, plus the optionally declared ``recovery_services``.

Git convergence alone proves only that a git-update step ran, so it is reported
``unknown`` - never ``succeeded`` - unless the message declares what "complete"
means: undeclared -> ``unknown`` (only definite git failure is ``failed``);
``recovery_services: none`` -> convergence alone is ``succeeded``; a service list ->
converged AND all listed units active, else a verified ``failed``. Anything
indistinguishable is ``unknown``, never guessed.
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

# Sentinel meaning "git convergence alone is the complete expected state" (as
# opposed to the field being absent = no declaration at all).
_NO_SERVICES_SENTINEL = "none"

ServiceChecker = Callable[[Sequence[str]], "bool | None"]


def _is_git_repo(repo_path: Path) -> bool:
    result = _git(["rev-parse", "--is-inside-work-tree"], repo_path, check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def parse_expected_services(meta: dict | None) -> list[str] | None:
    """Parse the optional ``recovery_services`` field: ``None`` when undeclared (the
    safe default), ``[]`` for ``recovery_services: none``, else the expected units."""
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
    """Best-effort check that every named systemd unit is active: True/False when
    determined, ``None`` when systemctl is unavailable (undetermined, not failed)."""
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
    """Best-effort local-only pre-run snapshot: just ``git rev-parse HEAD`` in
    *repo_path* (no network, cheap on the hot path). Returns ``{"pre_commit": None}``
    for non-git working directories so the command channel keeps working there."""
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
    """Decide whether an interrupted command (runner confirmed dead without an exit
    code, typically killed by a restart it triggered) reached a verifiable end state."""
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

    # Git converged: that proves a git-update step ran, but not that a multi-step
    # command finished - success requires a declared recovery_services contract.
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
