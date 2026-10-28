from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from modules.nexus_reviewer import default_reviewer
from modules.experiment_adoption_service import (
    _NullExperimentLogger,
    STATUS_ADOPTED,
    STATUS_ADOPTING,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_STARTED,
    STATUS_QUEUED,
    ExperimentAdoptionError,
    ExperimentAdoptionService,
    StaleFailureAssessment,
)
from modules.experiment_service import _truncate_text
from modules.runtime_execution import resolve_python_interpreter
from modules.run_store import DEFAULT_DB_PATH, init_db


logger = logging.getLogger(__name__)

PHASE_VERIFIED = "verified"
PHASE_MERGING = "merging"
PHASE_DEPLOYING = "deploying"
PHASE_RESTART_REQUESTED = "restart_requested"
PHASE_VALIDATING_LIVE = "validating_live"
PHASE_SUCCEEDED = "succeeded"
PHASE_NEEDS_REVIEW = "needs_review"

# Prefix of the error a finalization attempt records when adoption could
# not even be started. It is a shared constant because the needs_review
# requeue (#171) reads it back: a requeue that never started is not a new
# situation, so it must spend the ordinary budget instead of renewing it.
_START_FAILURE_PREFIX = "could not start adoption: "

_DEFAULT_MAX_RETRIES = 2
_DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/health"
# How long a requested restart may stay unobserved before finalization stops
# waiting (#269). On the VM it hung for over an hour with nothing to show.
_DEFAULT_RESTART_TIMEOUT_SECONDS = 600
# A restart that cannot happen (e.g. no permission) fails within this window;
# one that works may stop this very process before it returns.
_RESTART_FAILURE_WINDOW_SECONDS = 5
_ACTIVE_APPROVAL_STATUSES = {
    "pending_approval", "approved", "submitted", "running", "nexus_deciding",
}
# Restarted after an adopted change. scripts/setup-nexus-permissions.sh grants
# the service user exactly the transient-unit command assembled below.
RESTART_SERVICES: tuple[str, ...] = ("agent-api",)


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    status: str
    phase: str
    summary: str


@dataclass(slots=True)
class _State:
    approval_id: str
    experiment_id: str
    phase: str
    restart_requested_at: float | None
    retry_count: int
    merge_commit: str | None
    live_validation_json: str | None
    last_error: str | None


RestartRunner = Callable[[list[str]], None]
LiveValidator = Callable[[Any, Any], dict[str, Any]]


class LiveHealthUnavailableError(RuntimeError):
    """The restarted API has not begun accepting health requests yet."""


