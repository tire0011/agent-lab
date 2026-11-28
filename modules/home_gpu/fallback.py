"""Deadline fallback to the VM path and the savings number (#761, part 3 of #743).

Three things live here:

* :func:`expire_jobs` - a ``queued`` ``home_gpu_jobs`` row whose deadline has
  passed becomes ``expired``. Deadlines are compared as parsed timestamps, not
  as strings, so a row written in a different ISO format still expires.

* PC-off detection and :func:`submit_job` - when no job has been ``answered``
  in the last 24 h *and* the PC had work older than that (at least one row
  submitted at or before the 24 h cutoff - without that evidence a fresh queue
  with in-flight jobs would read as "off" and the home GPU could never boot),
  the PC counts as off. New :func:`submit_job` calls then return
  ``"fallback"`` at once and write nothing to the mailbox, so nothing in the
  paid path ever waits on a home-GPU job. Batch kinds (``hunk_replay``,
  ``bakeoff_prompt``) are put on the existing VM local path
  (``local_llm_jobs`` with role ``home_gpu``) and run in the night window only
  (:func:`in_vm_window`, ``NEXUS_HOME_GPU_VM_HOURS``, default ``0-6`` UTC);
  interactive kinds (``code_draft``, ``review_draft``, ``chat_completion``)
  are recorded as ``home_gpu_expired`` and dropped. Both cases still get a
  ``home_gpu_jobs`` row (status ``fallback`` / ``home_gpu_expired``) so the
  savings share can see them; only the mailbox is left untouched.

* :func:`savings_summary` - answered jobs, completion tokens and PC seconds
  over the last ``days`` days, plus the VM seconds those tokens would have
  taken at the measured VM rate (the latest ``LocalModelRates`` generation
  figure, ~2.6 tok/s; ``vm_seconds_saved = tokens / vm_rate``), and the share
  of expired-or-fallback jobs. :func:`home_gpu_status` bundles this for
  ``/status`` under ``home_gpu``.

All functions take an optional ``now`` (fake clock in tests). Database work
runs in savepoints exactly like :mod:`modules.home_gpu.collect`, so a caller's
open transaction on the shared connection is never committed here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from modules.home_gpu import jobs
from modules.home_gpu.collect import _ensure_schema, _tx
from modules.local_model_rates import LocalModelRates

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_ANSWERED = "answered"
STATUS_EXPIRED = "expired"
#: Row status for a batch job that went to the VM local path instead.
STATUS_FALLBACK = "fallback"
#: Row status for an interactive job that was dropped because the PC is off.
STATUS_HOME_GPU_EXPIRED = "home_gpu_expired"

#: Return value of :func:`submit_job` when the job did not go to the home GPU.
FALLBACK = "fallback"

#: Batch kinds may run on the VM in the night window; interactive kinds may not.
BATCH_KINDS = frozenset({"hunk_replay", "bakeoff_prompt"})
INTERACTIVE_KINDS = jobs.ALLOWED_KINDS - BATCH_KINDS

#: No ``answered`` row within this window (with older work as evidence) means
#: the PC counts as off.
PC_OFF_AFTER = timedelta(hours=24)

#: Night window for VM-run fallback jobs, as UTC hours ``"start-end"``
#: (start inclusive, end exclusive; wraps past midnight, e.g. ``"22-6"``).
ENV_VM_HOURS = "NEXUS_HOME_GPU_VM_HOURS"
DEFAULT_VM_HOURS = "0-6"

#: Used when no VM rate has been measured yet (~2.6 tok/s on the VM).
DEFAULT_VM_TOKENS_PER_SECOND = 2.6

#: Key read from ``LocalModelRates`` for the VM-seconds estimate.
_VM_RATE_KEY = "generation_tokens_per_second"


def _utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return _utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` or offset); ``None`` when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


# --- deadline expiry ---------------------------------------------------------


def expire_jobs(db: sqlite3.Connection, now: datetime | None = None) -> int:
    """Mark every ``queued`` row whose deadline has passed as ``expired``.

    Returns the number of rows expired. A row exactly at its deadline counts
    as past it. ``answered`` (and already ``expired``) rows are never touched.
    """
    now = _utc(now)
    _ensure_schema(db)
    expired: list[str] = []
    for job_id, deadline in db.execute(
        "SELECT job_id, deadline FROM home_gpu_jobs WHERE status = ?", (STATUS_QUEUED,)
    ):
        parsed = _parse_iso(deadline)
        if parsed is not None and parsed <= now:
            expired.append(str(job_id))
    if not expired:
        return 0
    placeholders = ",".join("?" for _ in expired)
    with _tx(db):
        updated = db.execute(
            f"UPDATE home_gpu_jobs SET status = ? "
            f"WHERE status = ? AND job_id IN ({placeholders})",
            (STATUS_EXPIRED, STATUS_QUEUED, *expired),
        ).rowcount
    if updated:
        logger.info("home-GPU jobs expired past their deadline: %d job(s)", updated)
    return updated


# --- PC-off detection --------------------------------------------------------


def pc_is_off(db: sqlite3.Connection, now: datetime | None = None) -> bool:
    """True when the home PC counts as off at *now*.

    The PC counts as off when no job has been ``answered`` in the last 24 h
    and there is evidence the PC had work for at least that long (a row
    submitted at or before the 24 h cutoff). A fresh queue whose jobs are
    still inside their deadline is not evidence - those may simply be in
    flight - and an empty table is not either, so the first job ever still
    goes to the GPU.
    """
    if not _table_exists(db, "home_gpu_jobs"):
        return False
    now = _utc(now)
    cutoff = now - PC_OFF_AFTER
    latest_answered: datetime | None = None
    has_old_work = False
    for submitted_at, answered_at, status in db.execute(
        "SELECT submitted_at, answered_at, status FROM home_gpu_jobs"
    ):
        submitted = _parse_iso(submitted_at)
        if submitted is not None and submitted <= cutoff:
            has_old_work = True
        if status == STATUS_ANSWERED:
            answered = _parse_iso(answered_at)
            if answered is not None and (latest_answered is None or answered > latest_answered):
                latest_answered = answered
    if latest_answered is not None and latest_answered >= cutoff:
        return False
    return has_old_work


# --- night window for VM-run fallback jobs -----------------------------------


def vm_hours_spec() -> str:
    """The configured ``NEXUS_HOME_GPU_VM_HOURS`` value, default ``"0-6"``."""
    raw = (os.environ.get(ENV_VM_HOURS) or "").strip()
    return raw or DEFAULT_VM_HOURS


def _parse_hours(spec: str) -> tuple[int, int] | None:
    """``"start-end"`` UTC hours to ``(start, end)``; ``None`` when malformed."""
    parts = spec.split("-")
    if len(parts) != 2:
        return None
    try:
        start, end = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 24 and start != end):
        return None
    return start, end


def vm_window() -> tuple[int, int]:
    """The active VM window as ``(start_hour, end_hour)`` UTC.

    A malformed ``NEXUS_HOME_GPU_VM_HOURS`` falls back to the default ``0-6``
    (with a warning) rather than disabling or widening the window silently.
    """
    spec = vm_hours_spec()
    parsed = _parse_hours(spec)
    if parsed is None:
        logger.warning("%s=%r is not 'start-end' UTC hours; using %s", ENV_VM_HOURS, spec,
                       DEFAULT_VM_HOURS)
        parsed = _parse_hours(DEFAULT_VM_HOURS) or (0, 6)
    return parsed


def in_vm_window(now: datetime | None = None) -> bool:
    """True when *now* (UTC) is inside the configured VM night window."""
    now = _utc(now)
    start, end = vm_window()
    hour = now.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps past midnight, e.g. 22-6


def vm_window_info(now: datetime | None = None) -> dict[str, Any]:
    """The window description for ``/status``: spec, bounds, active flag."""
    now = _utc(now)
    start, end = vm_window()
    return {
        "spec": vm_hours_spec(),
        "start_hour_utc": start,
        "end_hour_utc": end,
        "utc_hour": now.hour,
        "active": in_vm_window(now),
    }


# --- submission with fallback ------------------------------------------------


def _enqueue_vm_local(
    db: sqlite3.Connection, kind: str, payload: Any, *, now: datetime
) -> str:
    """Put a batch job on the existing VM local path (``local_llm_jobs``).

    The row gets role ``home_gpu``; :mod:`modules.local_llm_jobs` only claims
    such rows inside the VM night window, so the job waits there until the
    window opens. The prompt is the deterministic JSON envelope of the kind
    and the payload.
    """
    from modules.local_llm_jobs import ROLE_HOME_GPU  # lazy: local_llm_jobs imports this module lazily too

    _ensure_local_llm_jobs_table(db)
    prompt = json.dumps(
        {"home_gpu_kind": kind, "payload": payload},
        sort_keys=True, ensure_ascii=False, allow_nan=False,
    )
    max_tokens = 1024
    if isinstance(payload, dict):
        candidate = payload.get("max_tokens")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            max_tokens = candidate
    job_id = uuid.uuid4().hex
    with _tx(db):
        db.execute(
            """
            INSERT INTO local_llm_jobs
                (id, status, prompt, temperature, max_tokens, session_id, created_at, role)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, STATUS_QUEUED, prompt, 0.0, max_tokens, None, _iso(now), ROLE_HOME_GPU,
            ),
        )
    logger.info("home-GPU batch job fell back to the VM local queue job_id=%s kind=%s",
                job_id, kind)
    return job_id


