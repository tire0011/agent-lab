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
