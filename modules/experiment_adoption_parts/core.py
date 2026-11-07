"""Constants, record types and small helpers of the experiment adoption service."""

from __future__ import annotations

import json
import sqlite3

from dataclasses import dataclass
from typing import Any

from modules.github_work_tracking import GitHubCIVerification
from modules.redaction import (
    bounded_lines,
    redact,
)


STATUS_NOT_STARTED = "not_started"
STATUS_QUEUED = "queued"
STATUS_ADOPTING = "adopting"
STATUS_ADOPTED = "adopted"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
# Everything but the merge is done: the task forbids it (#185). Not a
# needs_review, which Nexus would try again.
STATUS_READY_FOR_REVIEW = "ready_for_review"

# Adoptions run in threads of this process; a short-lived caller such as the
# initiative tick must wait for them before it exits (#358).
ADOPTION_THREAD_PREFIX = "adopt-"

_TERMINAL_STATUSES = {
    STATUS_ADOPTED, STATUS_NEEDS_REVIEW, STATUS_FAILED, STATUS_INTERRUPTED, STATUS_READY_FOR_REVIEW,
}
_ACTIVE_STATUSES = {STATUS_QUEUED, STATUS_ADOPTING}

_DEFAULT_GIT_TIMEOUT_SECONDS = 60
_DEFAULT_TARGET_BRANCH = "main"

# How often one adoption may be resumed across process restarts before it
# is handed to a human. Restarts are expected (the refresh flow restarts
# Nexus deliberately), but an adoption that keeps dying mid-pipeline is a
# real problem and must not resume forever.
_MAX_RECOVERY_ATTEMPTS = 3

# How often a *failed* adoption may be retried because its recorded
# failure turned out to be stale. Separate from the restart-recovery
# budget above: that one bounds crashes mid-pipeline, this one bounds
# retries granted on fresh external evidence. A failure that keeps coming
# back despite green CI is a real problem and must reach a human.
_MAX_STALE_RETRIES = 2

# A `needs_review` row records *why* adoption stopped. Most reasons are a
# judgement about the change itself and stay terminal, but some are an
# external condition that a later attempt can simply re-check (#171). Such
# a condition is named here, in the row, because the reason *text* cannot
# be re-evaluated - only a named blocker can be asked "do you still hold?".
REVIEW_BLOCKER_GITHUB_LIFECYCLE = "github_lifecycle_not_ready"

_DEFAULT_LOCK_PATH = "/var/lib/nexus/adoption.lock"


class ExperimentAdoptionError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class ExperimentAdoption:
    id: str | None
    experiment_id: str
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    experiment_commit_sha: str | None = None
    integrated_commit_sha: str | None = None
    target_branch: str = _DEFAULT_TARGET_BRANCH
    expected_files: list[str] | None = None
    actual_files: list[str] | None = None
    test_exit_code: int | None = None
    test_output: str | None = None
    reason: str | None = None
    local_sync_pending: bool = False
    diagnostic_worktree_path: str | None = None
    review_blocker: str | None = None
    recovery_attempts: int = 0
    stale_retry_attempts: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "experiment_id": self.experiment_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "experiment_commit_sha": self.experiment_commit_sha,
            "integrated_commit_sha": self.integrated_commit_sha,
            "target_branch": self.target_branch,
            "expected_files": list(self.expected_files or []),
            "actual_files": list(self.actual_files or []),
            "test_exit_code": self.test_exit_code,
            "test_output_lines": bounded_lines(self.test_output, max_lines=200),
            "reason": self.reason,
            "local_sync_pending": self.local_sync_pending,
            "diagnostic_worktree_path": self.diagnostic_worktree_path,
            "review_blocker": self.review_blocker,
            "recovery_attempts": self.recovery_attempts,
            "stale_retry_attempts": self.stale_retry_attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class _NullExperimentLogger:
    """Absorbs log calls when no real experiment logger can be built."""

    def _noop(self, *args: Any, **kwargs: Any) -> None:
        return None

    debug = info = warning = error = _noop


@dataclass(frozen=True, slots=True)
class AuthoritativeCI:
    """GitHub CI evidence together with whether it verifies the exact commit."""

    verification: GitHubCIVerification
    checkpoint_sha: str
    exact: bool

    @property
    def passed(self) -> bool:
        """Green CI that verifies the exact checkpoint - the only kind that counts."""
        return bool(self.exact and self.verification.status == "passed")


@dataclass(frozen=True, slots=True)
class StaleFailureAssessment:
    """Whether a persisted adoption failure still reflects reality (#136)."""

    retryable: bool
    category: str
    detail: str
    previous_reason: str | None = None
    evidence_commit_sha: str | None = None
    evidence_run_id: int | None = None

    def __bool__(self) -> bool:
        return self.retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "retryable": self.retryable,
            "category": self.category,
            "detail": self.detail,
            "previous_reason": self.previous_reason,
            "evidence_commit_sha": self.evidence_commit_sha,
            "evidence_run_id": self.evidence_run_id,
        }


def _json_or_none(value: str | None) -> list[str] | None:
    if not value:
        return None
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, list):
        return None
    return [str(item) for item in loaded]


def _row_to_adoption(row: sqlite3.Row | None, experiment_id: str) -> ExperimentAdoption:
    if row is None:
        return ExperimentAdoption(
            id=None,
            experiment_id=experiment_id,
            status=STATUS_NOT_STARTED,
            target_branch=_DEFAULT_TARGET_BRANCH,
        )
    return ExperimentAdoption(
        id=row["id"],
        experiment_id=row["experiment_id"],
        status=row["status"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        experiment_commit_sha=row["experiment_commit_sha"],
        integrated_commit_sha=row["integrated_commit_sha"],
        target_branch=row["target_branch"] or _DEFAULT_TARGET_BRANCH,
        expected_files=_json_or_none(row["expected_files_json"]),
        actual_files=_json_or_none(row["actual_files_json"]),
        test_exit_code=row["test_exit_code"],
        test_output=row["test_output"],
        reason=row["reason"],
        local_sync_pending=bool(row["local_sync_pending"]),
        diagnostic_worktree_path=row["diagnostic_worktree_path"],
        review_blocker=(
            row["review_blocker"] if "review_blocker" in row.keys() else None
        ),
        recovery_attempts=(
            int(row["recovery_attempts"] or 0) if "recovery_attempts" in row.keys() else 0
        ),
        stale_retry_attempts=(
            int(row["stale_retry_attempts"] or 0) if "stale_retry_attempts" in row.keys() else 0
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _normalize_path(path: str) -> str:
    normalized = (path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _collapse_summary(raw: str | None, max_chars: int = 120) -> str:
    if not raw:
        return "experiment"
    text = " ".join(redact(raw).split())
    return text[:max_chars] if text else "experiment"


def _safe_list(values: list[str] | None, *, limit: int = 100) -> list[str]:
    result: list[str] = []
    for value in values or []:
        item = _normalize_path(str(value))[:200]
        if item and item not in result:
            result.append(item)
    return result[:limit]