_LOCAL_LLM_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS local_llm_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    temperature REAL NOT NULL,
    max_tokens INTEGER NOT NULL,
    response_format TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result TEXT,
    error TEXT,
    role TEXT NOT NULL DEFAULT 'controller',
    system TEXT,
    error_kind TEXT,
    worker_id TEXT,
    lease_expires_at TEXT,
    top_logprobs INTEGER
)
"""


def _ensure_local_llm_jobs_table(db: sqlite3.Connection) -> None:
    """Create ``local_llm_jobs`` when missing (mirrors the service DDL).

    Normally :class:`LocalLlmJobService` creates the table; the fallback only
    queues, it never runs the worker, so it creates the same shape itself when
    no service ever ran against this database. Existing tables are only
    checked for the ``role`` column and altered, exactly like the service.
    """
    if not _table_exists(db, "local_llm_jobs"):
        with _tx(db):
            db.execute(_LOCAL_LLM_JOBS_DDL)
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(local_llm_jobs)")}
    if "role" not in columns:
        with _tx(db):
            db.execute("ALTER TABLE local_llm_jobs ADD COLUMN role TEXT NOT NULL DEFAULT 'controller'")


def _record_fallback_row(
    db: sqlite3.Connection,
    *,
    job_id: str,
    kind: str,
    status: str,
    payload: Any,
    deadline_s: int,
    now: datetime,
) -> None:
    """Insert the ``home_gpu_jobs`` row for a job that did not reach the PC."""
    jobs.check_payload(payload)  # same public-data guard as the mailbox path
    digest = hashlib.sha256(jobs._serialize(payload).encode("utf-8")).hexdigest()
    with _tx(db):
        db.execute(
            """
            INSERT INTO home_gpu_jobs
                (job_id, kind, status, submitted_at, deadline, input_sha256)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, kind, status, _iso(now), _iso(now + timedelta(seconds=deadline_s)), digest),
        )


