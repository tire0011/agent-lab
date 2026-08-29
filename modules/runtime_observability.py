"""Lightweight runtime observability: what is Nexus doing *right now*.

This is deliberately small (see issue #76): a single thread-safe
``RuntimeState`` singleton that is explicitly updated as tasks/operations
execute, plus a context-manager helper (``operation``) that automatically
maintains a heartbeat and a bounded, hierarchical operation history for the
currently/most-recently active task.

This module never parses the existing JSONL logs (``modules.experiment_logger``)
to answer "what is running now" — that would be slow and racy. Instead it
maintains explicit state, and ``modules.experiment_logger`` pulls the current
trace/span context from here so existing structured log entries stay
correlated with the trace shown by the runtime status API and (when
configured) by OpenTelemetry/Grafana Cloud.

Design notes:
  * One *current* task/trace at a time is modelled (matching the "what is
    Nexus doing right now" framing from the issue) — Nexus normally runs
    one experiment pipeline at a time. If several run concurrently, the
    most recently started one wins for ``status()``; recorded operations
    for a task remain associated with that task's ``trace_id`` internally
    via each operation's own started_at ordering, but the bounded history
    is process-wide, not per-task.
  * Never raises: a bug in this module must not break the Nexus task it is
    observing. Public entry points defensively catch/log unexpected errors
    rather than propagating them.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from modules import otel_adapter

logger = logging.getLogger("nexus.runtime")

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0
DEFAULT_STALL_THRESHOLD_SECONDS = 60.0
DEFAULT_MAX_TRACE_OPERATIONS = 200


def new_id() -> str:
    return uuid.uuid4().hex


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class _OperationRecord:
    name: str
    span_id: str
    parent_span_id: str | None
    phase: str | None
    started_at: float
    status: str = "running"
    ended_at: float | None = None
    error: str | None = None

    def duration_ms(self, now: float | None = None) -> int:
        end = self.ended_at if self.ended_at is not None else (now if now is not None else time.monotonic())
        return max(0, int((end - self.started_at) * 1000))

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "name": self.name,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "status": self.status,
            "duration_ms": self.duration_ms(now),
        }
        if self.phase:
            entry["phase"] = self.phase
        if self.error:
            entry["error"] = self.error
        return entry


class RuntimeState:
    """Thread-safe singleton describing Nexus's current execution state."""

    def __init__(
        self,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        stall_threshold: float = DEFAULT_STALL_THRESHOLD_SECONDS,
        max_trace_operations: int = DEFAULT_MAX_TRACE_OPERATIONS,
    ) -> None:
        self._lock = threading.RLock()
        self._heartbeat_interval = heartbeat_interval
        self._stall_threshold = stall_threshold
        self._max_trace_operations = max_trace_operations
        self._local = threading.local()
        self._last_task: dict[str, Any] | None = None
        self._reset_locked()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reset_locked(self) -> None:
        self.status = STATUS_IDLE
        self.task_id: str | None = None
        self.experiment_id: str | None = None
        self.trace_id: str | None = None
        self.phase: str | None = None
        self.current_operation: str | None = None
        self.worker: str | None = None
        self.model: str | None = None
        self.started_at: float | None = None
        self.started_at_iso: str | None = None
        self.operation_started_at: float | None = None
        self.last_heartbeat: float | None = None
        self._operations: deque[_OperationRecord] = deque(maxlen=self._max_trace_operations)

    def _current_record(self) -> _OperationRecord | None:
        return getattr(self._local, "current", None)

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def start_task(
        self,
        task_id: str | None = None,
        *,
        experiment_id: str | None = None,
        trace_id: str | None = None,
        worker: str = "nexus",
        model: str | None = None,
        phase: str | None = None,
    ) -> str:
        """Mark a new task as actively running; returns its ``trace_id``."""
        with self._lock:
            self._reset_locked()
            self.status = STATUS_RUNNING
            self.task_id = task_id
            self.experiment_id = experiment_id
            self.trace_id = trace_id or new_id()
            self.worker = worker
            self.model = model
            self.phase = phase
            now = time.monotonic()
            self.started_at = now
            self.started_at_iso = _utcnow_iso()
            self.last_heartbeat = now
            trace_id_result = self.trace_id
        self._local.current = None
        return trace_id_result

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def heartbeat(self) -> None:
        with self._lock:
            self.last_heartbeat = time.monotonic()

    def end_task(self, result: str = "succeeded", *, error: str | None = None) -> None:
        """Mark the current task finished; resets the "current" status.

        The task's outcome remains available (bounded) via ``last_task()``
        so a caller checking status immediately after completion can still
        see how it ended, even though ``status()`` itself goes back to
        ``idle``.
        """
        with self._lock:
            if self.task_id is not None or self.trace_id is not None:
                self._last_task = {
                    "task_id": self.task_id,
                    "experiment_id": self.experiment_id,
                    "trace_id": self.trace_id,
                    "result": result,
                    "ended_at": _utcnow_iso(),
                }
                if error:
                    self._last_task["error"] = error[:500]
            self._reset_locked()
        self._local.current = None

    def last_task(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_task) if self._last_task is not None else None

    # ------------------------------------------------------------------
    # Operations / spans
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def operation(self, name: str, *, phase: str | None = None, **attributes: Any) -> Iterator[dict[str, Any]]:
        """Wrap one meaningful operation: maintains heartbeat + history.

        Starts a background heartbeat thread for the duration of the
        operation (so anything wrapped here - an LLM call, a subprocess, a
        test run - counts as "still healthy" without every call site having
        to remember to call ``heartbeat()`` manually), records the
        operation in the bounded trace history, and mirrors it as an
        OpenTelemetry span when tracing is configured.
        """
        parent = self._current_record()
        span_id = new_id()
        record = _OperationRecord(
            name=name,
            span_id=span_id,
            parent_span_id=parent.span_id if parent else None,
            phase=phase,
            started_at=time.monotonic(),
        )
        with self._lock:
            self._operations.append(record)
            if phase:
                self.phase = phase
            self.current_operation = name
            self.operation_started_at = record.started_at
            self.last_heartbeat = record.started_at
            trace_id = self.trace_id

        self._local.current = record
        stop_event = threading.Event()
        hb_thread = threading.Thread(
            target=self._heartbeat_loop, args=(stop_event,), daemon=True, name=f"nexus-hb-{name}"
        )
        hb_thread.start()

        error: BaseException | None = None
        try:
            with otel_adapter.span(
                name,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=record.parent_span_id,
                phase=phase,
                **attributes,
            ):
                yield {"trace_id": trace_id, "span_id": span_id, "parent_span_id": record.parent_span_id}
        except BaseException as exc:  # noqa: BLE001 - re-raised, recorded first
            error = exc
            raise
        finally:
            stop_event.set()
            hb_thread.join(timeout=1.0)
            with self._lock:
                record.ended_at = time.monotonic()
                record.status = "failed" if error is not None else "completed"
                if error is not None:
                    record.error = f"{type(error).__name__}: {error}"[:500]
                if self.current_operation == name:
                    self.current_operation = None
                    self.operation_started_at = None
            self._local.current = parent

    def _heartbeat_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(self._heartbeat_interval):
            self.heartbeat()

    def current_context(self) -> dict[str, Any] | None:
        """Trace/span context for the calling thread's active operation, if any."""
        with self._lock:
            trace_id = self.trace_id
        if trace_id is None:
            return None
        record = self._current_record()
        return {
            "trace_id": trace_id,
            "span_id": record.span_id if record else None,
            "parent_span_id": record.parent_span_id if record else None,
            "operation": record.name if record else None,
        }

    # ------------------------------------------------------------------
    # Read path (for the runtime status/trace API)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Cheap, deterministic snapshot for ``GET /api/runtime/status``."""
        with self._lock:
            if self.status != STATUS_RUNNING:
                result: dict[str, Any] = {"status": STATUS_IDLE}
                if self._last_task is not None:
                    result["last_task"] = dict(self._last_task)
                return result

            now = time.monotonic()
            task_duration = now - self.started_at if self.started_at is not None else None
            operation_duration = (
                now - self.operation_started_at if self.operation_started_at is not None else None
            )
            heartbeat_age = now - self.last_heartbeat if self.last_heartbeat is not None else None
            healthy = heartbeat_age is None or heartbeat_age <= self._stall_threshold

            result = {
                "status": self.status,
                "task_id": self.task_id,
                "experiment_id": self.experiment_id,
                "trace_id": self.trace_id,
                "phase": self.phase,
                "operation": self.current_operation,
                "worker": self.worker,
                "model": self.model,
                "started_at": self.started_at_iso,
                "task_duration_seconds": round(task_duration, 3) if task_duration is not None else None,
                "operation_duration_seconds": (
                    round(operation_duration, 3) if operation_duration is not None else None
                ),
                "last_heartbeat_seconds_ago": (
                    round(heartbeat_age, 3) if heartbeat_age is not None else None
                ),
                "healthy": healthy,
            }
            return {k: v for k, v in result.items() if v is not None or k in ("status", "healthy")}

    def trace_snapshot(self) -> dict[str, Any]:
        """Bounded recent-operation history for ``GET /api/runtime/trace``."""
        with self._lock:
            trace_id = self.trace_id
            if trace_id is None and self._last_task is not None:
                trace_id = self._last_task.get("trace_id")
            now = time.monotonic()
            operations = [op.to_dict(now) for op in self._operations]
        return {"trace_id": trace_id, "operations": operations}


# ---------------------------------------------------------------------------
# Process-wide default instance + module-level convenience wrappers
# ---------------------------------------------------------------------------

_default_state = RuntimeState()


def get_runtime_state() -> RuntimeState:
    """The process-wide ``RuntimeState`` singleton used by the API/pipeline."""
    return _default_state


def start_task(*args: Any, **kwargs: Any) -> str:
    return _default_state.start_task(*args, **kwargs)


def end_task(*args: Any, **kwargs: Any) -> None:
    _default_state.end_task(*args, **kwargs)


def set_phase(phase: str) -> None:
    _default_state.set_phase(phase)


def heartbeat() -> None:
    _default_state.heartbeat()


def operation(name: str, *, phase: str | None = None, **attributes: Any):
    return _default_state.operation(name, phase=phase, **attributes)


def snapshot() -> dict[str, Any]:
    return _default_state.snapshot()


def trace_snapshot() -> dict[str, Any]:
    return _default_state.trace_snapshot()


def current_context() -> dict[str, Any] | None:
    """Never raises: used by ``modules.experiment_logger`` on every log line."""
    try:
        return _default_state.current_context()
    except Exception:  # noqa: BLE001
        logger.debug("runtime_observability: current_context() failed", exc_info=True)
        return None
