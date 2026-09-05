from __future__ import annotations

import logging
from typing import Any

from modules.task_finalization import FinalizationResult, TaskFinalizationCoordinator
from . import task_service as _base

logger = logging.getLogger(__name__)


class FinalizingMailboxTaskService(_base.MailboxTaskService):
    """Mailbox task service whose development success boundary is live validation."""

    def __init__(self, *args: Any, finalization_coordinator: Any | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.finalization_coordinator = finalization_coordinator
        self._owns_finalization_coordinator = finalization_coordinator is None
        if self.finalization_coordinator is None and self.experiment_service is not None:
            self.finalization_coordinator = TaskFinalizationCoordinator(
                experiment_service=self.experiment_service,
                github_work_tracking=self.github_work_tracking,
                db_path=self.db_path,
                repo_root=str(self.repo_path),
            )

    def close(self) -> None:
        if self._owns_finalization_coordinator and self.finalization_coordinator is not None:
            try:
                self.finalization_coordinator.close()
            except Exception:
                logger.exception("mailbox_task: failed closing finalization coordinator")
        super().close()

    def poll_running(self) -> None:
        if self.experiment_service is None:
            return
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mailbox_task_approvals WHERE status IN (?, ?) AND experiment_id IS NOT NULL",
                (_base._STATUS_SUBMITTED, _base._STATUS_RUNNING),
            ).fetchall()
        for row in rows:
            approval = _base._row_to_approval(row)
            try:
                exp = self.experiment_service.get_experiment(approval.experiment_id)
            except Exception:
                logger.exception(
                    "mailbox_task: failed to fetch experiment %s", approval.experiment_id
                )
                continue

            if exp.status in _base._EXP_RUNNING or (
                approval.status == _base._STATUS_SUBMITTED and exp.status not in _base._EXP_TERMINAL
            ):
                self._mark_running(approval.id)
                continue

            if exp.status == "succeeded":
                self._advance_verified(approval, exp)
            elif exp.status in ("failed", "timed_out"):
                self._transition_terminal(approval, _base._STATUS_FAILED, exp)
            elif exp.status == "needs_review":
                self._transition_terminal(approval, _base._STATUS_NEEDS_REVIEW, exp)

    def _advance_verified(self, approval: Any, exp: Any) -> None:
        if self.finalization_coordinator is None:
            self._transition_finalization_terminal(
                approval,
                exp,
                _base._STATUS_NEEDS_REVIEW,
                "verification passed, but autonomous finalization is unavailable",
            )
            return
        try:
            result: FinalizationResult = self.finalization_coordinator.advance(approval, exp)
        except Exception as exc:
            logger.exception("mailbox_task: finalization failed for %s", exp.id)
            self._transition_finalization_terminal(
                approval,
                exp,
                _base._STATUS_NEEDS_REVIEW,
                f"autonomous finalization failed unexpectedly: {_base._safe_error(str(exc)) or type(exc).__name__}",
            )
            return

        task_log = self._task_logger(approval.id)
        if result.status == "running":
            self._mark_running(approval.id, summary=f"finalization={result.phase}; {result.summary}")
            task_log.info(
                "finalization",
                "finalization_progress",
                result.summary,
                experiment_id=exp.id,
                finalization_phase=result.phase,
            )
            return
        if result.status == "succeeded":
            task_log.info(
                "finalization",
                "live_validation_passed",
                result.summary,
                experiment_id=exp.id,
                finalization_phase=result.phase,
            )
            self._transition_finalization_terminal(
                approval,
                exp,
                _base._STATUS_SUCCEEDED,
                f"finalization={result.phase}; {result.summary}",
            )
            return

        task_log.warning(
            "finalization",
            "finalization_needs_review",
            result.summary,
            experiment_id=exp.id,
            finalization_phase=result.phase,
        )
        self._transition_finalization_terminal(
            approval,
            exp,
            _base._STATUS_NEEDS_REVIEW,
            f"finalization={result.phase}; {result.summary}",
        )

    def _mark_running(self, approval_id: str, *, summary: str | None = None) -> None:
        with self._lock:
            if summary is None:
                self._conn.execute(
                    "UPDATE mailbox_task_approvals SET status=?, updated_at=? WHERE id=?",
                    (_base._STATUS_RUNNING, _base._now_iso(), approval_id),
                )
            else:
                self._conn.execute(
                    """UPDATE mailbox_task_approvals
                       SET status=?, result_summary=?, error=NULL, updated_at=? WHERE id=?""",
                    (_base._STATUS_RUNNING, summary[:8000], _base._now_iso(), approval_id),
                )
            self._conn.commit()

    def _transition_finalization_terminal(
        self,
        approval: Any,
        exp: Any,
        status: str,
        finalization_summary: str,
    ) -> None:
        summary = f"{self._make_result_summary(exp)}; {finalization_summary}"
        self._write_result_outbox(approval, status, summary, exp)
        error = finalization_summary if status == _base._STATUS_NEEDS_REVIEW else None
        with self._lock:
            self._conn.execute(
                """UPDATE mailbox_task_approvals
                   SET status=?, result_summary=?, error=?, updated_at=? WHERE id=?""",
                (
                    status,
                    summary[:8000],
                    _base._safe_error(error),
                    _base._now_iso(),
                    approval.id,
                ),
            )
            self._conn.commit()
        if status == _base._STATUS_NEEDS_REVIEW:
            self._write_problem(approval, status, summary, exp)


def install() -> None:
    """Install the finalizing service behind the existing import surface.

    `api.py` and older callers import `MailboxTaskService` from
    `modules.mailbox.task_service`.  Keep that stable public path while the
    lifecycle extension lives in its own focused module.
    """

    if _base.MailboxTaskService is not FinalizingMailboxTaskService:
        _base.MailboxTaskService = FinalizingMailboxTaskService