class TaskFinalizationCoordinator:
    """Continue a verified development task until the merged code is live.

    Experiment verification remains evidence only. The existing adoption
    service owns PR finalization/merge and repository adoption; this class
    persists the late task lifecycle around it and resumes after process
    restarts, including the intentional service restart during deployment.
    """

    def __init__(
        self,
        *,
        experiment_service: Any,
        github_work_tracking: Any,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        adoption_service: Any | None = None,
        restart_runner: RestartRunner | None = None,
        live_validator: LiveValidator | None = None,
        process_started_at: float | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        restart_timeout_seconds: float | None = None,
    ) -> None:
        self.experiment_service = experiment_service
        if restart_timeout_seconds is None:
            restart_timeout_seconds = float(
                os.environ.get(
                    "NEXUS_FINALIZE_RESTART_TIMEOUT_SECONDS", _DEFAULT_RESTART_TIMEOUT_SECONDS
                )
            )
        self.restart_timeout_seconds = max(0.0, float(restart_timeout_seconds))
        self.github_work_tracking = github_work_tracking
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.max_retries = max(0, int(max_retries))
        self.process_started_at = process_started_at if process_started_at is not None else time.time()
        self.adoption_service = adoption_service or ExperimentAdoptionService(
            experiment_service=experiment_service,
            db_path=db_path,
            repo_root=str(self.repo_root),
            github_work_tracking=github_work_tracking,
            reviewer=default_reviewer(db_path),
        )
        self._owns_adoption_service = adoption_service is None
        self._restart_runner = restart_runner or self._restart_services
        self._live_validator = live_validator or self._validate_live
        self._conn = init_db(db_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        if self._owns_adoption_service:
            self.adoption_service.close()
        self._conn.close()

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_finalizations (
                    approval_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL UNIQUE,
                    phase TEXT NOT NULL,
                    restart_requested_at REAL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    merge_commit TEXT,
                    live_validation_json TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def get_phase(self, approval_id: str) -> str | None:
        state = self._get(approval_id)
        return state.phase if state is not None else None

    def advance(self, approval: Any, experiment: Any) -> FinalizationResult:
        state = self._get_or_create(str(approval.id), str(experiment.id))
        adoption = self.adoption_service.get(str(experiment.id))

        superseded: Any | None = None
        review_retry: Any | None = None
        if adoption.status == STATUS_NEEDS_REVIEW:
            review_retry = self._review_blocker_cleared(state, experiment, adoption)
            if review_retry is None:
                summary = (
                    "adoption requires review: "
                    f"{getattr(adoption, 'reason', None) or 'unknown reason'}"
                )
                if self._requeue_never_started(state):
                    # Keep the recorded start failure at the front: it is
                    # both the more useful diagnosis and the marker that
                    # says this requeue budget is spent. Replacing it with
                    # the review text would renew the budget on the next
                    # attempt and restart the very loop it bounds.
                    summary = f"{state.last_error} (requeue stopped; {summary})"
                return self._needs_review(state, summary)

        if (
            adoption.status in {STATUS_NOT_STARTED, STATUS_FAILED, STATUS_INTERRUPTED}
            or review_retry is not None
        ):
            if review_retry is not None:
                # The blocker that parked this adoption is gone, so this is a
                # new situation and not another attempt against the same
                # evidence: the finalization budget starts over, exactly as
                # it does for a superseded failure. The adoption service's
                # own evidence budget then bounds how often a cleared
                # blocker may reopen the same record - but only once the
                # restart actually reaches it, so a requeue that never got
                # that far keeps spending this budget instead of renewing
                # it (see `_review_blocker_cleared`).
                superseded = review_retry
                if self._requeue_never_started(state):
                    state.retry_count += 1
                else:
                    state.retry_count = 1
                self._save(state)
            elif adoption.status in {STATUS_FAILED, STATUS_INTERRUPTED}:
                if state.retry_count >= self.max_retries:
                    # The retry budget counts attempts made against the same
                    # evidence, so an exhausted budget only means "nothing
                    # new has happened". When current evidence has actually
                    # superseded the recorded failure (Issue #136) - most
                    # concretely a lifecycle fix like #134 landing after the
                    # failure was written - that is a genuinely new
                    # situation and earns a fresh budget. Without this an
                    # obsolete row stays terminal forever and the lifecycle
                    # fix never reaches the experiments it was written for.
                    assessment = self._failure_assessment(experiment)
                    superseded = (
                        assessment if getattr(assessment, "retryable", False) else None
                    )
                    if superseded is None:
                        reason = (
                            "finalization retries exhausted after adoption "
                            f"status={adoption.status}: "
                            f"{getattr(adoption, 'reason', None) or 'unknown error'}"
                        )
                        # The historical failure text alone made this look
                        # like a generic test failure. Name the decision and
                        # the evidence category that produced it (#141).
                        self._elog(experiment).warning(
                            "finalization",
                            "finalization_blocked",
                            "Finalization stays in needs_review; the recorded "
                            "adoption failure was not superseded",
                            # Not `phase`: that is the log entry's own
                            # column, and this is the finalization phase.
                            finalization_phase=PHASE_NEEDS_REVIEW,
                            adoption_status=adoption.status,
                            assessment_category=getattr(assessment, "category", None),
                            assessment_detail=getattr(assessment, "detail", None),
                            retry_count=state.retry_count,
                            max_retries=self.max_retries,
                            reason=reason,
                        )
                        return self._needs_review(state, reason)
                    logger.info(
                        "granting a fresh finalization budget for %s: %s",
                        experiment.id,
                        getattr(superseded, "detail", ""),
                    )
                    state.retry_count = 0
                state.retry_count += 1
                self._save(state)
            try:
                # A previous adoption may already have committed the exact
                # source diff that was verified. For the moved-base fallback,
                # restore that trusted commit to its original verified diff
                # shape before the ordinary adoption guard runs. This keeps
                # verified-source integrity separate from the later
                # current-base integration candidate (#149).
                self._prepare_integration_fallback_source(experiment, superseded)
                # Passing the assessment back is what tells the adoption
                # service to spend one unit of the evidence budget. An
                # ordinary retry leaves `superseded` None and so does not
                # consume it - otherwise ordinary retries could exhaust the
                # budget before any evidence changed.
                adoption = self.adoption_service.start_adoption(
                    str(experiment.id), superseded=superseded
                )
                if getattr(superseded, "category", None) == "integration_fallback_required":
                    self._elog(experiment).info(
                        "finalization",
                        "integration_candidate",
                        "Current-base integration candidate was queued after verified source integrity passed",
                        outcome="queued",
                        source_commit_sha=getattr(adoption, "experiment_commit_sha", None),
                    )
            except Exception as exc:
                return self._retry_or_review(state, f"{_START_FAILURE_PREFIX}{exc}")
            state.phase = PHASE_MERGING
            state.last_error = None
            self._save(state)
            return FinalizationResult("running", state.phase, "verification passed; adoption started")

        if adoption.status in {STATUS_QUEUED, STATUS_ADOPTING}:
            state.phase = PHASE_MERGING
            self._save(state)
            return FinalizationResult("running", state.phase, "verified change is being merged/adopted")

        if adoption.status != STATUS_ADOPTED:
            return self._retry_or_review(state, f"unexpected adoption status: {adoption.status}")

        state.merge_commit = getattr(adoption, "integrated_commit_sha", None) or state.merge_commit
        if getattr(adoption, "local_sync_pending", False):
            state.phase = PHASE_DEPLOYING
            self._save(state)
            try:
                self._sync_live_checkout()
            except Exception as exc:
                return self._retry_or_review(state, f"live checkout update failed: {exc}")

        if state.restart_requested_at is None:
            state.phase = PHASE_RESTART_REQUESTED
            state.restart_requested_at = time.time()
            state.last_error = None
            self._save(state)
            try:
                self._restart_runner(self._restart_service_names())
            except Exception as exc:
                state.restart_requested_at = None
                return self._retry_or_review(state, f"service restart failed: {exc}")
            return FinalizationResult(
                "running",
                state.phase,
                "merged main is on the live checkout; service restart requested",
            )

        if self.process_started_at <= state.restart_requested_at:
            waited = time.time() - state.restart_requested_at
            if waited > self.restart_timeout_seconds:
                reason = (
                    f"service restart of {', '.join(self._restart_service_names())} was "
                    f"requested {int(waited)}s ago but not observed within "
                    f"{int(self.restart_timeout_seconds)}s; this process has run since "
                    f"before the request"
                )
                logger.warning("finalization %s: %s", state.approval_id, reason)
                return self._needs_review(state, reason)
            return FinalizationResult(
                "running",
                PHASE_RESTART_REQUESTED,
                "waiting for Nexus to resume after the requested service restart",
            )

        state.phase = PHASE_VALIDATING_LIVE
        self._save(state)
        try:
            validation = self._live_validator(approval, experiment)
        except LiveHealthUnavailableError as exc:
            waited = time.time() - state.restart_requested_at
            if waited > self.restart_timeout_seconds:
                reason = (
                    f"live health stayed unreachable for {int(waited)}s after restart "
                    f"was requested (timeout {int(self.restart_timeout_seconds)}s): {exc}"
                )
                logger.warning("finalization %s: %s", state.approval_id, reason)
                return self._needs_review(state, reason)
            state.phase = PHASE_RESTART_REQUESTED
            state.last_error = None
            self._save(state)
            return FinalizationResult(
                "running",
                state.phase,
                "service restart was observed; waiting for the live health endpoint",
            )
        except Exception as exc:
            return self._retry_or_review(state, f"live validation failed: {exc}", reset_restart=True)

        state.phase = PHASE_SUCCEEDED
        state.live_validation_json = json.dumps(validation, sort_keys=True)[:16000]
        state.last_error = None
        self._save(state)
        details = validation.get("summary") if isinstance(validation, dict) else None
        return FinalizationResult(
            "succeeded",
            state.phase,
            str(details or "merged, deployed and live validation passed"),
        )

    def _elog(self, experiment: Any):
        """An ExperimentLogger for this experiment, or a no-op stand-in.

        Diagnostics must never break the lifecycle they describe, so any
        failure to build one degrades to silence.
        """
        try:
            return self.experiment_service.get_experiment_logger(str(experiment.id))
        except Exception:  # noqa: BLE001 - logging is never worth a lifecycle failure
            logger.warning("no experiment logger for %s", experiment.id, exc_info=True)
            return _NullExperimentLogger()

    @staticmethod
    def _requeue_never_started(state: _State) -> bool:
        """Whether the previous finalization attempt failed before adoption began."""
        return (state.last_error or "").startswith(_START_FAILURE_PREFIX)

    def _review_blocker_cleared(
        self, state: _State, experiment: Any, adoption: Any
    ) -> Any | None:
        """The assessment that reopens a persisted ``needs_review``, or None (#171).

        A ``needs_review`` used to be terminal here without ever being
        re-examined, so a review caused by a *transient* blocker - the
        GitHub lifecycle being unconfigured in #115/PR #116 - kept
        returning its recorded reason long after the VM was fixed and the
        service restarted. That reads like a current readiness verdict
        while no readiness check has happened at all.

        The re-evaluation is the adoption service's own stale-adoption
        assessment, so genuinely manual reviews stay terminal, the same
        bounded evidence budget applies, and the same record is reused
        rather than a second adoption being created.
        """
        assessment = self._failure_assessment(experiment)
        reason = getattr(adoption, "reason", None) or "unknown reason"
        # A cleared blocker that keeps failing to restart adoption is not
        # new evidence, however often it is re-checked. Without this bound
        # the same requeue would be attempted on every tick forever and a
        # permanent obstacle would stay hidden behind a "retrying" status.
        exhausted = (
            self._requeue_never_started(state) and state.retry_count >= self.max_retries
        )
        if not getattr(assessment, "retryable", False) or exhausted:
            self._elog(experiment).warning(
                "finalization",
                "needs_review_retained",
                "Persisted adoption needs_review is reused as terminal; its "
                "blocker is not re-evaluable, still applies, or its requeue "
                "budget is spent",
                finalization_phase=PHASE_NEEDS_REVIEW,
                adoption_status=getattr(adoption, "status", None),
                review_blocker=getattr(adoption, "review_blocker", None),
                assessment_category=getattr(assessment, "category", None),
                assessment_detail=getattr(assessment, "detail", None),
                requeue_budget_spent=exhausted,
                retry_count=state.retry_count,
                max_retries=self.max_retries,
                reason=reason,
            )
            return None

        self._elog(experiment).info(
            "finalization",
            "needs_review_requeued",
            "Persisted adoption needs_review is requeued; its recorded "
            "blocker no longer applies",
            adoption_status=getattr(adoption, "status", None),
            review_blocker=getattr(adoption, "review_blocker", None),
            assessment_category=getattr(assessment, "category", None),
            assessment_detail=getattr(assessment, "detail", None),
            previous_reason=reason,
        )
        logger.info(
            "requeuing a needs_review adoption for %s: %s",
            experiment.id,
            getattr(assessment, "detail", ""),
        )
        return assessment

    def _failure_assessment(self, experiment: Any) -> Any | None:
        """Return the adoption verdict, promoting persisted green CI to integration fallback.

        ``ExperimentAdoptionService.assess_failure`` remains the single owner of CI
        authority and base validation. If it rejects a previously persisted green
        exact-checkpoint verdict as ``no_ci_evidence``, finalization does not repeat
        the base comparison. It only observes that durable green evidence exists and
        reopens the existing adoption for the pipeline's current-base integration
        fallback. A real test verdict or exhausted stale-retry budget is returned by
        the adoption service with a different category and is never overridden.
        """
        assess = getattr(self.adoption_service, "assess_failure", None)
        if assess is None:
            return None
        try:
            assessment = assess(str(experiment.id))
        except Exception:  # noqa: BLE001 - the retry budget must still apply
            logger.warning(
                "could not assess adoption failure for %s", experiment.id, exc_info=True
            )
            return None

        if getattr(assessment, "category", None) != "no_ci_evidence":
            return assessment

        experiment_id = str(experiment.id)
        try:
            work_ref = self.github_work_tracking.for_experiment(experiment_id)
            checkpoint_sha = (
                str(getattr(work_ref, "checkpoint_sha", "") or "").strip()
                if work_ref is not None
                else ""
            )
            details = (
                self.github_work_tracking.recorded_ci_details(checkpoint_sha)
                if checkpoint_sha
                else None
            )
        except Exception:  # noqa: BLE001 - preserve the adoption service's refusal
            logger.warning(
                "could not inspect persisted CI evidence for %s",
                experiment_id,
                exc_info=True,
            )
            return assessment

        if not details or str(details.get("status") or "") != "passed":
            return assessment

        run_id = details.get("run_id")
        promoted = StaleFailureAssessment(
            True,
            "integration_fallback_required",
            "a persisted green exact-checkpoint verdict is no longer authoritative "
            "for the integration; validate the checkpoint change on the current "
            "integration base before retrying adoption",
            previous_reason=getattr(assessment, "previous_reason", None),
            evidence_commit_sha=checkpoint_sha,
            evidence_run_id=int(run_id) if run_id is not None else None,
        )
        self._elog(experiment).info(
            "finalization",
            "integration_fallback_required",
            "Persisted green checkpoint evidence requires current-base integration fallback",
            checkpoint_sha=checkpoint_sha,
            run_id=promoted.evidence_run_id,
            original_assessment_category=getattr(assessment, "category", None),
            original_assessment_detail=getattr(assessment, "detail", None),
        )
        return promoted

    def _prepare_integration_fallback_source(self, experiment: Any, assessment: Any | None) -> None:
        """Restore a previously committed verified source diff for a moved-base retry.

        The first adoption attempt may have committed the experiment worktree before
        failing later in integration. The experiment record still stores the verified
        *pre-commit* git status/stat, so blindly applying the normal start guard to the
        now-clean worktree incorrectly reports checkpoint tampering. For the explicit
        moved-base fallback only, prove that the persisted source commit is exactly the
        verified diff, prove the worktree itself has not changed, then restore that diff
        shape so the existing adoption guard can independently validate it again.
        """
        if getattr(assessment, "category", None) != "integration_fallback_required":
            return

        experiment_id = str(experiment.id)
        adoption = self.adoption_service.get(experiment_id)
        source_sha = str(getattr(adoption, "experiment_commit_sha", "") or "").strip()
        if not source_sha:
            # The old attempt never created a source commit. The ordinary guard
            # must validate the still-uncommitted verified worktree as before.
            return

        worktree_raw = str(getattr(experiment, "worktree_path", "") or "")
        worktree = Path(worktree_raw)
        elog = self._elog(experiment)

        def run_git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=worktree,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )

        def reject(reason: str) -> None:
            elog.warning(
                "finalization",
                "verified_source_integrity",
                "Persisted verified source commit failed integrity validation",
                outcome="rejected",
                source_form="persisted_commit",
                source_commit_sha=source_sha,
                reason=reason,
            )
            raise ExperimentAdoptionError(
                "Die Änderungen im Experiment-Worktree haben sich seit der erfolgreichen Prüfung verändert."
            )

        if not worktree_raw or not worktree.is_dir():
            reject("experiment worktree is unavailable")

        head = run_git("rev-parse", "HEAD")
        status = run_git("status", "--porcelain", "--untracked-files=all")
        if head.returncode != 0 or status.returncode != 0:
            reject("git could not inspect the persisted source worktree")
        if head.stdout.strip() != source_sha:
            reject("worktree HEAD no longer matches the persisted source commit")
        if status.stdout.strip():
            reject("worktree contains changes after the persisted source commit")

        parent = run_git("rev-parse", f"{source_sha}^")
        stat = run_git("diff", f"{source_sha}^", source_sha, "--stat")
        diff = run_git("diff", f"{source_sha}^", source_sha)
        if parent.returncode != 0 or stat.returncode != 0 or diff.returncode != 0:
            reject("persisted source commit cannot be compared with its parent")

        stored_stat = str(getattr(experiment, "diff_stat", "") or "").strip()
        stored_diff = str(getattr(experiment, "diff_text", "") or "").strip()
        max_diff_bytes = int(getattr(self.experiment_service, "max_diff_bytes", 64 * 1024))
        comparable_diff = _truncate_text(diff.stdout, max_diff_bytes, label="diff") or ""
        if stat.stdout.strip() != stored_stat or comparable_diff.strip() != stored_diff:
            reject("persisted source commit does not equal the verified experiment diff")

        elog.info(
            "finalization",
            "verified_source_integrity",
            "Persisted source commit exactly matches the verified experiment diff",
            outcome="accepted",
            source_form="persisted_commit",
            source_commit_sha=source_sha,
        )

        reset = run_git("reset", "--mixed", parent.stdout.strip())
        if reset.returncode != 0:
            reject("verified source commit could not be restored to its pre-commit diff")

        # The normal adoption guard runs immediately after this helper and
        # captures the restored worktree with its own scratch index. It is the
        # authoritative second check (including untracked/new files), so do not
        # duplicate that snapshot logic here with subtly different git semantics.

    def _retry_or_review(
        self,
        state: _State,
        error: str,
        *,
        reset_restart: bool = False,
    ) -> FinalizationResult:
        state.retry_count += 1
        state.last_error = str(error)[:2000]
        if reset_restart:
            state.restart_requested_at = None
        if state.retry_count > self.max_retries:
            return self._needs_review(state, error)
        self._save(state)
        return FinalizationResult(
            "running",
            state.phase,
            f"late-stage finalization failed; retry {state.retry_count}/{self.max_retries}: {error}",
        )

    def _needs_review(self, state: _State, error: str) -> FinalizationResult:
        state.phase = PHASE_NEEDS_REVIEW
        state.last_error = str(error)[:2000]
        self._save(state)
        return FinalizationResult("needs_review", state.phase, state.last_error)

    def _get_or_create(self, approval_id: str, experiment_id: str) -> _State:
        existing = self._get(approval_id)
        if existing is not None:
            if existing.experiment_id != experiment_id:
                raise RuntimeError("finalization state is bound to a different experiment")
            return existing

        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self._get(approval_id)
            if existing is not None:
                if existing.experiment_id != experiment_id:
                    raise RuntimeError("finalization state is bound to a different experiment")
                self._conn.commit()
                return existing

            experiment_state = self._get_by_experiment(experiment_id)
            if experiment_state is not None:
                if self._approval_is_active(experiment_state.approval_id):
                    raise RuntimeError(
                        "finalization state is already owned by an active approval: "
                        f"{experiment_state.approval_id}"
                    )
                updated = self._conn.execute(
                    """
                    UPDATE task_finalizations
                    SET approval_id=?, retry_count=0, last_error=NULL, updated_at=?
                    WHERE experiment_id=? AND approval_id=?
                    """,
                    (approval_id, now, experiment_id, experiment_state.approval_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("finalization state ownership changed during approval rebind")
            else:
                self._conn.execute(
                    """
                    INSERT INTO task_finalizations (
                        approval_id, experiment_id, phase, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (approval_id, experiment_id, PHASE_VERIFIED, now, now),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        state = self._get(approval_id)
        if state is None:
            raise RuntimeError("failed to persist task finalization state")
        return state

    def _approval_is_active(self, approval_id: str) -> bool:
        row = self._conn.execute(
            "SELECT status FROM mailbox_task_approvals WHERE id=?",
            (approval_id,),
        ).fetchone()
        return row is not None and str(row["status"]) in _ACTIVE_APPROVAL_STATUSES

    def _get(self, approval_id: str) -> _State | None:
        row = self._conn.execute(
            "SELECT * FROM task_finalizations WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        return self._state_from_row(row)

    def _get_by_experiment(self, experiment_id: str) -> _State | None:
        row = self._conn.execute(
            "SELECT * FROM task_finalizations WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        return self._state_from_row(row)

    @staticmethod
    def _state_from_row(row: sqlite3.Row | None) -> _State | None:
        if row is None:
            return None
        return _State(
            approval_id=str(row["approval_id"]),
            experiment_id=str(row["experiment_id"]),
            phase=str(row["phase"]),
            restart_requested_at=(
                float(row["restart_requested_at"])
                if row["restart_requested_at"] is not None
                else None
            ),
            retry_count=int(row["retry_count"] or 0),
            merge_commit=str(row["merge_commit"]) if row["merge_commit"] else None,
            live_validation_json=(
                str(row["live_validation_json"]) if row["live_validation_json"] else None
            ),
            last_error=str(row["last_error"]) if row["last_error"] else None,
        )

    def _save(self, state: _State) -> None:
        with self._conn:
            updated = self._conn.execute(
                """
                UPDATE task_finalizations
                SET phase=?, restart_requested_at=?, retry_count=?, merge_commit=?,
                    live_validation_json=?, last_error=?, updated_at=?
                WHERE approval_id=? AND experiment_id=?
                """,
                (
                    state.phase,
                    state.restart_requested_at,
                    state.retry_count,
                    state.merge_commit,
                    state.live_validation_json,
                    state.last_error,
                    time.time(),
                    state.approval_id,
                    state.experiment_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("finalization state ownership changed to another approval")

    def _sync_live_checkout(self) -> None:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if status.returncode != 0:
            raise RuntimeError(status.stderr.strip() or "git status failed")
        if status.stdout.strip():
            raise RuntimeError("live checkout has local changes")
        commands = (
            ["git", "fetch", "origin", "main"],
            ["git", "checkout", "main"],
            ["git", "merge", "--ff-only", "origin/main"],
        )
        for command in commands:
            proc = subprocess.run(
                command,
                cwd=self.repo_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or " ".join(command) + " failed")

    @staticmethod
    def _restart_service_names() -> list[str]:
        # Fixed on purpose: the sudoers rule from setup-nexus-permissions.sh
        # allows exactly this command, so a configurable list could only fail.
        return list(RESTART_SERVICES)

    @staticmethod
    def _restart_services(services: list[str]) -> None:
        if not services:
            return
        # The service user may restart the units only through the sudoers rule
        # from scripts/setup-nexus-permissions.sh; -n fails instead of prompting.
        # A systemctl process launched directly by agent-api remains in that
        # service's cgroup and is killed during its own restart, even with
        # --no-block. Hand the delayed restart to a transient systemd unit so
        # it survives agent-api's teardown and is collected afterwards (#280).
        command = [
            "sudo",
            "-n",
            "systemd-run",
            "--collect",
            "--on-active=2",
            "systemctl",
            "restart",
            *services,
        ]
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        # Its output used to be discarded, so a restart that could not happen
        # left finalization waiting for it with no trace of why (#269).
        try:
            _stdout, stderr = proc.communicate(timeout=_RESTART_FAILURE_WINDOW_SECONDS)
        except subprocess.TimeoutExpired:
            # Return now so a working restart can stop this process, but keep
            # watching: a slow restart that fails later must still leave its
            # exit code and stderr in the log (#269 review).
            logger.info("service restart still running: %s", " ".join(command))
            threading.Thread(
                target=TaskFinalizationCoordinator._log_restart_outcome,
                args=(proc, command),
                name="finalization-restart-watch",
                daemon=True,
            ).start()
            return
        stderr = TaskFinalizationCoordinator._log_restart_result(proc.returncode, stderr, command)
        if proc.returncode != 0:
            raise RuntimeError(
                f"{' '.join(command)} exited {proc.returncode}: {stderr or 'no output'}"
            )

    @staticmethod
    def _log_restart_outcome(proc: Any, command: list[str]) -> None:
        """Wait for a restart that outlived the failure window and log how it ended."""
        try:
            _stdout, stderr = proc.communicate()
        except Exception:  # noqa: BLE001 - a watcher must never raise
            logger.warning("service restart outcome unknown: %s", " ".join(command), exc_info=True)
            return
        TaskFinalizationCoordinator._log_restart_result(proc.returncode, stderr, command)

    @staticmethod
    def _log_restart_result(returncode: Any, stderr: Any, command: list[str]) -> str:
        stderr = (stderr or "").strip()[:500]
        log = logger.info if returncode == 0 else logger.warning
        log(
            "service restart finished: %s exit=%s stderr=%s",
            " ".join(command),
            returncode,
            stderr,
        )
        return stderr

    def _validate_live(self, approval: Any, experiment: Any) -> dict[str, Any]:
        health_url = os.environ.get("NEXUS_LIVE_HEALTH_URL", _DEFAULT_HEALTH_URL)
        try:
            with urllib.request.urlopen(health_url, timeout=10) as response:  # noqa: S310
                if response.status != 200:
                    raise RuntimeError(f"health endpoint returned HTTP {response.status}")
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"health endpoint returned HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise LiveHealthUnavailableError(f"health endpoint unavailable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid health response: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise RuntimeError(f"unexpected health response: {payload!r}")

        result: dict[str, Any] = {"health": payload}
        task_text = str(getattr(approval, "task_body", "") or "")
        if "--version" in task_text:
            python = resolve_python_interpreter(str(self.repo_root))
            proc = subprocess.run(
                [python, "agent.py", "--version"],
                cwd=self.repo_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "agent.py --version failed")
            cli_version = proc.stdout.strip()
            if not cli_version:
                raise RuntimeError("agent.py --version returned an empty version")
            health_version = payload.get("version")
            if health_version is None:
                raise RuntimeError("health response does not contain version")
            if str(health_version) != cli_version:
                raise RuntimeError(
                    f"live version mismatch: cli={cli_version!r}, health={health_version!r}"
                )
            result["cli_version"] = cli_version

        result["summary"] = "live health passed" + (
            f"; version={result['cli_version']}" if "cli_version" in result else ""
        )
        return result
