from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from modules.experiment_adoption_service import (
    _NullExperimentLogger,
    STATUS_ADOPTED,
    STATUS_ADOPTING,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_STARTED,
    STATUS_QUEUED,
    ExperimentAdoptionService,
    StaleFailureAssessment,
)
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

_DEFAULT_MAX_RETRIES = 2
_DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/health"
_ACTIVE_APPROVAL_STATUSES = {"pending_approval", "approved", "submitted", "running"}


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
    ) -> None:
        self.experiment_service = experiment_service
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
        if adoption.status in {STATUS_NOT_STARTED, STATUS_FAILED, STATUS_INTERRUPTED}:
            if adoption.status in {STATUS_FAILED, STATUS_INTERRUPTED}:
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
                # Passing the assessment back is what tells the adoption
                # service to spend one unit of the evidence budget. An
                # ordinary retry leaves `superseded` None and so does not
                # consume it - otherwise ordinary retries could exhaust the
                # budget before any evidence changed.
                adoption = self.adoption_service.start_adoption(
                    str(experiment.id), superseded=superseded
                )
            except Exception as exc:
                return self._retry_or_review(state, f"could not start adoption: {exc}")
            state.phase = PHASE_MERGING
            state.last_error = None
            self._save(state)
            return FinalizationResult("running", state.phase, "verification passed; adoption started")

        if adoption.status in {STATUS_QUEUED, STATUS_ADOPTING}:
            state.phase = PHASE_MERGING
            self._save(state)
            return FinalizationResult("running", state.phase, "verified change is being merged/adopted")

        if adoption.status == STATUS_NEEDS_REVIEW:
            return self._needs_review(
                state,
                f"adoption requires review: {getattr(adoption, 'reason', None) or 'unknown reason'}",
            )

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
            return FinalizationResult(
                "running",
                PHASE_RESTART_REQUESTED,
                "waiting for Nexus to resume after the requested service restart",
            )

        state.phase = PHASE_VALIDATING_LIVE
        self._save(state)
        try:
            validation = self._live_validator(approval, experiment)
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
        raw = os.environ.get("NEXUS_FINALIZE_RESTART_SERVICES", "nexus-mailbox,agent-api")
        return [item.strip() for item in raw.split(",") if item.strip()]

    @staticmethod
    def _restart_services(services: list[str]) -> None:
        if not services:
            return
        subprocess.Popen(
            ["systemctl", "restart", *services],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _validate_live(self, approval: Any, experiment: Any) -> dict[str, Any]:
        health_url = os.environ.get("NEXUS_LIVE_HEALTH_URL", _DEFAULT_HEALTH_URL)
        try:
            with urllib.request.urlopen(health_url, timeout=10) as response:  # noqa: S310
                if response.status != 200:
                    raise RuntimeError(f"health endpoint returned HTTP {response.status}")
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"health endpoint unavailable: {exc}") from exc
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
