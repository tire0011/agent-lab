"""Explicit liveness for long-running claimed work (#162).

Nexus used to infer liveness from whatever generic timestamp a row happened to
carry (``updated_at``). That conflates "this row changed recently" with "the
worker responsible for this work is alive", and the two differ in both
directions: a healthy controller turn can run for many minutes without writing
a row, while a dead one keeps its last ``updated_at`` forever. #160 had to pick
a six-hour reaping threshold for exactly that reason.

This module holds the subsystem-agnostic half of the heartbeat contract so the
controller/session path, mailbox tasks, experiments and later parallel workers
can share one meaning of "alive":

* a *claim* records who owns a unit of work (``worker_id``) and since when,
* a *heartbeat* is an explicit, periodic "I still own this and I am alive",
* work whose heartbeat stopped for longer than the configured timeout is
  *stale* and may be reconciled to an explicit terminal outcome.

Persistence stays with each subsystem (it knows its own table); only the
timing, identity and staleness rules live here.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger(__name__)

# How often a live worker refreshes its heartbeat. Cheap (one indexed UPDATE),
# so the interval is chosen to keep the stale timeout meaningfully short rather
# than to save writes.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30
# One second is a deliberate floor rather than a recommendation: beating that
# fast is wasteful, but nothing breaks, and a hard floor here would only make
# short-lived callers (and tests) wait for a slower clock.
MIN_HEARTBEAT_INTERVAL_SECONDS = 1
MAX_HEARTBEAT_INTERVAL_SECONDS = 3600

# How long a claim may go without a heartbeat before it is considered
# abandoned. The floor matters more than the default: a small timeout would
# declare healthy work dead, which is strictly worse than noticing a dead
# worker late, so no configuration may push it below MIN_STALE_HEARTBEAT_SECONDS
# or below a few missed heartbeats.
DEFAULT_STALE_HEARTBEAT_SECONDS = 15 * 60
MIN_STALE_HEARTBEAT_SECONDS = 60
MISSED_HEARTBEATS_BEFORE_STALE = 4

INTERVAL_ENV_VAR = "NEXUS_HEARTBEAT_INTERVAL_SECONDS"
STALE_ENV_VAR = "NEXUS_STALE_HEARTBEAT_SECONDS"
WORKER_ID_ENV_VAR = "NEXUS_WORKER_ID"

_worker_id_lock = threading.Lock()
_worker_id: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_int(value: object) -> int | None:
    """Return ``value`` as an int, or ``None`` when it is not usable at all.

    Configuration arrives from environment variables and API callers, so
    ``None``, ``""``, ``"abc"`` and ``12.7`` all have to be survivable.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def resolve_interval(value: object = None) -> int:
    """Heartbeat period in seconds; invalid input falls back to the default."""
    resolved = _coerce_int(value if value is not None else os.environ.get(INTERVAL_ENV_VAR))
    if resolved is None or resolved <= 0:
        resolved = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    return max(MIN_HEARTBEAT_INTERVAL_SECONDS, min(resolved, MAX_HEARTBEAT_INTERVAL_SECONDS))


def resolve_stale_timeout(value: object = None, *, interval: object = None) -> int:
    """Stale-heartbeat timeout in seconds, clamped to a safe minimum.

    Zero, negative and unparsable values are configuration mistakes, not a
    request to reap everything in flight: they fall back to the default. Any
    value below the floor - or below the few missed heartbeats it takes to tell
    a slow worker from a dead one - is raised to it.
    """
    resolved = _coerce_int(value if value is not None else os.environ.get(STALE_ENV_VAR))
    if resolved is None or resolved <= 0:
        resolved = DEFAULT_STALE_HEARTBEAT_SECONDS
    floor = max(
        MIN_STALE_HEARTBEAT_SECONDS,
        resolve_interval(interval) * MISSED_HEARTBEATS_BEFORE_STALE,
    )
    return max(floor, resolved)


def worker_id() -> str:
    """Stable identity for this process, so ownership is observable.

    Host and pid make the owner findable from a database row; the random suffix
    keeps two runs of the same pid (after a restart) from looking like one
    continuous owner.
    """
    global _worker_id
    with _worker_id_lock:
        if _worker_id is None:
            configured = (os.environ.get(WORKER_ID_ENV_VAR) or "").strip()
            if configured:
                _worker_id = configured[:128]
            else:
                host = (socket.gethostname() or "unknown").split(".")[0]
                _worker_id = f"{host[:48]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        return _worker_id


def parse_timestamp(value: object) -> datetime | None:
    """Parse a stored ISO timestamp, tolerating naive values as UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        # Structured logs write a ``Z`` suffix, which ``fromisoformat`` only
        # accepts from Python 3.11 on; the VM runs 3.10.
        if text[-1:] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def heartbeat_age_seconds(last_heartbeat_at: object, *, now: datetime | None = None) -> float | None:
    """Seconds since the last heartbeat, or ``None`` when there is none."""
    parsed = parse_timestamp(last_heartbeat_at)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (reference - parsed).total_seconds())


def is_stale(
    last_heartbeat_at: object,
    stale_after_seconds: object,
    *,
    now: datetime | None = None,
) -> bool:
    """Has this claim gone quiet for longer than the (clamped) timeout?

    Work that never heartbeat at all is not judged here: an unclaimed row has
    no owner to declare dead, and callers decide what to do with it.
    """
    age = heartbeat_age_seconds(last_heartbeat_at, now=now)
    if age is None:
        return False
    return age > resolve_stale_timeout(stale_after_seconds)


class Heartbeat:
    """Background refresher that reports a worker alive while it owns work.

    Instrumentation must never be the reason work fails, so a refresh that
    raises is logged and retried on the next tick. A refresh that returns
    ``False`` means the claim is gone (reaped, or taken over by another
    worker): the thread stops rather than resurrecting someone else's claim.
    """

    def __init__(
        self,
        refresh: Callable[[], bool],
        *,
        interval_seconds: object = None,
        name: str = "heartbeat",
    ) -> None:
        self._refresh = refresh
        self.interval_seconds = resolve_interval(interval_seconds)
        self._name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost_claim = False

    def start(self) -> "Heartbeat":
        if self._thread is not None:
            return self
        thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread = thread
        thread.start()
        return self

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def beat_once(self) -> bool:
        """Refresh once inline; returns False when the claim is no longer ours."""
        try:
            alive = bool(self._refresh())
        except Exception:  # noqa: BLE001 - a heartbeat must not break the work
            logger.exception("heartbeat refresh failed for %s", self._name)
            return True
        if not alive:
            self.lost_claim = True
        return alive

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            if not self.beat_once():
                return

    def __enter__(self) -> "Heartbeat":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()
