"""Approval-gated bridge from mailbox task messages to Nexus workflows."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from modules.run_store import init_db, DEFAULT_DB_PATH
from modules.outbox_publisher import PUBLISH_PENDING_WRITE, PUBLISH_WRITTEN, RepoLock
from modules.task_intent import (
    ACTION_CLARIFICATION,
    ACTION_COMMAND,
    ACTION_MISSING_CAPABILITY,
    ACTION_NEW_EXPERIMENT,
    ACTION_READ_ONLY,
    ACTION_RESUME_EXPERIMENT,
    classify_task_intent,
)

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
_EXP_TERMINAL = {"succeeded", "failed", "timed_out", "needs_review", "rejected"}
_EXP_RUNNING = {"selecting", "implementing", "verifying"}
_ROUTING_ONLY_ACTIONS = {
    ACTION_CLARIFICATION,
    ACTION_MISSING_CAPABILITY,
    ACTION_READ_ONLY,
    ACTION_COMMAND,
}

_REJECTION_REASON_RE = re.compile(r"^[\w\s.,!?()\'\"\-]{1,500}$")


class ApprovalNotFoundError(LookupError):
    pass


class ApprovalStateError(RuntimeError):
    pass


class MailboxTaskApproval:
    __slots__ = (
        "id", "mailbox_msg_id", "task_body", "sender", "file_path",
        "received_at", "status", "approved_at", "rejected_at",
        "rejection_reason", "experiment_id", "outbox_file",
        "publish_status", "publish_commit_sha",
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
        "publish_status", "publish_commit_sha",
        "result_summary", "error", "created_at", "updated_at",
    )
    if isinstance(row, sqlite3.Row):
        return MailboxTaskApproval(**{k: row[k] for k in keys if k in row.keys()})
    return MailboxTaskApproval(**dict(zip(keys, row)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MailboxTaskService:
    """Approval-gated bridge from mailbox task messages to Nexus workflows."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        experiment_service: "ExperimentService | None" = None,
        outbox_dir: str = "/agent/communication/outbox",
        repo_path: str = "/agent",
        mailbox_config: "Any | None" = None,
    ) -> None:
        self.db_path = db_path
        self.experiment_service = experiment_service
        if mailbox_config is not None:
            self.outbox_dir = mailbox_config.outbox_dir
            self.repo_path = mailbox_config.code_repo_path
            self.mailbox_repo_path = Path(mailbox_config.mailbox_repo_path)
        else:
            self.outbox_dir = Path(outbox_dir)
            self.repo_path = Path(repo_path)
            self.mailbox_repo_path = self.repo_path
        self._lock = threading.Lock()
        self._repo_lock = RepoLock(self.mailbox_repo_path)
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn

    def close(self) -> None:
        self._conn.close()

    def create_approval(
        self,
        mailbox_msg_id: int,
        task_body: str,
        sender: str,
        file_path: str,
        received_at: str,
    ) -> MailboxTaskApproval:
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
        logger.info(
            "mailbox_task: received task approval=%s from sender=%s (pending_approval)",
            approval_id[:8], sender,
        )
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

    def approve(self, approval_id: str) -> MailboxTaskApproval:
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
        logger.info("mailbox_task: approval=%s approved", approval_id[:8])
        return _row_to_approval(row)

    def reject(self, approval_id: str, reason: str) -> MailboxTaskApproval:
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
        logger.info("mailbox_task: approval=%s rejected reason=%s", approval_id[:8], reason)
        return _row_to_approval(row)

    def submit_approved(self) -> None:
        """Route one approved free-form task before selecting an execution pipeline."""
        if self.experiment_service is None:
            return

        # Do not hold the sqlite lock while the local model is thinking.
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE status = ? LIMIT 1",
                (_STATUS_APPROVED,),
            ).fetchone()
        if row is None:
            return
        approval = _row_to_approval(row)

        intent = classify_task_intent(approval.task_body, self.experiment_service)
        logger.info(
            "mailbox_task: approval=%s workflow=%s source=%s reason=%s",
            approval.id[:8], intent.action, intent.source, intent.reason[:300],
        )

        if intent.action in _ROUTING_ONLY_ACTIONS:
            self._finish_routing_outcome(approval, intent)
            return

        is_resume = intent.action == ACTION_RESUME_EXPERIMENT and intent.experiment_id is not None
        if intent.action not in (ACTION_NEW_EXPERIMENT, ACTION_RESUME_EXPERIMENT):
            self._finish_routing_outcome(
                approval,
                SimpleNamespace(
                    action=ACTION_MISSING_CAPABILITY,
                    message=f"Unsupported workflow decision: {intent.action}",
                    reason=intent.reason,
                    source=intent.source,
                ),
            )
            return

        # Re-check approval atomically after model inference, then allocate work.
        with self._lock:
            current = self._conn.execute(
                "SELECT status FROM mailbox_task_approvals WHERE id = ?", (approval.id,)
            ).fetchone()
            if current is None or current["status"] != _STATUS_APPROVED:
                return

            if is_resume:
                experiment = self.experiment_service.get_experiment(intent.experiment_id)
            else:
                experiment = self.experiment_service.create_experiment(approval.task_body)

            now = _now_iso()
            updated = self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, experiment_id=?, updated_at=?
                   WHERE id=? AND status=?""",
                (_STATUS_SUBMITTED, experiment.id, now, approval.id, _STATUS_APPROVED),
            ).rowcount
            self._conn.commit()

        if not updated:
            if is_resume:
                logger.warning(
                    "mailbox_task: lost race for approval %s while resuming experiment %s",
                    approval.id, experiment.id,
                )
                return
            logger.warning(
                "mailbox_task: lost race for approval %s, experiment %s orphaned",
                approval.id, experiment.id,
            )
            try:
                elog = self.experiment_service.get_experiment_logger(
                    experiment.id, mailbox_task_id=approval.id
                )
                elog.warning(
                    "mailbox", "mailbox_task_submit_race_lost",
                    f"lost race for approval {approval.id[:8]}, experiment orphaned",
                )
            except Exception:
                logger.exception("mailbox_task: failed to log race-lost for approval %s", approval.id)
            return

        try:
            elog = self.experiment_service.get_experiment_logger(
                experiment.id, mailbox_task_id=approval.id
            )
            elog.info(
                "mailbox",
                "mailbox_task_resumed" if is_resume else "mailbox_task_submitted",
                (
                    f"mailbox task approval {approval.id[:8]} resumed experiment "
                    f"{experiment.id[:8]} ({intent.reason})"
                    if is_resume
                    else f"mailbox task approval {approval.id[:8]} submitted as experiment "
                         f"(router={intent.source})"
                ),
            )
        except Exception:
            logger.exception("mailbox_task: failed to log submission for approval %s", approval.id)

        def _run():
            try:
                if is_resume:
                    self.experiment_service.resume_experiment(experiment.id)
                else:
                    self.experiment_service.run_experiment(experiment.id)
            except Exception:
                logger.exception("mailbox_task: experiment %s raised during run", experiment.id)

        t = threading.Thread(target=_run, daemon=True, name=f"mts-exp-{experiment.id[:8]}")
        t.start()

    def _finish_routing_outcome(self, approval: MailboxTaskApproval, intent: Any) -> None:
        """Persist a non-experiment router result and make it visible in outbox/API.

        Missing tools and clarification are valid outcomes of orchestration, not
        reasons to force the task through an unrelated experiment pipeline.
        Read-only/command decisions are also surfaced explicitly until a matching
        task execution primitive is injected into this bridge.
        """
        action = str(intent.action)
        message = (str(getattr(intent, "message", "") or "").strip()
                   or str(getattr(intent, "reason", "") or "").strip()
                   or f"Nexus selected workflow {action}.")
        summary = f"routing_action={action}; {message[:1500]}"
        pseudo_exp = SimpleNamespace(
            id="none",
            status=action,
            test_exit_code=None,
            diff_stat=None,
            error=message,
        )
        self._write_outbox(approval, _STATUS_NEEDS_REVIEW, pseudo_exp, summary)
        with self._lock:
            updated = self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, experiment_id=NULL, result_summary=?, error=?, updated_at=?
                   WHERE id=? AND status=?""",
                (_STATUS_NEEDS_REVIEW, summary, _safe_error(message), _now_iso(),
                 approval.id, _STATUS_APPROVED),
            ).rowcount
            self._conn.commit()
        if updated:
            logger.info(
                "mailbox_task: approval=%s completed with routing outcome=%s",
                approval.id[:8], action,
            )

    def poll_running(self) -> None:
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
                    self._log_mailbox_event(
                        approval, "mailbox", "mailbox_task_running",
                        f"mailbox task approval {approval.id[:8]} now running (experiment status={exp.status})",
                    )
            elif exp.status == "succeeded":
                self._transition_terminal(approval, _STATUS_SUCCEEDED, exp)
            elif exp.status in ("failed", "timed_out"):
                self._transition_terminal(approval, _STATUS_FAILED, exp)
            elif exp.status == "needs_review":
                self._transition_terminal(approval, _STATUS_NEEDS_REVIEW, exp)
            elif approval.status == _STATUS_SUBMITTED and exp.status not in _EXP_TERMINAL:
                with self._lock:
                    self._conn.execute(
                        "UPDATE mailbox_task_approvals SET status=?, updated_at=? WHERE id=?",
                        (_STATUS_RUNNING, now, approval.id),
                    )
                    self._conn.commit()
                self._log_mailbox_event(
                    approval, "mailbox", "mailbox_task_running",
                    f"mailbox task approval {approval.id[:8]} now running (experiment status={exp.status})",
                )

    def _log_mailbox_event(
        self, approval: MailboxTaskApproval, phase: str, event: str,
        message: str, level: str = "info"
    ) -> None:
        if self.experiment_service is None or not approval.experiment_id:
            return
        try:
            elog = self.experiment_service.get_experiment_logger(
                approval.experiment_id, mailbox_task_id=approval.id
            )
            getattr(elog, level)(phase, event, message)
        except Exception:
            logger.exception("mailbox_task: failed to log event %s for approval %s", event, approval.id)

    def _transition_terminal(self, approval: MailboxTaskApproval, new_status: str, exp) -> None:
        now = _now_iso()
        result_summary = self._make_result_summary(exp)
        self._write_outbox(approval, new_status, exp, result_summary)
        with self._lock:
            self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, result_summary=?, error=?, updated_at=?
                   WHERE id=?""",
                (new_status, result_summary,
                 _safe_error(exp.error) if new_status in (_STATUS_FAILED, _STATUS_NEEDS_REVIEW) else None,
                 now, approval.id),
            )
            self._conn.commit()
        self._log_mailbox_event(
            approval, "mailbox", "mailbox_task_terminal",
            f"mailbox task approval {approval.id[:8]} reached terminal status={new_status}; {result_summary}",
            level="warning" if new_status in (_STATUS_FAILED, _STATUS_NEEDS_REVIEW) else "info",
        )
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
            rel_path = str(path.relative_to(self.mailbox_repo_path))
            with self._repo_lock:
                with self._lock:
                    self._conn.execute(
                        """UPDATE mailbox_task_approvals
                           SET outbox_file=?, outbox_content=?, publish_status=?, updated_at=?
                           WHERE id=?""",
                        (rel_path, content, PUBLISH_PENDING_WRITE, _now_iso(), approval.id),
                    )
                    self._conn.commit()
                tmp = path.with_suffix(".tmp")
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(path)
                with self._lock:
                    self._conn.execute(
                        """UPDATE mailbox_task_approvals
                           SET publish_status=?, updated_at=? WHERE id=?""",
                        (PUBLISH_WRITTEN, _now_iso(), approval.id),
                    )
                    self._conn.commit()
            self._log_mailbox_event(
                approval, "mailbox", "outbox_written",
                f"outbox file {filename} written for approval {approval.id[:8]} (status={status})",
            )
            return rel_path
        except Exception:
            logger.exception("mailbox_task: failed to write outbox for approval %s", approval.id)
            self._log_mailbox_event(
                approval, "mailbox", "outbox_write_failed",
                f"failed to write outbox for approval {approval.id[:8]}", level="error",
            )
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
            self._log_mailbox_event(
                approval, "mailbox", "problem_file_written",
                f"problem file written for approval {approval.id[:8]} (status={status})",
                level="warning",
            )
        except Exception:
            logger.exception("mailbox_task: failed to write problem for approval %s", approval.id)
            self._log_mailbox_event(
                approval, "mailbox", "problem_write_failed",
                f"failed to write problem file for approval {approval.id[:8]}", level="error",
            )

    def recover_after_restart(self) -> None:
        if self.experiment_service is None:
            return
        try:
            self.poll_running()
        except Exception:
            logger.exception("mailbox_task: recover_after_restart failed")


def _safe_error(error: str | None) -> str | None:
    if not error:
        return None
    redacted = re.sub(
        r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+",
        "[REDACTED]", error,
    )
    return redacted[:500]


class MailboxTaskWorker(threading.Thread):
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
