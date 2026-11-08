"""State, records and lifecycle methods of :class:`ExperimentAdoptionService`."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid

from pathlib import Path
from typing import Any

from modules.experiment_service import _utcnow_iso
from modules.github_work_tracking import GitHubWorkTrackingService
from modules.mailbox import MailboxLock
from modules.redaction import (
    bounded_lines,
    sanitized_reason,
)
from modules.run_store import (
    DEFAULT_DB_PATH,
    init_db,
)

from modules.experiment_adoption_parts.core import (
    ADOPTION_THREAD_PREFIX,
    ExperimentAdoption,
    ExperimentAdoptionError,
    STATUS_ADOPTED,
    STATUS_ADOPTING,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_READY_FOR_REVIEW,
    StaleFailureAssessment,
    _DEFAULT_GIT_TIMEOUT_SECONDS,
    _DEFAULT_LOCK_PATH,
    _DEFAULT_TARGET_BRANCH,
    _MAX_RECOVERY_ATTEMPTS,
    _MAX_STALE_RETRIES,
    _NullExperimentLogger,
    _row_to_adoption,
    _safe_list,
)

logger = logging.getLogger("modules.experiment_adoption_service")

class LifecycleMixin:
    """State, records and lifecycle methods of :class:`ExperimentAdoptionService`."""

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
        # Adoption threads use the shared SQLite connection and temporary worktrees;
        # closing those while a thread is active can corrupt sqlite or crash the
        # interpreter, so join all workers first. A thread may be inside a verification
        # run bounded by the watchdog's absolute safety cap, so wait for that, not a
        # shorter fixed timeout.
        for thread in list(self._threads.values()):
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


    def start_adoption(
        self,
        experiment_id: str,
        *,
        superseded: StaleFailureAssessment | None = None,
    ) -> ExperimentAdoption:
        """Queue adoption for a verified experiment.

        Restarting a *failed* adoption reuses the same row, so the replaced failure
        is always archived to the durable attempt history first, in the same
        transaction (#136). ``superseded`` is the caller's assessment that current
        evidence overruled the recorded failure; only such an evidence-granted
        restart consumes ``stale_retry_attempts`` - ordinary retries must not spend
        that budget (#136).
        """
        existing = self.get(experiment_id)
        # A needs_review row is reopened only with an assessment saying its blocker
        # has cleared (#171); otherwise it stays as terminal as before.
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
        # Fresh start resets the budget, an ordinary retry carries it unchanged,
        # only an evidence-granted retry spends one unit.
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

        # One transaction: archive the previous failure, reset the row to queued
        # and count the attempt before any worker can observe an in-between state;
        # a crash between separate writes could lose the history or allow an
        # unbounded retry.
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


    def recover_after_restart(self) -> list[str]:
        """Resume adoptions that a process restart interrupted mid-flight.

        A restart is a resumable boundary, not a failure (refresh restarts Nexus
        deliberately): an adoption still queued/adopting when the process died is
        reconstructed from durable evidence. Per interrupted adoption: if GitHub
        already shows the PR merged, record ``adopted`` with the real merge commit
        and never merge again; otherwise resume the same adoption record by
        re-running the pipeline; if evidence is unavailable or the attempt budget
        is spent, mark ``needs_review`` naming the obstacle. Returns the experiment
        ids it acted on.
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
                # Adoptions are serialized by design, so several interrupted at
                # once is already an inconsistency: recording already-merged ones
                # is always safe, but only the oldest remaining record is resumed -
                # two pipelines would fight over the adoption lock.
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
            # The merge landed before the restart; finishing it again would be a
            # duplicate. Persist what actually happened instead.
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


    def failure_history(self, experiment_id: str) -> list[dict[str, Any]]:
        """Superseded adoption failures, oldest first - the row itself is reused
        across retries, so this is where a replaced failure stays readable."""
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


    def _elog(self, experiment_id: str):
        """An ExperimentLogger for this experiment, or a no-op stand-in: diagnostics
        must never break the lifecycle they describe, so a logger-factory failure
        (unsafe id, unusable log dir) degrades to silence rather than propagating."""
        try:
            return self.experiment_service.get_experiment_logger(experiment_id)
        except Exception:  # noqa: BLE001 - logging is never worth a lifecycle failure
            logger.warning("no experiment logger for %s", experiment_id, exc_info=True)
            return _NullExperimentLogger()
