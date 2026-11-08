"""Stall sweep methods of :class:`ExperimentService`."""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import time

from datetime import datetime, timezone
from pathlib import Path

from modules.central_logging import _tail_read_lines
from modules.coding_worker import agent_watchdog_limits

from ..models import STATUS_FAILED, STATUS_VERIFYING
from .core import (
    _DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS,
    _STALLED_ERROR_PREFIX,
    _STALL_LOG_TAIL_BYTES,
    _STALL_SWEEP_STATUSES,
    _STALL_TERMINATE_GRACE_SECONDS,
    _parse_utc,
    stall_grace_seconds,
)


logger = logging.getLogger("modules.experiment_service")

class StallSweepMixin:
    """Stall sweep methods of :class:`ExperimentService`."""

    def _phase_hard_limit_seconds(self, status: str) -> float:
        """Longest a phase may legitimately go without a single log entry.

        Verification runs the test suite under its own safety cap and may then start a
        repair agent, so it gets the larger of both.
        """
        # The implementation timeout is the phase's upper bound, not a floor: a shorter
        # coding-agent cap must make a silent phase sweep-eligible sooner.
        agent_cap = min(agent_watchdog_limits()[1], float(_DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS))
        if status == STATUS_VERIFYING:
            return max(agent_cap, float(self._get_test_watchdog_config().safety_cap_seconds))
        return agent_cap


    def _newest_log_entry(self, experiment_id: str) -> tuple[datetime | None, str]:
        """Timestamp and event of the newest entry in the experiment's JSONL log."""
        newest: datetime | None = None
        event = ""
        # A rotation leaves the active file briefly empty; the previous one then holds the
        # newest entry.
        for name in (f"{experiment_id}.jsonl", f"{experiment_id}-1.jsonl"):
            path = Path(self.experiment_log_dir) / name
            try:
                lines = _tail_read_lines(path, _STALL_LOG_TAIL_BYTES)
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                stamp = _parse_utc(entry.get("timestamp"))
                if stamp is not None and (newest is None or stamp >= newest):
                    newest, event = stamp, str(entry.get("event") or "")
            if newest is not None:
                break
        return newest, event


    def _last_progress(self, experiment_id: str, row: sqlite3.Row) -> tuple[datetime | None, str]:
        """Newest sign of life: log entry, activity event or lifecycle stamp."""
        seen: list[tuple[datetime, str]] = []
        log_at, log_event = self._newest_log_entry(experiment_id)
        if log_at is not None:
            seen.append((log_at, log_event or "log_entry"))
        with self._lock:
            activity = self._conn.execute(
                "SELECT timestamp, event FROM activity_log "
                "WHERE entity_type = 'experiment' AND entity_id = ? ORDER BY id DESC LIMIT 1",
                (experiment_id,),
            ).fetchone()
        if activity is not None:
            stamp = _parse_utc(activity[0])
            if stamp is not None:
                seen.append((stamp, f"activity:{activity[1]}"))
        for column in ("created_at", "implementation_started_at", "implementation_ended_at"):
            stamp = _parse_utc(row[column])
            if stamp is not None:
                seen.append((stamp, column))
        if not seen:
            return None, ""
        return max(seen, key=lambda item: item[0])


    def _stop_stalled_worker(self, experiment_id: str, pgid: int | None) -> str:
        """Stop the worker's process group; returns what happened, never raises."""
        with self._process_lock:
            process = self._active_processes.pop(experiment_id, None)
        if process is not None:
            try:
                self._terminate_process(process)
            except (OSError, subprocess.SubprocessError) as exc:
                return f"terminate failed: {type(exc).__name__}"
            return "terminated"
        if not pgid:
            return "no worker recorded"
        pgid = int(pgid)
        # Never signal this process's own group, init, or anything below it.
        if pgid <= 1 or pgid == os.getpgrp():
            return "refused to signal own process group"
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return "worker no longer running"
        except PermissionError:
            return "not permitted to signal worker"
        deadline = time.monotonic() + _STALL_TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return "terminated"
            time.sleep(0.1)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return "terminated"


    def sweep_stalled_experiments(self, now: datetime | None = None) -> list[str]:
        """Fail active experiments that logged nothing past their phase limit.

        Startup reconciliation (#371) only helps on a restart; a worker that hangs silently
        in a live process kept its experiment ``implementing`` for 11 hours (#400), blocking
        the issue, the budget and retries. An experiment whose newest log entry or activity
        event is older than the phase hard limit plus the grace
        (``NEXUS_EXPERIMENT_STALL_GRACE_SECONDS``) moves to ``failed`` with a ``stalled:``
        error and its worker's process group is stopped if still alive; anything that
        logged within the limit is never touched. Idempotent: a swept experiment is terminal.

        Returns the ids of the experiments marked stalled.
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        grace = stall_grace_seconds()
        placeholders = ", ".join("?" * len(_STALL_SWEEP_STATUSES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM experiments WHERE status IN ({placeholders})",
                _STALL_SWEEP_STATUSES,
            ).fetchall()
        stalled: list[str] = []
        for row in rows:
            experiment_id, phase = row["id"], row["status"]
            limit = self._phase_hard_limit_seconds(phase)
            last_at, last_event = self._last_progress(experiment_id, row)
            if last_at is None:
                continue  # nothing to judge by; never guess
            silent = (now - last_at).total_seconds()
            if silent <= limit + grace:
                continue
            error = f"{_STALLED_ERROR_PREFIX}no progress for {int(silent)}s in phase {phase}"
            # Claim the row first, conditionally: an experiment that moved on in between is left
            # alone, and once failed the woken pipeline thread cannot overwrite the verdict.
            if not self._transition(
                experiment_id, phase, STATUS_FAILED,
                ended_at=now.isoformat(), error=error, worker_pgid=None,
            ):
                continue
            worker = self._stop_stalled_worker(experiment_id, row["worker_pgid"])
            self.get_experiment_logger(experiment_id).error(
                phase, "stalled_detected",
                f"No progress for {int(silent)}s in phase {phase} "
                f"(limit {int(limit)}s + grace {int(grace)}s); last event "
                f"{last_event or 'unknown'} at {last_at.isoformat()}; worker: {worker}",
                last_event=last_event,
                last_event_at=last_at.isoformat(),
                silent_seconds=int(silent),
                limit_seconds=int(limit),
                grace_seconds=int(grace),
                worker=worker,
            )
            logger.warning(
                "experiment %s stalled in phase %s: no progress for %ds (last event %s at %s); worker: %s",
                experiment_id, phase, int(silent), last_event, last_at.isoformat(), worker,
            )
            stalled.append(experiment_id)
        return stalled

