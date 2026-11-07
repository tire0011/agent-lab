"""Worktree cleanup methods of :class:`ExperimentService`."""

from __future__ import annotations

import logging
import shutil
import subprocess

from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.redaction import sanitized_reason
from modules.run_store import insert_activity

from ..git_utils import _run_git
from ..models import _TERMINAL_STATUSES
from ..text_utils import _utcnow_iso
from .core import (
    WorktreeCleanupResult,
    _ADOPTION_TERMINAL_STATUSES,
    _parse_utc,
    worktree_retention_hours,
)


logger = logging.getLogger("modules.experiment_service")

class CleanupMixin:
    """Worktree cleanup methods of :class:`ExperimentService`."""

    def cleanup_worktree(self, experiment_id: str) -> None:
        """Explicitly remove an experiment's git worktree and branch.

        Never called automatically by ``run_experiment`` - the worktree is kept after an
        experiment finishes so a human can inspect it. Safe to call multiple times.
        """
        elog = self.get_experiment_logger(experiment_id)
        experiment = self.get_experiment(experiment_id)
        elog.info("cleanup", "worktree_cleanup_started", "Cleaning up experiment worktree and branch")
        if experiment.worktree_path:
            _run_git(
                ["worktree", "remove", "--force", experiment.worktree_path],
                cwd=self.repo_root,
                elog=elog, phase="cleanup",
            )
            shutil.rmtree(experiment.worktree_path, ignore_errors=True)
        if experiment.branch_name:
            _run_git(["branch", "-D", experiment.branch_name], cwd=self.repo_root, elog=elog, phase="cleanup")
        elog.info("cleanup", "worktree_cleanup_done", "Experiment worktree and branch cleanup finished")


    def cleanup_expired_worktrees(
        self, retention_hours: float | None = None, *, now: datetime | None = None
    ) -> WorktreeCleanupResult:
        """Remove worktrees of terminal experiments that ended over ``retention_hours`` ago.

        ``retention_hours`` defaults to ``NEXUS_EXPERIMENT_WORKTREE_RETENTION_HOURS``
        (see ``worktree_retention_hours``); ``0`` disables the pass. Only the worktree
        directory goes - the experiment row, its JSONL log and its branch are kept, so
        the record stays complete and committed work stays recoverable.

        Never removed while the experiment is non-terminal, has no ``ended_at``, ended
        within the window, has an unfinished adoption, or has a path outside
        ``worktrees_root``. Already-missing directories are skipped silently so repeated
        passes are no-ops; a failure on one worktree is recorded and does not stop the others.
        """
        hours = worktree_retention_hours() if retention_hours is None else max(0.0, float(retention_hours))
        result = WorktreeCleanupResult(retention_hours=hours)
        if hours <= 0:
            return result
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ended_at, worktree_path FROM experiments "
                f"WHERE status IN ({', '.join('?' * len(_TERMINAL_STATUSES))}) "
                "AND worktree_path IS NOT NULL AND worktree_path != ''",
                _TERMINAL_STATUSES,
            ).fetchall()
        for row in rows:
            ended_at = _parse_utc(row["ended_at"])
            if ended_at is None or ended_at > cutoff:
                continue
            path = Path(row["worktree_path"])
            if not path.exists() and not path.is_symlink():
                continue
            try:
                if self._remove_expired_worktree(row["id"], path, hours):
                    result.removed.append(row["id"])
            except Exception as exc:  # noqa: BLE001 - one bad worktree must not stop the pass
                reason = sanitized_reason(f"{type(exc).__name__}: {exc}")
                result.errors[row["id"]] = reason
                self._record_worktree_cleanup_error(row["id"], str(path), reason)
        return result


    def _remove_expired_worktree(self, experiment_id: str, path: Path, hours: float) -> bool:
        """Remove one expired worktree; ``False`` if it turned out to be still needed."""
        terminal = ", ".join("?" * len(_TERMINAL_STATUSES))
        finished = ", ".join("?" * len(_ADOPTION_TERMINAL_STATUSES))
        with self._lock:
            # Re-checked right before deleting: an adoption may have been queued since the
            # candidates were listed.
            still_expired = self._conn.execute(
                f"SELECT 1 FROM experiments e WHERE e.id = ? AND e.status IN ({terminal}) "
                "AND NOT EXISTS (SELECT 1 FROM experiment_adoptions a "
                f"WHERE a.experiment_id = e.id AND a.status NOT IN ({finished}))",
                (experiment_id, *_TERMINAL_STATUSES, *_ADOPTION_TERMINAL_STATUSES),
            ).fetchone()
        if still_expired is None:
            return False

        root = Path(self.worktrees_root).resolve()
        if path.is_symlink() or root not in path.resolve().parents:
            raise ValueError(f"refusing to remove {path}: not a directory inside {root}")

        elog = self.get_experiment_logger(experiment_id)
        try:
            # Unregister the worktree from the main repository; the rmtree below is what actually
            # guarantees the directory is gone.
            _run_git(
                ["worktree", "remove", "--force", str(path)],
                cwd=self.repo_root, elog=elog, phase="cleanup",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("git worktree remove failed for %s: %s", path, exc)
        if path.exists():
            shutil.rmtree(path)

        message = f"Removed worktree {path} after the {hours:g} h retention window"
        with self._lock, self._conn:
            insert_activity(
                self._conn,
                timestamp=_utcnow_iso(),
                entity_type="experiment",
                entity_id=experiment_id,
                event="worktree_removed",
                message=message,
            )
        logger.info("experiment %s: %s", experiment_id, message)
        elog.info("cleanup", "worktree_retention_removed", message, worktree_path=str(path))
        return True


    def _record_worktree_cleanup_error(self, experiment_id: str, path: str, reason: str) -> None:
        logger.warning("experiment %s: could not remove expired worktree %s: %s", experiment_id, path, reason)
        try:
            with self._lock, self._conn:
                insert_activity(
                    self._conn,
                    timestamp=_utcnow_iso(),
                    entity_type="experiment",
                    entity_id=experiment_id,
                    event="worktree_cleanup_failed",
                    message=reason,
                )
            self.get_experiment_logger(experiment_id).error(
                "cleanup", "worktree_retention_failed",
                "Could not remove expired worktree", worktree_path=path, reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 - recording is best effort
            logger.warning("could not record worktree cleanup failure for %s: %s", experiment_id, exc)

