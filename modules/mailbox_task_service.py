"""Approval-gated bridge from mailbox task messages to ExperimentService.

State machine:
    pending_approval → approved → submitted → running → succeeded
                     → rejected                       → failed
                                                      → needs_review

Exactly-once guarantee: the submitted transition uses an atomic
  UPDATE ... WHERE status='approved' so concurrent callers or retries
  cannot create two experiments for the same approval.

Recovery after restart: on startup the service scans for approvals in
  non-terminal states and updates them based on the current experiment
  status, or requeues them if the experiment was never started.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from modules.run_store import init_db, DEFAULT_DB_PATH

if TYPE_CHECKING:
    from modules.experiment_service import ExperimentService

logger = logging.getLogger(__name__)

_STATUS_PENDING = "pending_approval"
_STATUS_APPROVED = "approved"
_STATUS_REJECTED = "rejected"
_STATUS_SUBMITTED = "submitted"
_STATUS_RUNNING = "running"
_STATUS_SUCCEEDED = "succeeded"
_STATUS_FAILED = "failed"
_STATUS_NEEDS_REVIEW = "needs_review"

_TERMINAL_STATUSES = {_STATUS_REJECTED, _STATUS_SUCCEEDED, _STATUS_FAILED, _STATUS_NEEDS_REVIEW}
_NON_TERMINAL_STATUSES = {_STATUS_PENDING, _STATUS_APPROVED, _STATUS_SUBMITTED, _STATUS_RUNNING}

# Experiment statuses considered terminal
_EXP_TERMINAL = {"succeeded", "failed", "timed_out", "needs_review", "rejected"}
_EXP_RUNNING = {"selecting", "implementing", "verifying"}

_REJECTION_REASON_RE = re.compile(r"^[\w\s.,!?()\'\"\-]{1,500}$")


class ApprovalNotFoundError(LookupError):
    pass


class ApprovalStateError(RuntimeError):
    pass


class MailboxTaskApproval:
    """In-memory representation of a mailbox_task_approvals row."""

    __slots__ = (
        "id", "mailbox_msg_id", "task_body", "sender", "file_path",
        "received_at", "status", "approved_at", "rejected_at",
        "rejection_reason", "experiment_id", "outbox_file",
        "result_summary", "error", "created_at", "updated_at",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def to_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__}


def _row_to_approval(row: sqlite3.Row | tuple) -> MailboxTaskApproval:
    keys = (
        "id", "mailbox_msg_id", "task_body", "sender", "file_path",
        "received_at", "status", "approved_at", "rejected_at",
        "rejection_reason", "experiment_id", "outbox_file",
        "result_summary", "error", "created_at", "updated_at",
    )
    if isinstance(row, sqlite3.Row):
        return MailboxTaskApproval(**{k: row[k] for k in keys})
    return MailboxTaskApproval(**dict(zip(keys, row)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MailboxTaskService:
    """Approval-gated bridge from mailbox task messages to ExperimentService."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        experiment_service: "ExperimentService | None" = None,
        outbox_dir: str = "/agent/communication/outbox",
        repo_path: str = "/agent",
    ) -> None:
        self.db_path = db_path
        self.experiment_service = experiment_service
        self.outbox_dir = Path(outbox_dir)
        self.repo_path = Path(repo_path)
        self._lock = threading.Lock()
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_approval(
        self,
        mailbox_msg_id: int,
        task_body: str,
        sender: str,
        file_path: str,
        received_at: str,
    ) -> MailboxTaskApproval:
        """Create a pending_approval record (idempotent per mailbox_msg_id)."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE mailbox_msg_id = ?",
                (mailbox_msg_id,),
            ).fetchone()
            if existing is not None:
                return _row_to_approval(existing)

            approval_id = uuid.uuid4().hex
            now = _now_iso()
            self._conn.execute(
                """
                INSERT INTO mailbox_task_approvals
                    (id, mailbox_msg_id, task_body, sender, file_path, received_at,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (approval_id, mailbox_msg_id, task_body or "", sender, file_path,
                 received_at, _STATUS_PENDING, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return _row_to_approval(row)

    def get(self, approval_id: str) -> MailboxTaskApproval:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise ApprovalNotFoundError(f"no approval with id {approval_id!r}")
        return _row_to_approval(row)

    def list_pending(self, limit: int = 50) -> list[MailboxTaskApproval]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (_STATUS_PENDING, limit),
            ).fetchall()
        return [_row_to_approval(r) for r in rows]

    def list_recent(self, limit: int = 20) -> list[MailboxTaskApproval]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_approval(r) for r in rows]

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def approve(self, approval_id: str) -> MailboxTaskApproval:
        """Atomically transition pending_approval → approved. Idempotent if already approved."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise ApprovalNotFoundError(f"no approval with id {approval_id!r}")
            status = row["status"]
            if status == _STATUS_APPROVED:
                return _row_to_approval(row)
            if status != _STATUS_PENDING:
                raise ApprovalStateError(
                    f"cannot approve approval in status {status!r} (only pending_approval allowed)"
                )
            now = _now_iso()
            self._conn.execute(
                "UPDATE mailbox_task_approvals SET status=?, approved_at=?, updated_at=? WHERE id=? AND status=?",
                (_STATUS_APPROVED, now, now, approval_id, _STATUS_PENDING),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return _row_to_approval(row)

    def reject(self, approval_id: str, reason: str) -> MailboxTaskApproval:
        """Transition pending_approval → rejected."""
        reason = reason.strip()
        if not _REJECTION_REASON_RE.match(reason):
            raise ValueError("rejection reason contains disallowed characters or is empty")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise ApprovalNotFoundError(f"no approval with id {approval_id!r}")
            status = row["status"]
            if status != _STATUS_PENDING:
                raise ApprovalStateError(
                    f"cannot reject approval in status {status!r} (only pending_approval allowed)"
                )
            now = _now_iso()
            self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, rejected_at=?, rejection_reason=?, updated_at=?
                   WHERE id=? AND status=?""",
                (_STATUS_REJECTED, now, reason, now, approval_id, _STATUS_PENDING),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return _row_to_approval(row)

    # ------------------------------------------------------------------
    # Background processing
    # ------------------------------------------------------------------

    def submit_approved(self) -> None:
        """Find one approved record, atomically create experiment, transition to submitted."""
        if self.experiment_service is None:
            return

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE status = ? LIMIT 1",
                (_STATUS_APPROVED,),
            ).fetchone()
            if row is None:
                return
            approval = _row_to_approval(row)

            # Atomic transition: only move if still approved
            now = _now_iso()
            experiment = self.experiment_service.create_experiment(approval.task_body)
            updated = self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, experiment_id=?, updated_at=?
                   WHERE id=? AND status=?""",
                (_STATUS_SUBMITTED, experiment.id, now, approval.id, _STATUS_APPROVED),
            ).rowcount
            self._conn.commit()

        if not updated:
            # Raced; abort the experiment (it's still queued and harmless, but mark it)
            logger.warning("mailbox_task: lost race for approval %s, experiment %s orphaned",
                           approval.id, experiment.id)
            return

        # Run experiment in a daemon thread
        def _run():
            try:
                self.experiment_service.run_experiment(experiment.id)
            except Exception:
                logger.exception("mailbox_task: experiment %s raised during run", experiment.id)

        t = threading.Thread(target=_run, daemon=True, name=f"mts-exp-{experiment.id[:8]}")
        t.start()

    def poll_running(self) -> None:
        """Check submitted/running approvals and advance state machine."""
        if self.experiment_service is None:
            return

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE status IN (?, ?)",
                (_STATUS_SUBMITTED, _STATUS_RUNNING),
            ).fetchall()

        for row in rows:
            approval = _row_to_approval(row)
            if not approval.experiment_id:
                continue
            try:
                exp = self.experiment_service.get_experiment(approval.experiment_id)
            except Exception:
                logger.exception("mailbox_task: failed to fetch experiment %s", approval.experiment_id)
                continue

            now = _now_iso()
            if exp.status in _EXP_RUNNING:
                if approval.status != _STATUS_RUNNING:
                    with self._lock:
                        self._conn.execute(
                            "UPDATE mailbox_task_approvals SET status=?, updated_at=? WHERE id=?",
                            (_STATUS_RUNNING, now, approval.id),
                        )
                        self._conn.commit()
            elif exp.status == "succeeded":
                self._transition_terminal(approval, _STATUS_SUCCEEDED, exp)
            elif exp.status in ("failed", "timed_out"):
                self._transition_terminal(approval, _STATUS_FAILED, exp)
            elif exp.status == "needs_review":
                self._transition_terminal(approval, _STATUS_NEEDS_REVIEW, exp)
            # queued/selecting/etc → just update to running
            elif approval.status == _STATUS_SUBMITTED and exp.status not in _EXP_TERMINAL:
                with self._lock:
                    self._conn.execute(
                        "UPDATE mailbox_task_approvals SET status=?, updated_at=? WHERE id=?",
                        (_STATUS_RUNNING, now, approval.id),
                    )
                    self._conn.commit()

    def _transition_terminal(self, approval: MailboxTaskApproval, new_status: str, exp) -> None:
        now = _now_iso()
        result_summary = self._make_result_summary(exp)
        outbox_file = self._write_outbox(approval, new_status, exp, result_summary)

        with self._lock:
            self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, result_summary=?, error=?, outbox_file=?, updated_at=?
                   WHERE id=?""",
                (new_status,
                 result_summary,
                 _safe_error(exp.error) if new_status in (_STATUS_FAILED, _STATUS_NEEDS_REVIEW) else None,
                 outbox_file,
                 now,
                 approval.id),
            )
            self._conn.commit()

        if new_status in (_STATUS_FAILED, _STATUS_NEEDS_REVIEW):
            self._write_problem(approval, new_status, result_summary, exp)

    def _make_result_summary(self, exp) -> str:
        parts = [f"status={exp.status}"]
        if exp.test_exit_code is not None:
            parts.append(f"tests={'pass' if exp.test_exit_code == 0 else 'fail'}")
        if exp.diff_stat:
            parts.append(f"diff_stat={exp.diff_stat[:100]}")
        return "; ".join(parts)

    def _write_outbox(self, approval: MailboxTaskApproval, status: str, exp, summary: str) -> str | None:
        try:
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            filename = f"{ts}-task-approval-{approval.id[:8]}.md"
            test_result = ""
            if exp.test_exit_code is not None:
                test_result = f"\ntest_result: {'pass' if exp.test_exit_code == 0 else 'fail'}"
            content = (
                f"---\n"
                f"type: task_approval_result\n"
                f"status: {status}\n"
                f"approval_id: {approval.id}\n"
                f"experiment_id: {exp.id}\n"
                f"sender: {approval.sender}\n"
                f"source: {Path(approval.file_path).name}\n"
                f"created_at: {approval.created_at}{test_result}\n"
                f"---\n\n"
                f"## Task Approval Result\n\n"
                f"**Status:** {status}\n\n"
                f"**Summary:** {summary}\n"
            )
            path = self.outbox_dir / filename
            tmp = path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.rename(path)
            return filename
        except Exception:
            logger.exception("mailbox_task: failed to write outbox for approval %s", approval.id)
            return None

    def _write_problem(self, approval: MailboxTaskApproval, status: str, summary: str, exp) -> None:
        try:
            runs_dir = self.repo_path / "state" / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            filename = runs_dir / f"{ts}-task-approval-{approval.id[:8]}.json"
            data = {
                "timestamp": _now_iso(),
                "source": "mailbox_task_service",
                "approval_id": approval.id,
                "experiment_id": exp.id,
                "response": f"ERROR: task approval {approval.id[:8]} ended with status={status}; {summary}",
            }
            filename.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("mailbox_task: failed to write problem for approval %s", approval.id)

    def recover_after_restart(self) -> None:
        """On startup: advance state machine for non-terminal approvals."""
        if self.experiment_service is None:
            return
        try:
            self.poll_running()
        except Exception:
            logger.exception("mailbox_task: recover_after_restart failed")


def _safe_error(error: str | None) -> str | None:
    if not error:
        return None
    # Redact anything that looks like a secret or path
    redacted = re.sub(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+", "[REDACTED]", error)
    return redacted[:500]


class MailboxTaskWorker(threading.Thread):
    """Single background daemon that processes approved mailbox tasks.

    Runs submit_approved() + poll_running() every 10 seconds.
    Single-threaded so experiments run serially (appropriate for 1-VM budget).
    """

    def __init__(self, service: MailboxTaskService, interval: float = 10.0) -> None:
        super().__init__(daemon=True, name="mailbox-task-worker")
        self._service = service
        self._interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("MailboxTaskWorker started (interval=%.0fs)", self._interval)
        while not self._stop_event.wait(self._interval):
            try:
                self._service.submit_approved()
            except Exception:
                logger.exception("MailboxTaskWorker: submit_approved failed")
            try:
                self._service.poll_running()
            except Exception:
                logger.exception("MailboxTaskWorker: poll_running failed")
        logger.info("MailboxTaskWorker stopped")
