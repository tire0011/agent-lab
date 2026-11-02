from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modules.experiment_evaluation import ChangedFiles, evaluate_implementation, parse_git_status_short
from modules.experiment_service import (
    STATUS_SUCCEEDED,
    _TEST_COMMANDS,
    _DEFAULT_TEST_COMMAND_KEY,
    _run_git,
    _truncate_text,
    _utcnow_iso,
)
from modules.github_work_tracking import (
    LIFECYCLE_READINESS_MESSAGE,
    GitHubCIVerification,
    GitHubWorkTrackingError,
    MergeForbiddenError,
    GitHubWorkTrackingService,
)
from modules.mailbox import MailboxLock
from modules.redaction import bounded_lines, redact, sanitized_reason
from modules.run_store import DEFAULT_DB_PATH, init_db
from modules.test_run_log import global_heartbeat_bridge
from modules.verification_runner import run_verification_test_monitored

logger = logging.getLogger(__name__)

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


class ExperimentAdoptionService:
    """Persisted, exactly-once experiment adoption state machine.

    Adoption publishes only through the mandatory GitHub lifecycle: append to
    the Issue-linked branch, open/reuse its PR, mark the verified Draft ready
    and merge it. Direct pushes to main are not allowed.
    """

    def __init__(
        self,
        *,
        experiment_service,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        worktrees_root: str | None = None,
        lock_path: str = _DEFAULT_LOCK_PATH,
        github_work_tracking: GitHubWorkTrackingService | None = None,
        reviewer: Any | None = None,
    ) -> None:
        self.experiment_service = experiment_service
        # Reviews the change before the merge (#396); None merges unreviewed.
        self.reviewer = reviewer
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.worktrees_root = Path(worktrees_root or getattr(experiment_service, "worktrees_root", str(Path.home() / "agent-worktrees")))
        self.lock_path = Path(lock_path)
        self.github_work_tracking = github_work_tracking or GitHubWorkTrackingService(
            db_path=db_path,
            repo_root=str(self.repo_root),
        )
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn

    def close(self) -> None:
        # Adoption pipelines run in daemon threads and use both the shared SQLite
        # connection and temporary worktrees. Closing/removing their resources
        # while a thread is still active can corrupt sqlite state or crash the
        # interpreter. Join all known workers before closing the connection.
        for thread in list(self._threads.values()):
            # An adoption thread may be inside a local verification run, which
            # the progress watchdog bounds by the absolute safety cap rather
            # than by a five-minute timeout - so wait for that, not for a
            # value the run can legitimately exceed.
            thread.join(
                timeout=self.experiment_service._get_test_watchdog_config().safety_cap_seconds
                + _DEFAULT_GIT_TIMEOUT_SECONDS
            )
        self._conn.close()

    def get(self, experiment_id: str) -> ExperimentAdoption:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiment_adoptions WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return _row_to_adoption(row, experiment_id)

    def wait_for_thread(self, experiment_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(experiment_id)
        if thread is not None:
            thread.join(timeout)

    def recover_after_restart(self) -> list[str]:
        """Resume adoptions that a process restart interrupted mid-flight.

        A restart is a resumable boundary, not a failure: Nexus restarts
        itself as a normal part of the refresh/deploy flow, so an adoption
        that was still ``queued``/``adopting`` when the process died must
        be reconstructed from durable evidence rather than declared
        ``interrupted`` (which the finalization coordinator then turned
        into a misleading ``needs_review``).

        For each interrupted adoption, in order of how much evidence it
        takes to be sure:

        1. **GitHub already shows the pull request merged** - the work
           really finished and only the bookkeeping was lost. Record it as
           ``adopted`` with the real merge commit; never merge again.
        2. **Otherwise resume** the *same* adoption record (same adoption
           id, experiment, issue, PR and branch) by re-running the
           pipeline, which re-derives its state from git.
        3. **Evidence is unavailable or the attempt budget is spent** ->
           ``needs_review`` naming the actual obstacle.

        Returns the experiment ids it acted on.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiment_adoptions WHERE status IN (?, ?) "
                "ORDER BY created_at ASC, id ASC",
                (STATUS_QUEUED, STATUS_ADOPTING),
            ).fetchall()

        handled: list[str] = []
        resumed_one = False
        for row in rows:
            experiment_id = row["experiment_id"]
            handled.append(experiment_id)
            try:
                # Adoptions are serialized by design (start_adoption refuses
                # a second one), so finding several interrupted at once is
                # already an inconsistency. Recording an already-merged one
                # is safe for any number of them, but only the oldest
                # remaining record is actually resumed: running two
                # pipelines would just have them fight over the adoption
                # lock and fail each other.
                resumed = self._recover_one(row, allow_resume=not resumed_one)
                resumed_one = resumed_one or resumed
            except Exception:  # noqa: BLE001 - one bad record must not block the rest
                logger.exception("adoption recovery failed for %s", experiment_id)
                self._set_status(
                    experiment_id,
                    STATUS_NEEDS_REVIEW,
                    ended_at=_utcnow_iso(),
                    reason=sanitized_reason(
                        "recovery failed: the adoption state could not be reconstructed "
                        "after a process restart"
                    ),
                    updated_at=_utcnow_iso(),
                )
        return handled

    def _recover_one(self, row: sqlite3.Row, *, allow_resume: bool = True) -> bool:
        """Recover one interrupted adoption. Returns whether it was resumed."""
        experiment_id = row["experiment_id"]
        adoption_id = row["id"]

        merged_sha = self._merged_sha_for(experiment_id)
        if merged_sha:
            # The merge landed before the restart; finishing it again would
            # be a duplicate. Persist what actually happened instead.
            self._set_status(
                experiment_id,
                STATUS_ADOPTED,
                ended_at=_utcnow_iso(),
                integrated_commit_sha=merged_sha,
                reason=sanitized_reason(
                    "recovered after restart: the pull request was already merged"
                ),
                local_sync_pending=1,
                updated_at=_utcnow_iso(),
            )
            return False

        merge_refusal = self._merge_refusal_for(experiment_id)
        if merge_refusal is not None:
            self._set_status(
                experiment_id,
                STATUS_READY_FOR_REVIEW,
                ended_at=_utcnow_iso(),
                reason=sanitized_reason(merge_refusal),
                updated_at=_utcnow_iso(),
            )
            return False

        blocker = self._recovery_blocker(experiment_id)
        if blocker is not None:
            self._set_status(
                experiment_id,
                STATUS_NEEDS_REVIEW,
                ended_at=_utcnow_iso(),
                reason=sanitized_reason(
                    f"adoption cannot be resumed after the restart: {blocker}"
                ),
                updated_at=_utcnow_iso(),
            )
            return False

        attempts = int(row["recovery_attempts"] or 0) if "recovery_attempts" in row.keys() else 0
        if attempts >= _MAX_RECOVERY_ATTEMPTS:
            self._set_status(
                experiment_id,
                STATUS_NEEDS_REVIEW,
                ended_at=_utcnow_iso(),
                reason=sanitized_reason(
                    f"adoption did not complete after {attempts} restart "
                    "recovery attempts; manual review required"
                ),
                updated_at=_utcnow_iso(),
            )
            return False

        if not allow_resume:
            self._set_status(
                experiment_id,
                STATUS_NEEDS_REVIEW,
                ended_at=_utcnow_iso(),
                reason=sanitized_reason(
                    "another interrupted adoption is already being resumed; "
                    "concurrent adoptions are not supported and this one needs review"
                ),
                updated_at=_utcnow_iso(),
            )
            return False

        self._set_status(
            experiment_id,
            STATUS_QUEUED,
            started_at=None,
            ended_at=None,
            reason=sanitized_reason(
                "resuming adoption after a process restart "
                f"(attempt {attempts + 1} of {_MAX_RECOVERY_ATTEMPTS})"
            ),
            recovery_attempts=attempts + 1,
            updated_at=_utcnow_iso(),
        )
        thread = threading.Thread(
            target=self._run_thread,
            args=(adoption_id, experiment_id),
            kwargs={"resume": True},
            daemon=True,
            name=f"{ADOPTION_THREAD_PREFIX}resume-{experiment_id[:8]}",
        )
        self._threads[experiment_id] = thread
        thread.start()
        return True

    def _recovery_blocker(self, experiment_id: str) -> str | None:
        try:
            experiment = self.experiment_service.get_experiment(experiment_id)
        except Exception:
            return "the experiment record is no longer available"
        worktree_path = getattr(experiment, "worktree_path", "") or ""
        if not worktree_path or not os.path.isdir(worktree_path):
            return "the experiment worktree is no longer available"
        return None

    def _merge_refusal_for(self, experiment_id: str) -> str | None:
        """The no-merge constraint on an experiment's existing pull request."""
        work_ref = self.github_work_tracking.for_experiment(experiment_id)
        if work_ref is None or work_ref.pr_number is None:
            return None
        return self.github_work_tracking.merge_refusal(work_ref)

    def _merged_sha_for(self, experiment_id: str) -> str | None:
        try:
            work_ref = self.github_work_tracking.for_experiment(experiment_id)
            if work_ref is None:
                return None
            return self.github_work_tracking.merged_commit_sha(work_ref)
        except Exception:
            logger.warning(
                "could not determine merge state for %s during recovery", experiment_id
            )
            return None

    def _elog(self, experiment_id: str):
        """An ExperimentLogger for this experiment, or a no-op stand-in.

        Diagnostics must never be able to break the lifecycle they
        describe, and the logger factory can legitimately refuse (an id
        that is not path-safe, a log directory that cannot be created), so
        a failure here degrades to silence rather than propagating.
        """
        try:
            return self.experiment_service.get_experiment_logger(experiment_id)
        except Exception:  # noqa: BLE001 - logging is never worth a lifecycle failure
            logger.warning("no experiment logger for %s", experiment_id, exc_info=True)
            return _NullExperimentLogger()

    def _integration_base_sha(self) -> str:
        """The target-branch commit adoption would cherry-pick onto.

        Read from the local ``origin/<target>`` ref rather than from
        GitHub, because this has to work in a process with no
        credentials - and because it is the base adoption actually uses.
        Returns an empty string when it cannot be resolved, which callers
        treat as "cannot prove the base is unchanged".
        """
        proc = _run_git(
            ["rev-parse", f"origin/{_DEFAULT_TARGET_BRANCH}"],
            cwd=str(self.repo_root),
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            logger.warning(
                "could not resolve the integration base for %s", _DEFAULT_TARGET_BRANCH
            )
            return ""
        return (proc.stdout or "").strip()

    def _exact_commit_ci(
        self, experiment_id: str, *, phase: str = "adoption", elog: Any = None
    ) -> AuthoritativeCI:
        """Authoritative GitHub CI for the exact verified checkpoint (#134).

        The single definition of "authoritative", shared by the adoption
        pipeline and the stale-failure assessment: CI evidence only counts
        when it verifies the very commit adoption is about to integrate.
        ``wait_for_ci`` already refuses to report a result whose base has
        moved; the SHA comparison here rejects a result reported for some
        other commit of the same experiment.

        A verdict already accepted for that exact commit is read from the
        durable store first and is sufficient on its own (Issue #138).
        Re-deriving it needs live credentials, so a restart into a context
        without a token used to make accepted evidence vanish and left the
        #136 retry stuck at ``no_ci_evidence`` - the lifecycle is only
        restart-safe if the fact outlives the process that observed it.

        Precedence is therefore: **the most recent authoritative
        observation for the checkpoint SHA, taken on the base we are about
        to integrate onto, wins.** A live lookup runs only when no such
        verdict is stored, and whatever it finds is written through, so a
        re-run that flips the conclusion supersedes the old record. An
        implementation replaced through the normal lifecycle gets a new
        checkpoint SHA, which has no record and so goes live.

        The base is part of the question, not a detail: ``wait_for_ci``
        refuses to call CI authoritative once the target branch has moved
        past the checkpoint's parent, because cherry-picking the same
        checkpoint onto a different base produces a tree CI never saw
        (#134). Reading a stored verdict has to honour that too, so the
        base this adoption will actually integrate onto is resolved from
        the local ``origin/<target>`` ref - no credentials needed - and a
        verdict recorded against any other base is not reused.
        """
        elog = elog or self._elog(experiment_id)
        work_ref = self.github_work_tracking.for_experiment(experiment_id)
        checkpoint_sha = (work_ref.checkpoint_sha or "").strip() if work_ref is not None else ""
        integration_base = self._integration_base_sha()

        stored = (
            self.github_work_tracking.recorded_ci_details(checkpoint_sha)
            if checkpoint_sha
            else None
        )
        elog.info(
            phase,
            "ci_evidence_lookup",
            "Looking up authoritative CI evidence for the verified checkpoint",
            checkpoint_sha=checkpoint_sha or None,
            integration_base_sha=integration_base or None,
            persisted_found=stored is not None,
        )

        recorded = None
        if checkpoint_sha and integration_base:
            recorded = self.github_work_tracking.recorded_ci_for(
                checkpoint_sha, base_sha=integration_base
            )

        if recorded is not None:
            verification = recorded
            evidence_source = "persisted"
            if recorded.status == "passed":
                # `ci_evidence_reused` means an accepted, usable verdict.
                # A red one is loaded the same way but supports nothing, so
                # it is reported below as a rejection instead - one piece of
                # evidence must never be logged as both accepted and
                # rejected in the same pass.
                elog.info(
                    phase,
                    "ci_evidence_reused",
                    "Reusing persisted authoritative CI evidence; no GitHub lookup needed",
                    source="persisted",
                    checkpoint_sha=checkpoint_sha,
                    ci_status=recorded.status,
                    conclusion=recorded.conclusion,
                    run_id=recorded.run_id,
                    provider=(stored or {}).get("provider"),
                    workflow=(stored or {}).get("workflow"),
                    observed_at=(stored or {}).get("observed_at"),
                    recorded_base_sha=(stored or {}).get("base_sha"),
                    base_validated=True,
                )
        else:
            evidence_source = "live"
            # Why the store could not answer decides what the operator has
            # to fix, so name it rather than collapsing every case into
            # "no evidence".
            if stored is None:
                fallback_reason = "no_persisted_evidence"
            elif not integration_base:
                fallback_reason = "incomplete_base_evidence"
            elif not stored.get("base_sha"):
                fallback_reason = "incomplete_base_evidence"
            else:
                fallback_reason = "target_base_moved"

            if stored is not None:
                elog.warning(
                    phase,
                    "ci_evidence_rejected",
                    "Persisted CI evidence exists but is not authoritative for this integration",
                    rejection_reason=fallback_reason,
                    checkpoint_sha=checkpoint_sha,
                    ci_status=stored.get("status"),
                    run_id=stored.get("run_id"),
                    recorded_base_sha=stored.get("base_sha"),
                    integration_base_sha=integration_base or None,
                )

            elog.info(
                phase,
                "ci_evidence_live_fallback",
                "Persisted CI evidence unusable; falling back to a live GitHub lookup",
                fallback_reason=fallback_reason,
                checkpoint_sha=checkpoint_sha or None,
            )
            verification = self.github_work_tracking.wait_for_ci(
                experiment_id,
                start_timeout_seconds=0,
                completion_timeout_seconds=0,
                poll_interval_seconds=0.01,
            )
            # `reason` is where "GitHub CI identity unavailable: configure
            # GITHUB_TOKEN/GH_TOKEN" surfaces. Logging it here is what makes
            # a credential problem readable as itself instead of arriving
            # later disguised as a generic adoption failure (#141).
            level = "info" if verification.status == "passed" else "warning"
            getattr(elog, level)(
                phase,
                "ci_evidence_live_result",
                "Live GitHub CI lookup finished",
                source="live",
                ci_status=verification.status,
                commit_sha=verification.commit_sha or None,
                conclusion=verification.conclusion,
                run_id=verification.run_id,
                reason=verification.reason,
                credentials_available=bool(
                    getattr(self.github_work_tracking, "configured", True)
                ),
            )

        exact = bool(checkpoint_sha and verification.commit_sha == checkpoint_sha)
        if not exact and verification.status in {"passed", "failed"}:
            # An authoritative verdict that belongs to a different commit is
            # a distinct situation from having none: the run exists, it just
            # does not verify the tree adoption is about to integrate.
            elog.warning(
                phase,
                "ci_evidence_rejected",
                "CI result does not verify the exact checkpoint and cannot be used",
                rejection_reason="checkpoint_mismatch",
                source=evidence_source,
                checkpoint_sha=checkpoint_sha or None,
                commit_sha=verification.commit_sha or None,
                ci_status=verification.status,
                run_id=verification.run_id,
            )
        elif exact and verification.status == "failed":
            # CI verified this exact tree and rejected it. Logged here, at
            # the one place that resolves a verdict, so it reads the same
            # whether the row came from the store or from a live lookup -
            # and so no consumer has to repeat it.
            elog.warning(
                phase,
                "ci_evidence_rejected",
                "Authoritative CI verified this exact checkpoint and failed it",
                rejection_reason="red_verdict",
                source=evidence_source,
                checkpoint_sha=checkpoint_sha,
                ci_status=verification.status,
                conclusion=verification.conclusion,
                run_id=verification.run_id,
                observed_at=(stored or {}).get("observed_at"),
            )

        return AuthoritativeCI(
            verification=verification,
            checkpoint_sha=checkpoint_sha,
            exact=exact,
        )

    def assess_failure(self, experiment_id: str) -> StaleFailureAssessment:
        """Decide whether a persisted adoption failure still stands.

        A ``needs_review`` row is assessed too (#171), by
        :meth:`_assess_needs_review`: it is only retryable when the row
        names a re-evaluable blocker and that blocker demonstrably no
        longer applies. Everything below is about ``failed``/``interrupted``.

        A ``failed`` row is not automatically an immutable fact about the
        implementation (Issue #136). It can also record that the *local*
        run never reached a verdict - it timed out, the worktree could not
        be prepared, git failed - or that a gate has since been removed.
        The concrete case: adoption re-ran the full local suite, that run
        timed out after ~11 minutes, and #134 then removed the local suite
        as a mandatory gate in favour of exact-commit GitHub CI. Without
        this assessment the obsolete row blocks every future retry forever
        and the lifecycle fix cannot reach the experiments it was written
        for.

        The rule is deliberately narrow and evidence-based, never a
        keyword match on the German failure text:

        * ``test_exit_code`` set to a non-zero value is a *verdict* about
          the change - the tests ran and rejected it. That stays terminal
          no matter how much other evidence appears; only a repair may
          clear it. (The #134 branch that honours failing authoritative CI
          records exit code 1 for exactly this reason.)
        * ``test_exit_code`` unset means no verdict was ever reached. Such
          a failure is superseded only when authoritative CI for the exact
          verified checkpoint is green *now* - the same evidence adoption
          itself would accept.

        So a genuinely broken implementation is never retried: its CI is
        red or missing, and the assessment refuses.

        The outcome is logged once, here, as ``stale_adoption_assessment``
        (Issue #141): the evidence events that led to it come from
        :meth:`_exact_commit_ci`, and repeating the verdict at every
        return path inside would just be the same decision logged six
        times.
        """
        elog = self._elog(experiment_id)
        assessment = self._assess_failure(experiment_id, elog)
        level = "info" if assessment.retryable else "warning"
        getattr(elog, level)(
            "assessment",
            "stale_adoption_assessment",
            "Assessed whether the recorded adoption failure still stands",
            retryable=assessment.retryable,
            category=assessment.category,
            detail=assessment.detail,
            previous_reason=assessment.previous_reason,
            evidence_commit_sha=assessment.evidence_commit_sha,
            evidence_run_id=assessment.evidence_run_id,
            stale_retry_attempts=self.get(experiment_id).stale_retry_attempts,
            max_stale_retries=_MAX_STALE_RETRIES,
        )
        return assessment

    def _assess_failure(self, experiment_id: str, elog: Any) -> StaleFailureAssessment:
        """The decision itself; :meth:`assess_failure` logs its outcome."""
        record = self.get(experiment_id)
        if record.status not in {STATUS_FAILED, STATUS_INTERRUPTED, STATUS_NEEDS_REVIEW}:
            return StaleFailureAssessment(
                False, "not_failed", f"adoption status is {record.status}"
            )

        previous_reason = record.reason
        if record.test_exit_code is not None and record.test_exit_code != 0:
            return StaleFailureAssessment(
                False,
                "test_verdict",
                "the recorded failure is a test verdict about the change "
                f"(exit code {record.test_exit_code}) and stays terminal",
                previous_reason=previous_reason,
            )

        if record.stale_retry_attempts >= _MAX_STALE_RETRIES:
            return StaleFailureAssessment(
                False,
                "budget_exhausted",
                f"the adoption was already retried {record.stale_retry_attempts} time(s) "
                "on fresh evidence and kept failing; manual review required",
                previous_reason=previous_reason,
            )

        if record.status == STATUS_NEEDS_REVIEW:
            return self._assess_needs_review(record)

        try:
            evidence = self._exact_commit_ci(
                experiment_id, phase="assessment", elog=elog
            )
        except Exception as exc:  # noqa: BLE001 - missing evidence must not raise into the caller
            logger.warning(
                "could not read authoritative CI evidence for %s", experiment_id, exc_info=True
            )
            elog.error(
                "assessment",
                "ci_evidence_lookup_failed",
                "Authoritative CI evidence could not be read at all",
                exc=exc,
            )
            return StaleFailureAssessment(
                False,
                "evidence_unavailable",
                "authoritative CI evidence could not be read",
                previous_reason=previous_reason,
            )

        ci = evidence.verification
        if not evidence.passed:
            # Why the evidence does not support a retry has already been
            # named by `_exact_commit_ci` (red_verdict, target_base_moved,
            # checkpoint_mismatch, ...); repeating it here would log one
            # decision twice.
            return StaleFailureAssessment(
                False,
                "no_ci_evidence",
                "no successful authoritative CI for the exact verified checkpoint "
                f"(status={ci.status}; reason={ci.reason or 'none'})",
                previous_reason=previous_reason,
            )

        return StaleFailureAssessment(
            True,
            "superseded_local_verification",
            "the recorded failure never produced a test verdict and authoritative "
            f"GitHub Actions passed for the exact checkpoint {evidence.checkpoint_sha}"
            f" (run_id={ci.run_id})",
            previous_reason=previous_reason,
            evidence_commit_sha=evidence.checkpoint_sha,
            evidence_run_id=ci.run_id,
        )

    def _assess_needs_review(self, record: ExperimentAdoption) -> StaleFailureAssessment:
        """Decide whether a persisted ``needs_review`` may be resumed (#171).

        ``needs_review`` means adoption stopped without a verdict about the
        change - but not every such stop is a request for a human. Some
        record an external condition (#115/PR #116: the GitHub lifecycle was
        unconfigured, the VM was fixed afterwards and the service
        restarted). Those are re-checkable, and re-checking is the only
        honest answer: returning the persisted text would present a stale
        configuration failure as a current readiness verdict.

        The re-check asks the *live* dependency, never the recorded text,
        and only for a blocker the row actually names. Anything else -
        a file mismatch, a denied path, a failed merge, a manual review -
        has no re-evaluable condition and stays terminal.
        """
        blocker = self._review_blocker_of(record)
        previous_reason = record.reason
        if blocker is None:
            return StaleFailureAssessment(
                False,
                "manual_review",
                "the recorded needs_review is not tied to a re-evaluable "
                "condition and stays terminal",
                previous_reason=previous_reason,
            )

        if blocker == REVIEW_BLOCKER_GITHUB_LIFECYCLE:
            try:
                self.github_work_tracking.require_lifecycle_ready()
            except Exception as exc:  # noqa: BLE001 - a blocker that still holds is not an error
                return StaleFailureAssessment(
                    False,
                    "review_blocker_persists",
                    f"the recorded needs_review blocker {blocker} still applies: {exc}",
                    previous_reason=previous_reason,
                )
            return StaleFailureAssessment(
                True,
                "review_blocker_cleared",
                f"the recorded needs_review blocker {blocker} no longer applies; "
                "the GitHub lifecycle is ready now",
                previous_reason=previous_reason,
            )

        return StaleFailureAssessment(
            False,
            "manual_review",
            f"the recorded needs_review blocker {blocker} cannot be re-evaluated",
            previous_reason=previous_reason,
        )

    @staticmethod
    def _review_blocker_of(record: ExperimentAdoption) -> str | None:
        """The row's named blocker, recognizing rows written before #171.

        Older rows - including the one this issue was filed for - predate
        the column, so the readiness refusal survives only as text. That
        one sentence is exported by the tracking service that produces it
        (``LIFECYCLE_READINESS_MESSAGE``), so recognizing it is still
        reading the producer's own marker rather than guessing keywords.
        """
        blocker = (record.review_blocker or "").strip()
        if blocker:
            return blocker
        if LIFECYCLE_READINESS_MESSAGE in (record.reason or ""):
            return REVIEW_BLOCKER_GITHUB_LIFECYCLE
        return None

    def failure_history(self, experiment_id: str) -> list[dict[str, Any]]:
        """Superseded adoption failures, oldest first.

        The adoption row itself is reused across retries, so this is where
        a replaced failure remains readable after the retry moved on.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiment_adoption_failures WHERE experiment_id=? "
                "ORDER BY superseded_at ASC, id ASC",
                (experiment_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "adoption_id": row["adoption_id"],
                "status": row["status"],
                "reason": row["reason"],
                "test_exit_code": row["test_exit_code"],
                "test_output_lines": bounded_lines(row["test_output"], max_lines=50),
                "experiment_commit_sha": row["experiment_commit_sha"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "superseded_at": row["superseded_at"],
                "superseded_by": row["superseded_by"],
            }
            for row in rows
        ]

    def _archive_failure(self, record: ExperimentAdoption, *, superseded_by: str) -> None:
        """Append the failure a retry is about to overwrite. Caller holds the lock."""
        self._conn.execute(
            """
            INSERT INTO experiment_adoption_failures (
                id, adoption_id, experiment_id, status, reason, test_exit_code,
                test_output, experiment_commit_sha, started_at, ended_at,
                superseded_at, superseded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                record.id or "",
                record.experiment_id,
                record.status,
                record.reason,
                record.test_exit_code,
                record.test_output,
                record.experiment_commit_sha,
                record.started_at,
                record.ended_at,
                _utcnow_iso(),
                superseded_by[:500],
            ),
        )

    def start_adoption(
        self,
        experiment_id: str,
        *,
        superseded: StaleFailureAssessment | None = None,
    ) -> ExperimentAdoption:
        """Queue adoption for a verified experiment.

        Restarting a *failed* adoption reuses the same row - there is one
        per experiment - which means the retry would silently overwrite
        the failure that preceded it. So the replaced failure is always
        appended to the durable attempt history first, in the transaction
        that queues the retry (Issue #136).

        Whether a failed adoption *should* be restarted is the caller's
        policy decision, not this method's: the finalization coordinator
        consults :meth:`assess_failure` before reopening an exhausted
        lifecycle, while an operator calling the adopt endpoint has
        already decided.

        ``superseded`` is that assessment, passed back by a caller that
        restarted *because* current evidence overruled the recorded
        failure. Only such a restart consumes ``stale_retry_attempts``:
        that budget bounds how often changed evidence may reopen the
        lifecycle, so ordinary retries - a finalization retry still inside
        its own budget, an operator pressing adopt - must not spend it.
        Otherwise two ordinary retries would exhaust the evidence budget
        before any evidence had changed, and #136's whole point (changed
        evidence reopens an exhausted lifecycle) would be unreachable.
        """
        existing = self.get(experiment_id)
        # A `needs_review` row is reopened only by a caller that brings an
        # assessment saying its blocker has cleared (#171). Without one it
        # stays exactly as terminal as before - an operator pressing adopt
        # on a review row still gets the record back untouched.
        reopening_review = existing.status == STATUS_NEEDS_REVIEW and bool(
            superseded is not None and superseded.retryable
        )
        if existing.status in {STATUS_ADOPTED, STATUS_QUEUED, STATUS_ADOPTING} or (
            existing.status == STATUS_NEEDS_REVIEW and not reopening_review
        ):
            return existing
        if existing.status == STATUS_READY_FOR_REVIEW:
            # Asking again changes nothing, but the refusal is recorded (#185).
            work_ref = self.github_work_tracking.for_experiment(experiment_id)
            if work_ref is not None:
                self.github_work_tracking.refuse_merge(work_ref)
            return existing

        restarting_failure = (
            existing.status in {STATUS_FAILED, STATUS_INTERRUPTED} or reopening_review
        )
        on_fresh_evidence = restarting_failure and bool(
            superseded is not None and superseded.retryable
        )

        other = self._find_active_other(experiment_id)
        if other is not None:
            raise ExperimentAdoptionError("Eine andere Übernahme läuft bereits.")
        if not self._lock_probe():
            raise ExperimentAdoptionError("Eine andere Übernahme läuft bereits.")

        validation = self._validate_startable(experiment_id, persist_review=True)
        now = _utcnow_iso()
        adoption_id = existing.id or uuid.uuid4().hex

        retry_reason: str | None = None
        # A fresh start (nothing failed yet) resets the budget; an ordinary
        # retry carries it unchanged; only an evidence-granted retry spends
        # one unit of it.
        stale_retry_attempts = existing.stale_retry_attempts if restarting_failure else 0
        if restarting_failure:
            retry_reason = (
                f"retrying a previously {existing.status} adoption; the superseded "
                f"failure was: {existing.reason or 'unknown'}"
            )
        if on_fresh_evidence:
            stale_retry_attempts += 1
            retry_reason = (
                f"retrying a superseded {existing.status} adoption on fresh evidence "
                f"({superseded.detail}); the superseded failure was: "
                f"{existing.reason or 'unknown'}"
            )

        # One transaction: the previous failure is archived, the row is
        # reset to `queued` and the attempt is counted before any worker
        # exists to observe an in-between state. A crash between the two
        # writes could otherwise either lose the failure history or hand
        # out an unbounded retry.
        with self._lock, self._conn:
            if restarting_failure:
                self._archive_failure(existing, superseded_by=retry_reason or "retry")
            self._conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, started_at, ended_at,
                    experiment_commit_sha, integrated_commit_sha, target_branch,
                    expected_files_json, actual_files_json, test_exit_code,
                    test_output, reason, local_sync_pending,
                    diagnostic_worktree_path, recovery_attempts,
                    stale_retry_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, NULL, NULL, ?, 0, NULL, 0, ?, ?, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET
                    status=excluded.status,
                    started_at=NULL,
                    ended_at=NULL,
                    experiment_commit_sha=NULL,
                    integrated_commit_sha=NULL,
                    target_branch=excluded.target_branch,
                    expected_files_json=excluded.expected_files_json,
                    actual_files_json=excluded.actual_files_json,
                    test_exit_code=NULL,
                    test_output=NULL,
                    reason=excluded.reason,
                    local_sync_pending=0,
                    diagnostic_worktree_path=NULL,
                    review_blocker=NULL,
                    recovery_attempts=0,
                    stale_retry_attempts=excluded.stale_retry_attempts,
                    updated_at=excluded.updated_at
                """,
                (
                    adoption_id,
                    experiment_id,
                    STATUS_QUEUED,
                    _DEFAULT_TARGET_BRANCH,
                    json.dumps(validation["expected_files"]),
                    json.dumps(validation["actual_files"]),
                    sanitized_reason(retry_reason),
                    stale_retry_attempts,
                    now,
                    now,
                ),
            )
        if restarting_failure:
            logger.info(
                "restarting adoption %s for %s (%s; evidence-granted retries used: %d); "
                "superseded failure: %s",
                adoption_id,
                experiment_id,
                "on fresh evidence" if on_fresh_evidence else "ordinary retry",
                stale_retry_attempts,
                existing.reason or "unknown",
            )
            self._elog(experiment_id).info(
                "adoption",
                "stale_adoption_requeued",
                "Requeued the existing adoption record after its recorded failure",
                adoption_id=adoption_id,
                previous_status=existing.status,
                previous_reason=existing.reason,
                on_fresh_evidence=on_fresh_evidence,
                evidence_category=(
                    superseded.category if superseded is not None else None
                ),
                stale_retry_attempts=stale_retry_attempts,
                max_stale_retries=_MAX_STALE_RETRIES,
            )
        thread = threading.Thread(
            target=self._run_thread,
            args=(adoption_id, experiment_id),
            daemon=True,
            name=f"{ADOPTION_THREAD_PREFIX}{experiment_id[:8]}",
        )
        self._threads[experiment_id] = thread
        thread.start()
        return self.get(experiment_id)

    def _run_thread(self, adoption_id: str, experiment_id: str, *, resume: bool = False) -> None:
        try:
            self._run_adoption_pipeline(adoption_id, experiment_id, resume=resume)
        except Exception:
            logger.exception("experiment adoption %s failed unexpectedly", experiment_id)
            try:
                self._set_status(
                    experiment_id,
                    STATUS_FAILED,
                    ended_at=_utcnow_iso(),
                    reason=sanitized_reason("unexpected adoption failure"),
                )
            except Exception:
                logger.exception("failed to persist unexpected adoption error for %s", experiment_id)

    def _find_active_other(self, experiment_id: str) -> ExperimentAdoption | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiment_adoptions WHERE experiment_id != ? AND status IN (?, ?) LIMIT 1",
                (experiment_id, STATUS_QUEUED, STATUS_ADOPTING),
            ).fetchone()
        return _row_to_adoption(row, row["experiment_id"]) if row is not None else None

    def _lock_probe(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = MailboxLock(self.lock_path)
        try:
            return lock.acquire()
        finally:
            lock.release()

    def _update_review_state(self, experiment_id: str, reason: str, *, actual_files: list[str], expected_files: list[str]) -> ExperimentAdoption:
        now = _utcnow_iso()
        adoption_id = self.get(experiment_id).id or uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, started_at, ended_at,
                    experiment_commit_sha, integrated_commit_sha, target_branch,
                    expected_files_json, actual_files_json, test_exit_code,
                    test_output, reason, local_sync_pending,
                    diagnostic_worktree_path, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?, NULL, NULL, ?, 0, NULL, ?, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET
                    status=excluded.status,
                    ended_at=excluded.ended_at,
                    expected_files_json=excluded.expected_files_json,
                    actual_files_json=excluded.actual_files_json,
                    reason=excluded.reason,
                    -- This review is about the worktree, not about any
                    -- external condition, so an earlier row's blocker must
                    -- not survive to authorize a retry of *this* reason.
                    review_blocker=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    adoption_id,
                    experiment_id,
                    STATUS_NEEDS_REVIEW,
                    now,
                    _DEFAULT_TARGET_BRANCH,
                    json.dumps(_safe_list(expected_files)),
                    json.dumps(_safe_list(actual_files)),
                    sanitized_reason(reason),
                    now,
                    now,
                ),
            )
        return self.get(experiment_id)

    def _resumable_commit_sha(self, experiment_id: str) -> str | None:
        record = self.get(experiment_id)
        recorded = (record.experiment_commit_sha or "").strip()
        if not recorded:
            return None
        try:
            experiment = self.experiment_service.get_experiment(experiment_id)
        except Exception:
            return None
        worktree_path = getattr(experiment, "worktree_path", "") or ""
        if not worktree_path or not os.path.isdir(worktree_path):
            return None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if head.returncode != 0:
            return None
        return recorded if head.stdout.strip() == recorded else None

    def _committed_verified_work(
        self, experiment_id: str, experiment: Any, worktree_path: str, elog
    ) -> tuple[str | None, list[str]]:
        """``HEAD`` and its changed paths when the verified content is already committed.

        Adoption commits the verified changes as its own first step, so any
        attempt that got that far leaves a *clean* worktree with the content in
        ``HEAD``. Deriving the file list from the uncommitted diff then yields
        nothing and the attempt dies with "Keine Änderungen zur Übernahme
        vorhanden." - for a change whose CI is green and whose content is
        sitting right there. The adoption record cannot rescue it either:
        requeuing resets ``experiment_commit_sha`` to NULL, so
        :meth:`_resumable_commit_sha` has nothing left to resume from (#175).

        The content invariant answers this without trusting any record, which
        is also why it survives a process restart: if ``HEAD`` carries exactly
        the tree verification accepted, and that tree differs from the target
        branch, then ``HEAD`` *is* the change to adopt. Returns ``(None, [])``
        when the content is not committed or there is genuinely nothing left
        to integrate, and raises when the comparison itself could not be made
        - an unanswerable question must not read as an empty answer.
        """
        checkpoint_sha = self._verified_checkpoint_sha(experiment_id, experiment)
        verified_tree = self._tree_sha_of(worktree_path, checkpoint_sha, elog, phase="adoption")
        head_sha = self._git_stdout(["rev-parse", "HEAD"], worktree_path, elog).strip()
        head_tree = self._tree_sha_of(worktree_path, head_sha, elog, phase="adoption")
        if not verified_tree or not head_tree or verified_tree != head_tree:
            return None, []
        # "Nothing to adopt" may only be concluded against a target branch
        # that actually resolved. Without that check an unreachable ref would
        # report an empty change set - reintroducing the very symptom this
        # method exists to remove, under a cause nobody would look for.
        target_ref = f"origin/{_DEFAULT_TARGET_BRANCH}"
        resolved_target = _run_git(
            ["rev-parse", "--verify", f"{target_ref}^{{commit}}"],
            cwd=worktree_path,
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
            elog=elog,
            phase="adoption",
        )
        if resolved_target.returncode != 0:
            raise ExperimentAdoptionError(
                f"Der Ziel-Branch {target_ref} ist im Experiment-Worktree nicht auflösbar."
            )
        # Three dots: what this experiment added since it diverged from the
        # target branch, not whatever else landed on the target meanwhile.
        changed = _run_git(
            ["diff", "--name-only", f"{target_ref}...HEAD"],
            cwd=worktree_path,
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
            elog=elog,
            phase="adoption",
        )
        if changed.returncode != 0:
            raise ExperimentAdoptionError(
                "Der Experiment-Commit konnte nicht mit dem Ziel-Branch verglichen werden."
            )
        changed_files = _safe_list(
            [line.strip() for line in changed.stdout.splitlines() if line.strip()]
        )
        if not changed_files:
            return None, []
        return head_sha, changed_files

    def _verified_checkpoint_sha(self, experiment_id: str, experiment: Any) -> str:
        """Return the commit whose content verification actually accepted.

        The GitHub work ref is authoritative. ``test_command`` is the fallback
        for experiments verified before a work ref existed: verification writes
        it as ``["GitHub Actions", <workflow>, <sha>]``.
        """
        try:
            work_ref = self.github_work_tracking.for_experiment(experiment_id)
        except Exception:
            work_ref = None
        sha = (getattr(work_ref, "checkpoint_sha", None) or "").strip() if work_ref is not None else ""
        if sha:
            return sha
        command = getattr(experiment, "test_command", None) or []
        if len(command) >= 3 and str(command[0]).strip() == "GitHub Actions":
            return str(command[2]).strip()
        return ""

    def _tree_sha_of(self, worktree_path: str, commit_sha: str, elog, *, phase: str) -> str:
        if not commit_sha:
            return ""
        proc = _run_git(
            ["rev-parse", f"{commit_sha}^{{tree}}"],
            cwd=worktree_path,
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
            elog=elog,
            phase=phase,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _validate_startable(self, experiment_id: str, *, persist_review: bool) -> dict[str, Any]:
        experiment = self.experiment_service.get_experiment(experiment_id)
        if experiment.status != STATUS_SUCCEEDED:
            raise ExperimentAdoptionError("Nur erfolgreiche Experimente können übernommen werden.")
        quality = experiment.quality_evaluation or {}
        if quality.get("outcome") != "pass":
            raise ExperimentAdoptionError("Das Qualitäts-Gate des Experiments ist nicht bestanden.")
        worktree_path = experiment.worktree_path or ""
        if not worktree_path or not os.path.isdir(worktree_path):
            raise ExperimentAdoptionError("Der Experiment-Worktree ist nicht mehr verfügbar.")

        elog = self.experiment_service.get_experiment_logger(experiment_id)
        snapshot = self._capture_snapshot(worktree_path, elog, phase="adoption")
        current_status = (snapshot["git_status"] or "").strip()
        stored_status = (experiment.git_status or "").strip()
        current_stat = (snapshot["diff_stat"] or "").strip()
        stored_stat = (experiment.diff_stat or "").strip()
        if current_status != stored_status or current_stat != stored_stat:
            # `git status`/`diff --stat` describe how the content is *arranged*
            # in the worktree, not what the content is. Adoption's own first
            # step commits the verified changes, which turns a dirty worktree
            # into a clean one and makes those strings differ from the ones
            # verification stored - so a resumed adoption accused itself of
            # tampering and could never converge (#147 follow-up; the real
            # blocker behind the eight identical needs_review retries).
            # The content invariant answers the question the strings only
            # approximated: is the tree adoption would integrate byte-for-byte
            # the tree CI verified?
            checkpoint_sha = self._verified_checkpoint_sha(experiment_id, experiment)
            verified_tree = self._tree_sha_of(worktree_path, checkpoint_sha, elog, phase="adoption")
            current_tree = (snapshot.get("tree_sha") or "").strip()
            if verified_tree and current_tree and verified_tree == current_tree:
                elog.info(
                    "adoption", "worktree_content_matches_checkpoint",
                    "Worktree layout changed since verification, but its content is "
                    "identical to the verified checkpoint; continuing adoption",
                    checkpoint_sha=checkpoint_sha,
                    tree_sha=current_tree,
                )
            else:
                raise ExperimentAdoptionError(
                    "Die Änderungen im Experiment-Worktree haben sich seit der erfolgreichen Prüfung verändert."
                )

        actual_files = _safe_list(snapshot["changed"].all_paths())
        # An empty uncommitted diff is not the same thing as an empty change:
        # the verified content may already be committed in the worktree. Decide
        # that here rather than in the pipeline, so a retry that cannot possibly
        # succeed is refused *before* it spends an attempt from the bounded
        # budget - which is how ten seconds exhausted the retries in #175.
        committed_commit_sha: str | None = None
        if not actual_files:
            committed_commit_sha, actual_files = self._committed_verified_work(
                experiment_id, experiment, worktree_path, elog
            )
            if committed_commit_sha is None:
                raise ExperimentAdoptionError("Keine Änderungen zur Übernahme vorhanden.")
            elog.info(
                "adoption",
                "verified_work_already_committed",
                "The worktree is clean because the verified content is already "
                "committed; adopting that commit instead of an uncommitted diff",
                experiment_commit_sha=committed_commit_sha,
                changed_files=actual_files,
            )
        expected_files = _safe_list((quality.get("changed_files") or quality.get("expected_files") or actual_files))
        review_reason = self._review_reason(actual_files, expected_files)
        if review_reason is not None:
            if persist_review:
                self._update_review_state(
                    experiment_id,
                    review_reason,
                    actual_files=actual_files,
                    expected_files=expected_files,
                )
            raise ExperimentAdoptionError(review_reason)

        return {
            "experiment": experiment,
            "actual_files": actual_files,
            "expected_files": expected_files,
            "snapshot": snapshot,
            "committed_commit_sha": committed_commit_sha,
        }

    def _review_reason(self, actual_files: list[str], expected_files: list[str]) -> str | None:
        for path in actual_files:
            reason = self._denied_path_reason(path)
            if reason is not None:
                return reason
        if expected_files:
            extras = [path for path in actual_files if path not in expected_files]
            if extras:
                return "Unerwartete Dateien im Experiment-Diff; manuelle Prüfung nötig."
        return None

    def _denied_path_reason(self, path: str) -> str | None:
        normalized = _normalize_path(path)
        lower = normalized.lower()
        if not normalized:
            return "Leere Dateipfade im Experiment-Diff sind unzulässig."
        parts = Path(normalized).parts
        if normalized.startswith("/") or ".." in parts:
            return "Dateien außerhalb des Repositories erfordern manuelle Prüfung."
        if lower.startswith(".git/") or "/.git/" in lower:
            return "Änderungen an Git-Metadaten erfordern manuelle Prüfung."
        if lower.startswith("communication/outbox/"):
            return "Änderungen unter communication/outbox/ werden nicht automatisch übernommen."
        if lower == ".env" or lower.endswith("/.env") or lower.endswith(".pem"):
            return "Sensible Konfigurationsdateien werden nicht automatisch übernommen."
        # Test files may be named after what they test (fixtures such as
        # "print_vault_secrets.md"); the reviewer reads their content.
        if ("secret" in lower or "credential" in lower) and not lower.startswith("tests/"):
            return "Potentiell sensible Dateien werden nicht automatisch übernommen."
        return None

    def _capture_snapshot(self, worktree_path: str, elog, *, phase: str) -> dict[str, Any]:
        status_proc = _run_git(
            ["status", "--short", "--untracked-files=all"],
            cwd=worktree_path,
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
            elog=elog,
            phase=phase,
        )
        scratch_dir = self.worktrees_root / f"adoption-index-{uuid.uuid4().hex}"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        index_env = dict(os.environ)
        index_env["GIT_INDEX_FILE"] = str(scratch_dir / "index")
        try:
            read_tree = _run_git(
                ["read-tree", "HEAD"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            add_proc = _run_git(
                ["add", "-A"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            if read_tree.returncode != 0 or add_proc.returncode != 0:
                failed = read_tree if read_tree.returncode != 0 else add_proc
                raise subprocess.CalledProcessError(failed.returncode, failed.args, output=failed.stdout, stderr=failed.stderr)
            stat_proc = _run_git(
                ["diff", "--cached", "--stat", "HEAD"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            diff_proc = _run_git(
                ["diff", "--cached", "HEAD"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            # The scratch index already holds HEAD plus every worktree change,
            # so its tree *is* the content adoption would integrate. Recording
            # it lets the content be compared directly against the verified
            # checkpoint, independently of whether that content currently sits
            # in the worktree as uncommitted changes or as a commit.
            tree_proc = _run_git(
                ["write-tree"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        return {
            "git_status": status_proc.stdout,
            "diff_stat": stat_proc.stdout,
            "diff_text": diff_proc.stdout,
            "changed": parse_git_status_short(status_proc.stdout),
            "tree_sha": tree_proc.stdout.strip() if tree_proc.returncode == 0 else "",
        }

    def _transition_status(self, experiment_id: str, from_status: str, to_status: str, **fields: Any) -> bool:
        set_clauses = ["status = ?"]
        values: list[Any] = [to_status]
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.extend([experiment_id, from_status])
        query = f"UPDATE experiment_adoptions SET {', '.join(set_clauses)} WHERE experiment_id = ? AND status = ?"
        with self._lock, self._conn:
            updated = self._conn.execute(query, values).rowcount
        return bool(updated)

    def _set_status(self, experiment_id: str, status: str, **fields: Any) -> None:
        set_clauses = ["status = ?"]
        values: list[Any] = [status]
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.append(experiment_id)
        query = f"UPDATE experiment_adoptions SET {', '.join(set_clauses)} WHERE experiment_id = ?"
        with self._lock, self._conn:
            self._conn.execute(query, values)

    def _run_adoption_pipeline(
        self, adoption_id: str, experiment_id: str, *, resume: bool = False
    ) -> ExperimentAdoption:
        elog = self.experiment_service.get_experiment_logger(experiment_id)
        lock = MailboxLock(self.lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not lock.acquire():
            self._set_status(
                experiment_id,
                STATUS_FAILED,
                ended_at=_utcnow_iso(),
                reason=sanitized_reason("Eine andere Übernahme läuft bereits."),
                updated_at=_utcnow_iso(),
            )
            return self.get(experiment_id)

        integration_path: Path | None = None
        keep_integration = False
        try:
            if not self._transition_status(
                experiment_id,
                STATUS_QUEUED,
                STATUS_ADOPTING,
                started_at=_utcnow_iso(),
                updated_at=_utcnow_iso(),
            ):
                return self.get(experiment_id)

            elog.info("adoption", "adoption_started", "Starting persisted experiment adoption")
            experiment_for_tracking = self.experiment_service.get_experiment(experiment_id)
            try:
                self.github_work_tracking.recover_experiment(experiment_for_tracking)
            except GitHubWorkTrackingError as exc:
                return self._finish_review(
                    experiment_id,
                    reason=f"GitHub lifecycle blocked before adoption: {exc}",
                    review_blocker=self._lifecycle_readiness_blocker(),
                    elog=elog,
                )
            fetch_proc = _run_git(
                ["fetch", "origin", _DEFAULT_TARGET_BRANCH],
                cwd=str(self.repo_root),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if fetch_proc.returncode != 0:
                return self._finish_failure(
                    experiment_id,
                    reason="git fetch origin main failed",
                    elog=elog,
                )

            resumed_commit_sha = self._resumable_commit_sha(experiment_id) if resume else None
            if resumed_commit_sha is not None:
                experiment = self.experiment_service.get_experiment(experiment_id)
                worktree_path = str(experiment.worktree_path)
                record = self.get(experiment_id)
                actual_files = _safe_list(record.actual_files or [])
                expected_files = _safe_list(record.expected_files or [])
                experiment_commit_sha = resumed_commit_sha
                commit_message = self._commit_message(
                    experiment_id, experiment.goal, experiment.plan
                )
                elog.info(
                    "adoption",
                    "adoption_resumed_from_commit",
                    "Resuming adoption from the experiment commit created before the restart",
                    experiment_commit_sha=experiment_commit_sha,
                )
            else:
                validation = self._validate_startable(experiment_id, persist_review=False)
                experiment = validation["experiment"]
                worktree_path = str(experiment.worktree_path)
                snapshot = validation["snapshot"]
                actual_files = validation["actual_files"]
                expected_files = validation["expected_files"]
                elog.debug(
                    "adoption",
                    "adoption_diff_captured",
                    "Captured experiment diff prior to commit",
                    changed_files=actual_files,
                    diff_stat=sanitized_reason(snapshot["diff_stat"], max_chars=1000),
                )
                # Validation already proved there is something to adopt, and
                # said in which shape: an uncommitted diff still to be
                # committed, or a commit an earlier attempt left behind.
                experiment_commit_sha = validation["committed_commit_sha"]
                commit_message = self._commit_message(
                    experiment_id, experiment.goal, experiment.plan
                )

            if experiment_commit_sha is None:
                _run_git(["reset", "HEAD", "--"], cwd=worktree_path, timeout=60, elog=elog, phase="adoption")
                for rel_path in actual_files:
                    add_proc = _run_git(
                        ["add", "--", rel_path],
                        cwd=worktree_path,
                        timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                        elog=elog,
                        phase="adoption",
                    )
                    if add_proc.returncode != 0:
                        return self._finish_failure(
                            experiment_id,
                            reason=f"git add failed for {Path(rel_path).name}",
                            actual_files=actual_files,
                            expected_files=expected_files,
                            elog=elog,
                        )

                commit_proc = _run_git(
                    ["commit", "--no-verify", "-m", commit_message],
                    cwd=worktree_path,
                    timeout=120,
                    elog=elog,
                    phase="adoption",
                )
                if commit_proc.returncode != 0:
                    return self._finish_failure(
                        experiment_id,
                        reason="Der Experiment-Commit konnte nicht erstellt werden.",
                        actual_files=actual_files,
                        expected_files=expected_files,
                        elog=elog,
                    )
                experiment_commit_sha = self._git_stdout(["rev-parse", "HEAD"], worktree_path, elog).strip() or None

            # Persist the source commit whichever way it was obtained. A
            # requeue resets this column, so an adoption that adopts an
            # already-committed change has to write it back - otherwise the
            # next attempt is blind again in exactly the same way.
            self._set_status(
                experiment_id,
                STATUS_ADOPTING,
                experiment_commit_sha=experiment_commit_sha,
                actual_files_json=json.dumps(_safe_list(actual_files)),
                expected_files_json=json.dumps(_safe_list(expected_files)),
                updated_at=_utcnow_iso(),
            )

            integration_path = self.worktrees_root / f"adopt-{experiment_id[:8]}-{uuid.uuid4().hex[:8]}"
            integration_path.parent.mkdir(parents=True, exist_ok=True)
            add_worktree = _run_git(
                ["worktree", "add", str(integration_path), f"origin/{_DEFAULT_TARGET_BRANCH}"],
                cwd=str(self.repo_root),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if add_worktree.returncode != 0:
                return self._finish_failure(
                    experiment_id,
                    reason="Der Integrations-Worktree konnte nicht erstellt werden.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    elog=elog,
                )

            # The whole change since the experiment branch left main, not
            # only its last commit: a retry builds on the earlier attempt's
            # commits, and picking just its own commit either conflicts or
            # lands half a change (#455, the reverted #410).
            squash = _run_git(
                ["merge", "--squash", experiment_commit_sha or "HEAD"],
                cwd=str(integration_path),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if squash.returncode != 0:
                keep_integration = True
                conflicted = self._git_stdout(
                    ["diff", "--name-only", "--diff-filter=U"], str(integration_path), elog
                ).split()
                _run_git(["reset", "--hard", "HEAD"], cwd=str(integration_path), timeout=60, elog=elog, phase="adoption")
                return self._finish_review(
                    experiment_id,
                    reason="Integrationskonflikt: " + (", ".join(conflicted[:10]) or "merge failed"),
                    actual_files=actual_files,
                    expected_files=expected_files,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )
            # Earlier attempts' files were never checked by an adoption.
            staged = self._git_stdout(["diff", "--cached", "--name-only"], str(integration_path), elog).split()
            for rel_path in staged:
                denied = self._denied_path_reason(rel_path)
                if denied is not None:
                    keep_integration = True
                    return self._finish_review(
                        experiment_id,
                        reason=denied,
                        actual_files=actual_files,
                        expected_files=expected_files,
                        diagnostic_worktree_path=str(integration_path),
                        elog=elog,
                    )

            evidence = self._exact_commit_ci(experiment_id, phase="adoption", elog=elog)
            ci = evidence.verification
            checkpoint_sha = evidence.checkpoint_sha
            exact_ci = evidence.exact
            verification_summary: str
            if evidence.passed:
                test_exit_code = 0
                test_output = (
                    "Reused authoritative GitHub Actions verification for exact checkpoint "
                    f"{checkpoint_sha}; workflow=tests.yml; run_id={ci.run_id}; conclusion={ci.conclusion}."
                )
                verification_summary = (
                    f"authoritative GitHub Actions passed for exact checkpoint `{checkpoint_sha}`"
                )
                elog.info(
                    "adoption",
                    "authoritative_ci_reused",
                    "Reusing successful exact-commit GitHub Actions verification; local full-suite fallback skipped",
                    commit_sha=checkpoint_sha,
                    run_id=ci.run_id,
                    conclusion=ci.conclusion,
                    url=ci.url,
                )
            elif ci.status == "failed" and exact_ci:
                keep_integration = True
                return self._finish_failure(
                    experiment_id,
                    reason=(
                        "Die autoritative GitHub-CI für den verifizierten Commit ist fehlgeschlagen; "
                        "ein lokaler Fallback darf dieses Ergebnis nicht überschreiben."
                    ),
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=1,
                    test_output=(
                        f"GitHub Actions exact checkpoint {checkpoint_sha}: "
                        f"conclusion={ci.conclusion}; run_id={ci.run_id}; reason={ci.reason or 'none'}"
                    ),
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )
            else:
                if ci.status == "passed" and not exact_ci:
                    elog.warning(
                        "adoption",
                        "authoritative_ci_sha_mismatch",
                        "Ignoring GitHub CI result because it does not match the persisted checkpoint SHA",
                        checkpoint_sha=checkpoint_sha,
                        ci_commit_sha=ci.commit_sha,
                    )
                else:
                    elog.info(
                        "adoption",
                        "local_tests_fallback_started",
                        "Authoritative exact-commit GitHub CI is unavailable; running local fallback tests",
                        checkpoint_sha=checkpoint_sha,
                        ci_status=ci.status,
                        reason=ci.reason,
                    )
                test_command_template = _TEST_COMMANDS[_DEFAULT_TEST_COMMAND_KEY]
                test_command = [part.format(python=self.experiment_service.test_python_executable) for part in test_command_template]
                watchdog = self.experiment_service._get_test_watchdog_config()
                expected_total = self.experiment_service._get_expected_test_total()
                test_run = run_verification_test_monitored(
                    test_command,
                    cwd=str(integration_path),
                    run_id=f"adoption-{experiment_id}",
                    config=watchdog,
                    expected_total=expected_total,
                    # Same heartbeat propagation as experiment verification:
                    # one progress entry per watchdog tick in the global log,
                    # every per-test entry in the dedicated test log.
                    on_heartbeat=global_heartbeat_bridge(elog, "adoption"),
                )
                self.experiment_service._remember_test_total(test_run.tests_total)
                test_exit_code = test_run.exit_code
                test_output = test_run.output
                verification_summary = f"local fallback integration tests passed (exit code {test_exit_code})"
                if test_run.timed_out:
                    keep_integration = True
                    elog.log_process(
                        "adoption",
                        "integration_tests_stalled" if test_run.stalled else "integration_tests_safety_cap_exceeded",
                        test_run.stall_diagnostics or "Integration test run aborted by the progress watchdog",
                        command=test_command, cwd=str(integration_path),
                        duration_ms=test_run.duration_ms,
                        timed_out=True, level="ERROR",
                        stall_timeout_seconds=watchdog.stall_timeout_seconds,
                        safety_cap_seconds=watchdog.safety_cap_seconds,
                        test_log_path=test_run.test_log_path,
                        **(test_run.progress or {}),
                    )
                elif test_run.process_died:
                    keep_integration = True
                    elog.log_process(
                        "adoption", "integration_tests_process_died",
                        test_run.stall_diagnostics or "Integration test process was killed before it could finish",
                        command=test_command, cwd=str(integration_path),
                        duration_ms=test_run.duration_ms, level="ERROR",
                        test_log_path=test_run.test_log_path,
                        **(test_run.progress or {}),
                    )
                elif test_run.exception is not None:
                    keep_integration = True
                    elog.error(
                        "adoption", "integration_tests_exception",
                        "Integration test command raised an exception",
                        exc=test_run.exception, command=test_command,
                        cwd=str(integration_path),
                    )
                else:
                    elog.log_process(
                        "adoption", "integration_tests_completed",
                        f"Integration tests finished with exit code {test_exit_code}",
                        command=test_command, cwd=str(integration_path),
                        duration_ms=test_run.duration_ms,
                        exit_code=test_exit_code,
                        stdout=test_run.stdout, stderr=test_run.stderr,
                    )

            bounded_test_output = "\n".join(bounded_lines(test_output, max_lines=200))
            if test_exit_code != 0:
                keep_integration = True
                return self._finish_failure(
                    experiment_id,
                    reason=(
                        "Die Integrationstests sind fehlgeschlagen."
                        if test_exit_code is not None
                        else "Die Integrationstests konnten nicht erfolgreich abgeschlossen werden."
                    ),
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            integration_status = _run_git(
                ["status", "--short", "--untracked-files=all"],
                cwd=str(integration_path),
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase="adoption",
            )
            integrated_changed = parse_git_status_short(integration_status.stdout)
            eval_result = evaluate_implementation(
                experiment.plan,
                experiment.goal,
                integrated_changed,
                str(integration_path),
            )
            if eval_result.outcome != "pass":
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason=f"Qualitäts-Gate auf integriertem Stand: {eval_result.reason}",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            integrated_commit_proc = _run_git(
                ["commit", "--no-verify", "-m", commit_message],
                cwd=str(integration_path),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if integrated_commit_proc.returncode != 0:
                keep_integration = True
                return self._finish_failure(
                    experiment_id,
                    reason="Der Integrations-Commit konnte nicht erstellt werden.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )
            integrated_commit_sha = self._git_stdout(["rev-parse", "HEAD"], str(integration_path), elog).strip() or None

            published_sha = integrated_commit_sha
            work_ref = self.github_work_tracking.for_experiment(experiment_id)
            if work_ref is None:
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason="Für das Experiment fehlt die persistierte GitHub-Issue-Zuordnung.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )
            try:
                self.github_work_tracking.require_lifecycle_ready()
            except GitHubWorkTrackingError as exc:
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason=f"GitHub lifecycle ist für die Veröffentlichung nicht bereit: {exc}",
                    review_blocker=self._lifecycle_readiness_blocker(),
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            try:
                published_sha = self.github_work_tracking.publish_final_tree(
                    work_ref,
                    integrated_commit_sha=integrated_commit_sha,
                    worktree_path=str(integration_path),
                    experiment_id=experiment_id,
                )
            except GitHubWorkTrackingError as exc:
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason=f"Der finale Push auf den Issue-Branch ist fehlgeschlagen: {exc}",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            # CI ran on the experiment branch; what merges is that change on
            # top of today's main. Test exactly that tree first (#455).
            if published_sha and not (exact_ci and evidence.passed and published_sha == checkpoint_sha):
                final_ci = self.github_work_tracking.wait_for_commit_ci(
                    published_sha, experiment_id=experiment_id
                )
                if final_ci.status != "passed":
                    keep_integration = True
                    return self._finish_failure(
                        experiment_id,
                        reason=(
                            f"CI on the integrated tree {published_sha[:12]} did not pass: "
                            f"{final_ci.status} {final_ci.reason or final_ci.conclusion or ''}".strip()
                        ),
                        actual_files=actual_files,
                        expected_files=expected_files,
                        test_exit_code=test_exit_code,
                        test_output=bounded_test_output,
                        experiment_commit_sha=experiment_commit_sha,
                        integrated_commit_sha=integrated_commit_sha,
                        diagnostic_worktree_path=str(integration_path),
                        elog=elog,
                    )

            plan_title = ""
            if isinstance(experiment.plan, dict):
                plan_title = str(experiment.plan.get("title") or "")
            pr_title = _collapse_summary(plan_title or experiment.goal, max_chars=200)
            pr_body = (
                f"Fixes #{work_ref.issue_number}\n\n"
                f"Nexus experiment: `{experiment_id}`\n\n"
                f"Goal: {_collapse_summary(experiment.goal, max_chars=1000)}\n\n"
                f"Verification: {verification_summary}; deterministic quality gate passed.\n\n"
                f"Changed files: {', '.join(actual_files[:30]) or '(none)'}"
            )
            try:
                work_ref = self.github_work_tracking.ensure_pull_request(
                    work_ref,
                    title=pr_title,
                    body=pr_body,
                    draft=False,
                )
                self.github_work_tracking.mark_pull_request_ready(work_ref)
                blocked = self._review_blocks(experiment_id, experiment.goal, work_ref, integration_path, elog)
                if blocked is not None:
                    return self._finish_failure(
                        experiment_id,
                        reason=blocked,
                        actual_files=actual_files,
                        expected_files=expected_files,
                        test_exit_code=test_exit_code,
                        test_output=bounded_test_output,
                        experiment_commit_sha=experiment_commit_sha,
                        integrated_commit_sha=integrated_commit_sha,
                        elog=elog,
                    )
                published_sha = self.github_work_tracking.merge_and_complete(
                    work_ref,
                    expected_head_sha=published_sha,
                ) or published_sha
            except MergeForbiddenError as exc:
                elog.warning(
                    "adoption", "merge_refused", str(exc),
                    pull_request_number=work_ref.pr_number,
                )
                return self._finish_review(
                    experiment_id,
                    reason=str(exc),
                    status=STATUS_READY_FOR_REVIEW,
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                )
            except GitHubWorkTrackingError as exc:
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason=f"GitHub PR konnte nicht automatisch abgeschlossen werden: {exc}",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            _run_git(
                ["fetch", "origin", _DEFAULT_TARGET_BRANCH],
                cwd=str(self.repo_root),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            local_sync_pending, adopted_reason = self._sync_local_repo(elog)
            self._set_status(
                experiment_id,
                STATUS_ADOPTED,
                ended_at=_utcnow_iso(),
                experiment_commit_sha=experiment_commit_sha,
                integrated_commit_sha=published_sha,
                expected_files_json=json.dumps(expected_files),
                actual_files_json=json.dumps(actual_files),
                test_exit_code=test_exit_code,
                test_output=bounded_test_output,
                reason=sanitized_reason(adopted_reason),
                local_sync_pending=1 if local_sync_pending else 0,
                diagnostic_worktree_path=None,
                updated_at=_utcnow_iso(),
            )
            return self.get(experiment_id)
        except ExperimentAdoptionError as exc:
            return self._finish_failure(experiment_id, reason=str(exc), elog=elog)
        except subprocess.SubprocessError as exc:
            keep_integration = True
            return self._finish_failure(
                experiment_id,
                reason=f"Git-/Test-Prozess fehlgeschlagen: {exc}",
                diagnostic_worktree_path=str(integration_path) if integration_path else None,
                elog=elog,
            )
        finally:
            try:
                if integration_path is not None and integration_path.exists() and not keep_integration:
                    _run_git(
                        ["worktree", "remove", "--force", str(integration_path)],
                        cwd=str(self.repo_root),
                        timeout=120,
                        elog=elog,
                        phase="adoption",
                    )
                    shutil.rmtree(integration_path, ignore_errors=True)
            finally:
                lock.release()

    def _lifecycle_readiness_blocker(self) -> str | None:
        """``REVIEW_BLOCKER_GITHUB_LIFECYCLE`` iff the lifecycle is unconfigured now.

        A ``GitHubWorkTrackingError`` is not automatically a readiness
        problem - the same call can fail because the GitHub API rejected a
        request - and only the readiness case is re-evaluable. So the
        blocker is recorded from the service's own current verdict rather
        than from the exception text.
        """
        try:
            return (
                REVIEW_BLOCKER_GITHUB_LIFECYCLE
                if self.github_work_tracking.readiness_error()
                else None
            )
        except Exception:  # noqa: BLE001 - classification must never fail the review
            logger.warning("could not classify GitHub lifecycle readiness", exc_info=True)
            return None

    def _review_blocks(self, experiment_id, goal, work_ref, integration_path, elog) -> str | None:
        """Review the change before the merge (#396); the reason if it is blocked.

        The review result goes on the pull request either way. A review that
        cannot run does not block: tests and the quality gate already passed.
        """
        if self.reviewer is None:
            return None
        try:
            result = self.reviewer.review(
                experiment_id=experiment_id,
                goal=str(goal or ""),
                worktree_path=str(integration_path),
                base_sha=self._integration_base_sha(),
            )
        except Exception as exc:  # noqa: BLE001 - a broken review must not block
            logger.warning("adoption review failed for %s: %s", experiment_id, exc)
            return None
        from modules.nexus_reviewer import review_comment

        if elog is not None:
            elog.info("adoption", "review_completed", f"{result.reviewer}: {result.verdict}")
        try:
            self.github_work_tracking.comment(work_ref.pr_number, review_comment(result))
        except Exception as exc:  # noqa: BLE001 - the comment is informational
            logger.warning("could not post the review on PR #%s: %s", work_ref.pr_number, exc)
        if result.blocks:
            return f"Review by {result.reviewer} blocked the merge: {result.summary}"
        return None

    def _finish_failure(
        self,
        experiment_id: str,
        *,
        reason: str,
        actual_files: list[str] | None = None,
        expected_files: list[str] | None = None,
        test_exit_code: int | None = None,
        test_output: str | None = None,
        experiment_commit_sha: str | None = None,
        integrated_commit_sha: str | None = None,
        diagnostic_worktree_path: str | None = None,
        elog=None,
    ) -> ExperimentAdoption:
        if elog is not None:
            elog.warning("adoption", "adoption_failed", reason)
        self._set_status(
            experiment_id,
            STATUS_FAILED,
            ended_at=_utcnow_iso(),
            experiment_commit_sha=experiment_commit_sha,
            integrated_commit_sha=integrated_commit_sha,
            expected_files_json=json.dumps(_safe_list(expected_files)),
            actual_files_json=json.dumps(_safe_list(actual_files)),
            test_exit_code=test_exit_code,
            test_output=_truncate_text(test_output, 32 * 1024, label="test output"),
            reason=sanitized_reason(reason),
            diagnostic_worktree_path=diagnostic_worktree_path,
            updated_at=_utcnow_iso(),
        )
        return self.get(experiment_id)

    def _finish_review(
        self,
        experiment_id: str,
        *,
        reason: str,
        actual_files: list[str] | None = None,
        expected_files: list[str] | None = None,
        test_exit_code: int | None = None,
        test_output: str | None = None,
        experiment_commit_sha: str | None = None,
        integrated_commit_sha: str | None = None,
        diagnostic_worktree_path: str | None = None,
        review_blocker: str | None = None,
        status: str = STATUS_NEEDS_REVIEW,
        elog=None,
    ) -> ExperimentAdoption:
        """Park the adoption for review.

        ``review_blocker`` names a re-evaluable external condition when there
        is one (#171). It is deliberately not derived from ``reason``: the
        text is a human explanation, while this is the machine-readable
        handle a later attempt uses to ask whether the condition still holds.
        Leaving it ``None`` keeps the review terminal, which is the default
        for everything that is a judgement about the change itself.
        """
        if elog is not None:
            elog.warning(
                "adoption",
                "adoption_needs_review",
                reason,
                review_blocker=review_blocker,
            )
        self._set_status(
            experiment_id,
            status,
            review_blocker=review_blocker,
            ended_at=_utcnow_iso(),
            experiment_commit_sha=experiment_commit_sha,
            integrated_commit_sha=integrated_commit_sha,
            expected_files_json=json.dumps(_safe_list(expected_files)),
            actual_files_json=json.dumps(_safe_list(actual_files)),
            test_exit_code=test_exit_code,
            test_output=_truncate_text(test_output, 32 * 1024, label="test output"),
            reason=sanitized_reason(reason),
            diagnostic_worktree_path=diagnostic_worktree_path,
            updated_at=_utcnow_iso(),
        )
        return self.get(experiment_id)

    def _git_stdout(self, args: list[str], cwd: str, elog) -> str:
        proc = _run_git(args, cwd=cwd, timeout=_DEFAULT_GIT_TIMEOUT_SECONDS, elog=elog, phase="adoption")
        return proc.stdout or ""

    def _commit_message(self, experiment_id: str, goal: str, plan: dict[str, Any] | None) -> str:
        title = ""
        if isinstance(plan, dict):
            title = str(plan.get("title") or "")
        summary = _collapse_summary(title or goal)
        return f"Adopt experiment {experiment_id[:8]}: {summary}"

    def _sync_local_repo(self, elog) -> tuple[bool, str]:
        status_proc = _run_git(["status", "--porcelain"], cwd=str(self.repo_root), timeout=30, elog=elog, phase="adoption")
        if status_proc.returncode != 0 or status_proc.stdout.strip():
            return True, "Übernahme veröffentlicht; lokales Repo ist nicht sauber und muss manuell synchronisiert werden."
        branch_proc = _run_git(["branch", "--show-current"], cwd=str(self.repo_root), timeout=30, elog=elog, phase="adoption")
        if branch_proc.returncode != 0 or branch_proc.stdout.strip() != _DEFAULT_TARGET_BRANCH:
            return True, "Übernahme veröffentlicht; lokales Repo ist nicht auf main und wurde nicht automatisch synchronisiert."
        merge_proc = _run_git(["merge", "--ff-only", f"origin/{_DEFAULT_TARGET_BRANCH}"], cwd=str(self.repo_root), timeout=120, elog=elog, phase="adoption")
        if merge_proc.returncode != 0:
            return True, "Übernahme veröffentlicht; lokales Repo konnte nicht per Fast-Forward synchronisiert werden."
        return False, "Experiment erfolgreich übernommen und veröffentlicht."