def submit_job(
    db: sqlite3.Connection,
    kind: str,
    payload: Any,
    deadline_s: int = jobs.DEFAULT_DEADLINE_S,
    *,
    mailbox_dir: Path | str,
    commit: bool,
    now: datetime | None = None,
    enqueue_vm: Callable[[sqlite3.Connection, str, Any], str] | None = None,
) -> str:
    """Submit a job, falling back to the VM path when the PC counts as off.

    Returns the ``hg-...`` job id when the job went to the home GPU (mailbox
    file written by :func:`modules.home_gpu.jobs.submit_job` plus a ``queued``
    row here), or ``"fallback"`` when the PC is off. In the fallback case
    nothing is written to the mailbox and nothing waits: batch kinds are
    enqueued on the VM local path via *enqueue_vm* (default
    :func:`enqueue_vm_local`; the worker only claims such rows inside the
    night window) and recorded as ``fallback``; interactive kinds are recorded as
    ``home_gpu_expired`` and dropped. The return happens at once either way,
    so the paid path never waits on a home-GPU job.
    """
    now = _utc(now)
    _ensure_schema(db)
    if kind not in jobs.ALLOWED_KINDS:
        raise jobs.HomeGpuJobError(
            f"unknown job kind {kind!r}; allowed: {', '.join(sorted(jobs.ALLOWED_KINDS))}"
        )
    if isinstance(deadline_s, bool) or not isinstance(deadline_s, int) or deadline_s <= 0:
        raise jobs.HomeGpuJobError(f"deadline_s must be a positive int, got {deadline_s!r}")
    expire_jobs(db, now)
    if pc_is_off(db, now):
        job_id = jobs._new_job_id(now)
        if kind in BATCH_KINDS:
            if enqueue_vm is not None:
                enqueue_vm(db, kind, payload)
            else:
                _enqueue_vm_local(db, kind, payload, now=now)
            _record_fallback_row(
                db, job_id=job_id, kind=kind, status=STATUS_FALLBACK, payload=payload,
                deadline_s=deadline_s, now=now,
            )
            logger.info("home-GPU job %s (kind=%s) fell back to the VM path; PC off", job_id, kind)
        else:
            _record_fallback_row(
                db, job_id=job_id, kind=kind, status=STATUS_HOME_GPU_EXPIRED, payload=payload,
                deadline_s=deadline_s, now=now,
            )
            logger.info("home-GPU job %s (kind=%s) dropped as home_gpu_expired; PC off", job_id, kind)
        return FALLBACK
    submitted = jobs.submit_job(
        kind, payload, deadline_s, mailbox_dir=mailbox_dir, commit=commit
    )
    with _tx(db):
        db.execute(
            """
            INSERT INTO home_gpu_jobs
                (job_id, kind, status, submitted_at, deadline, input_sha256)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                submitted.job_id, kind, STATUS_QUEUED, _iso(now), submitted.deadline,
                submitted.input_sha256,
            ),
        )
    return submitted.job_id


# --- savings -----------------------------------------------------------------


def _vm_rate(
    rates: LocalModelRates | None, vm_rate: float | None
) -> float:
    """The VM generation rate to price saved VM seconds with."""
    if vm_rate is not None:
        if not (isinstance(vm_rate, (int, float)) and not isinstance(vm_rate, bool)
                and vm_rate > 0):
            raise ValueError(f"vm_rate must be a positive number, got {vm_rate!r}")
        return float(vm_rate)
    rates = rates or LocalModelRates()
    latest = rates.latest_rate(_VM_RATE_KEY)
    return latest if latest is not None and latest > 0 else DEFAULT_VM_TOKENS_PER_SECOND


def savings_summary(
    db: sqlite3.Connection,
    days: int = 7,
    *,
    now: datetime | None = None,
    rates: LocalModelRates | None = None,
    vm_rate: float | None = None,
) -> dict[str, Any]:
    """What the home GPU spared the VM over the last *days* days.

    ``answered_jobs``, ``completion_tokens`` and ``pc_seconds`` count the rows
    ``answered`` within the window. ``vm_seconds_saved`` is what those
    completion tokens would have taken on the VM at the measured VM rate (the
    latest ``LocalModelRates`` generation figure, default ~2.6 tok/s):
    ``vm_seconds_saved = completion_tokens / vm_rate``. ``expired_or_fallback_share``
    is the share of all rows that never ran on the PC (``expired``,
    ``fallback`` or ``home_gpu_expired``). A missing table reads as zeros, so
    this is safe on a read-only connection for ``/status``.
    """
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError(f"days must be a positive int, got {days!r}")
    now = _utc(now)
    cutoff = now - timedelta(days=days)
    answered_jobs = 0
    completion_tokens = 0
    pc_seconds = 0.0
    total_jobs = 0
    expired_or_fallback_jobs = 0
    if _table_exists(db, "home_gpu_jobs"):
        for status, answered_at, completion, seconds in db.execute(
            "SELECT status, answered_at, completion_tokens, seconds FROM home_gpu_jobs"
        ):
            total_jobs += 1
            if status in (STATUS_EXPIRED, STATUS_FALLBACK, STATUS_HOME_GPU_EXPIRED):
                expired_or_fallback_jobs += 1
            if status != STATUS_ANSWERED:
                continue
            answered = _parse_iso(answered_at)
            if answered is None or answered < cutoff:
                continue
            answered_jobs += 1
            if isinstance(completion, (int, float)) and not isinstance(completion, bool):
                completion_tokens += int(completion)
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                pc_seconds += float(seconds)
    rate = _vm_rate(rates, vm_rate)
    vm_seconds_saved = completion_tokens / rate if rate > 0 else 0.0
    return {
        "days": days,
        "window_start": _iso(cutoff),
        "answered_jobs": answered_jobs,
        "completion_tokens": completion_tokens,
        "pc_seconds": pc_seconds,
        "vm_rate_tokens_per_second": rate,
        "vm_seconds_saved": vm_seconds_saved,
        "total_jobs": total_jobs,
        "expired_or_fallback_jobs": expired_or_fallback_jobs,
        "expired_or_fallback_share": (expired_or_fallback_jobs / total_jobs) if total_jobs else 0.0,
    }


def home_gpu_status(
    db_path: Path | str, *, now: datetime | None = None, days: int = 7
) -> dict[str, Any]:
    """The ``home_gpu`` block for ``/status``.

    Opens *db_path* read-only (never creating it) and reports whether the
    feature is enabled, whether the PC counts as off, the VM night window and
    the savings summary. No path is ever included in the result.
    """
    now = _utc(now)
    info: dict[str, Any] = {
        "enabled": jobs.home_gpu_enabled(),
        "vm_window": vm_window_info(now),
    }
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        info["available"] = False
        return info
    try:
        info["available"] = True
        info["pc_off"] = pc_is_off(conn, now)
        info["savings"] = savings_summary(conn, days=days, now=now)
    finally:
        conn.close()
    return info


__all__ = [
    "BATCH_KINDS",
    "DEFAULT_VM_HOURS",
    "DEFAULT_VM_TOKENS_PER_SECOND",
    "ENV_VM_HOURS",
    "FALLBACK",
    "INTERACTIVE_KINDS",
    "PC_OFF_AFTER",
    "STATUS_ANSWERED",
    "STATUS_EXPIRED",
    "STATUS_FALLBACK",
    "STATUS_HOME_GPU_EXPIRED",
    "STATUS_QUEUED",
    "enqueue_vm_local",
    "expire_jobs",
    "home_gpu_status",
    "in_vm_window",
    "pc_is_off",
    "savings_summary",
    "submit_job",
    "vm_hours_spec",
    "vm_window",
    "vm_window_info",
]


# Public alias: the default VM enqueue is useful on its own in tests/tools.
enqueue_vm_local = _enqueue_vm_local
