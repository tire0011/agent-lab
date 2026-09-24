from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from modules import heartbeat
from modules.llama_cpp_client import LlamaCppClient
from modules.redaction import redact
from modules.run_store import DEFAULT_DB_PATH

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
_TERMINAL = {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED}

# Job roles (#167). Every local-model caller submits through this one service,
# so the role is what keeps their jobs separable in the queue, in the database
# and in the logs. Roles are free-form strings; these are the ones Nexus ships.
ROLE_CONTROLLER = "controller"
ROLE_FAILURE_ANALYSIS = "failure_analysis"
ROLE_LOCAL_WORKER = "local_worker"

# Error classification (#167). A job's failure reason is part of the shared
# observability contract: callers should be able to tell a wedged worker from
# a refused connection without parsing exception text themselves.
ERROR_KIND_TIMEOUT = "timeout"
ERROR_KIND_CONNECTION = "connection"
ERROR_KIND_PROTOCOL = "protocol"
ERROR_KIND_MODEL = "model_error"
ERROR_KIND_CANCELLED = "cancelled"
ERROR_KIND_INTERRUPTED = "interrupted"
ERROR_KIND_STUCK = "stuck"
ERROR_KIND_UNKNOWN = "unknown"

# How long a `running` job may outlive the client watchdog before it is
# treated as stuck. The watchdog is the HTTP read timeout the worker itself
# uses, so anything still `running` well past it means the worker - not the
# model - is wedged, and a waiter would otherwise block forever.
_STUCK_GRACE_SECONDS = 120.0
# Cheap opportunistic sweep: at most one stuck-job UPDATE per interval, no
# extra thread. Waiters poll anyway, so this is where liveness is enforced.
_STUCK_SWEEP_INTERVAL_SECONDS = 30.0

# Cross-process serialization (#167 review). A process-wide singleton only
# stops one process from racing itself; Nexus runs the controller (nexus_tick)
# and the API in separate processes against the same database, so the "one
# request at a time" rule has to live in the database. A worker claims the
# single inference slot by transitioning a job to `running` inside one
# IMMEDIATE transaction, and states its liveness on the claim (D-018) rather
# than letting a dead process hold the slot forever.
_LEASE_ENV_VAR = "NEXUS_LOCAL_LLM_LEASE_SECONDS"
_DEFAULT_LEASE_SECONDS = 120
# How long a worker blocks on SQLite's write lock before giving up. Claims are
# short (a few statements), so contention resolves well inside this.
_BUSY_TIMEOUT_MS = 10_000
# How often an otherwise idle worker looks for work another process queued, or
# for the inference slot another process released.
_DEFAULT_IDLE_POLL_SECONDS = 1.0

_LOGGER = logging.getLogger(__name__)

# The systemd unit backing local inference. Diagnostics are best-effort: any
# failure to reach systemd/journald must never mask the original job error.
_LLAMA_SERVICE_NAME = "llama-server"
_DIAG_JOURNAL_LINES = 5
_DIAG_TIMEOUT_SECONDS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalLlmJobError(RuntimeError):
    """A local-LLM job did not produce a result.

    Carries the job id and the shared error classification so a caller can
    report *why* inference did not happen (failed, cancelled, stuck) without
    re-deriving it from the message text.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        kind: str = ERROR_KIND_UNKNOWN,
        status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.kind = kind
        self.status = status


def classify_job_error(exc: BaseException) -> str:
    """Map an inference exception onto one of the shared ``ERROR_KIND_*`` values.

    Deliberately type-name based: the worker must classify transport errors
    from httpx (or any future client) without this module importing them.
    """
    name = type(exc).__name__.lower()
    if "cancel" in name:
        return ERROR_KIND_CANCELLED
    if "timeout" in name or "timedout" in name:
        return ERROR_KIND_TIMEOUT
    if "connect" in name:
        return ERROR_KIND_CONNECTION
    if "protocol" in name or "readerror" in name or "writeerror" in name:
        return ERROR_KIND_PROTOCOL
    if "llamacpp" in name:
        return ERROR_KIND_MODEL
    return ERROR_KIND_UNKNOWN


def llama_server_failure_diagnostics(
    journal_lines: int = _DIAG_JOURNAL_LINES,
) -> dict[str, Any]:
    """Best-effort, cheap snapshot of llama-server health for failure logs.

    Collects ``systemctl show`` properties (active state, PID, restart count)
    and the last few ``journalctl`` lines for the llama-server unit. Never
    raises: any missing tool, permission error, or timeout is reported as a
    string under ``"error"`` instead of propagating, since this is only ever
    called from an exception handler and must not itself fail a job.
    """
    diagnostics: dict[str, Any] = {"service": _LLAMA_SERVICE_NAME}
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                _LLAMA_SERVICE_NAME,
                "--no-pager",
                "--property=ActiveState,SubState,MainPID,NRestarts",
            ],
            capture_output=True,
            text=True,
            timeout=_DIAG_TIMEOUT_SECONDS,
            check=False,
        )
        props: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip()
        diagnostics["active_state"] = props.get("ActiveState")
        diagnostics["sub_state"] = props.get("SubState")
        diagnostics["main_pid"] = props.get("MainPID")
        diagnostics["restart_count"] = props.get("NRestarts")
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostics["systemctl_error"] = f"{type(exc).__name__}: {exc}"

    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                _LLAMA_SERVICE_NAME,
                f"-n{max(1, journal_lines)}",
                "--no-pager",
                "--output=short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=_DIAG_TIMEOUT_SECONDS,
            check=False,
        )
        diagnostics["recent_journal"] = [
            redact(line) for line in result.stdout.splitlines() if line.strip()
        ]
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostics["journalctl_error"] = f"{type(exc).__name__}: {exc}"

    return diagnostics


_JOB_COLUMNS = (
    "id,status,session_id,created_at,started_at,finished_at,result,error,role,error_kind,"
    "worker_id,lease_expires_at"
)


@dataclass(slots=True)
class LocalLlmJob:
    id: str
    status: str
    session_id: str | None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: str | None = None
    error: str | None = None
    role: str = ROLE_CONTROLLER
    error_kind: str | None = None
    worker_id: str | None = None
    lease_expires_at: str | None = None


class LocalLlmJobService:
    """Persist and serialize slow local-LLM inference behind one claimed slot.

    One daemon worker per process, SQLite persistence and polling;
    llama-server remains the inference backend and callers never own the long
    HTTP request themselves.

    Serialization is enforced in the database, not in the process: a worker
    runs a job only after claiming the single inference slot in one IMMEDIATE
    transaction, so the controller process and the API process cannot both be
    talking to llama-server at once. A claim states its own liveness
    (``worker_id`` + ``lease_expires_at``, refreshed while the request is in
    flight); a claim whose lease expired belonged to a process that died, and
    is reclaimed. Queued work is visible to every worker, so a job submitted
    by a process that then exits is still executed.
    """

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        llama_client: LlamaCppClient | None = None,
        poll_interval: float = 0.25,
        idle_poll_interval: float | None = None,
    ) -> None:
        self.db_path = db_path
        watchdog = float(os.environ.get("NEXUS_LOCAL_LLM_JOB_WATCHDOG_SECONDS", "3600"))
        watchdog = max(300.0, min(watchdog, 7200.0))
        self.watchdog_seconds = watchdog
        self.llama = llama_client or LlamaCppClient(timeout=watchdog)
        self.poll_interval = max(0.05, poll_interval)
        self.idle_poll_interval = max(
            0.01,
            _DEFAULT_IDLE_POLL_SECONDS if idle_poll_interval is None else idle_poll_interval,
        )
        self._stuck_after_seconds = watchdog + _STUCK_GRACE_SECONDS
        self._last_stuck_sweep = 0.0
        # Identity and liveness come from the shared heartbeat contract
        # (D-018): who owns a claim, and for how long silence still means
        # "alive". The lease floor is deliberate - calling live work dead is
        # worse than noticing a dead worker late.
        self.worker_id = heartbeat.worker_id()
        self.lease_seconds = heartbeat.resolve_stale_timeout(
            os.environ.get(_LEASE_ENV_VAR, _DEFAULT_LEASE_SECONDS)
        )
        self._lock = threading.RLock()
        # Autocommit: the claim below is one explicit IMMEDIATE transaction,
        # and everything else is a single statement.
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._conn.execute(
            """
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
                error_kind TEXT
            )
            """
        )
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(local_llm_jobs)")
        }
        if "response_format" not in columns:
            self._conn.execute(
                "ALTER TABLE local_llm_jobs ADD COLUMN response_format TEXT"
            )
        # Additive migrations for databases created before #167. Existing rows
        # are controller jobs by construction, which is exactly what the
        # column default backfills.
        if "role" not in columns:
            self._conn.execute(
                "ALTER TABLE local_llm_jobs ADD COLUMN role TEXT NOT NULL DEFAULT 'controller'"
            )
        if "system" not in columns:
            self._conn.execute("ALTER TABLE local_llm_jobs ADD COLUMN system TEXT")
        if "error_kind" not in columns:
            self._conn.execute("ALTER TABLE local_llm_jobs ADD COLUMN error_kind TEXT")
        if "worker_id" not in columns:
            self._conn.execute("ALTER TABLE local_llm_jobs ADD COLUMN worker_id TEXT")
        if "lease_expires_at" not in columns:
            self._conn.execute("ALTER TABLE local_llm_jobs ADD COLUMN lease_expires_at TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_llm_jobs_status_created "
            "ON local_llm_jobs (status, created_at)"
        )
        # A process that died cannot keep an in-flight HTTP request alive, so
        # its claim is released here. Only dead claims: a `running` job whose
        # lease is still being refreshed belongs to another live process, and
        # killing it on our startup is exactly the cross-process damage this
        # lease exists to prevent.
        self._reclaim_dead_leases()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._closed = threading.Event()
        # The job this service's worker is executing right now, if any.
        self._current_job: str | None = None
        self._worker = threading.Thread(target=self._worker_loop, name="local-llm-job-worker", daemon=True)
        self._worker.start()

    def close(self) -> None:
        if self._closed.is_set():
            return
        # A closed service must never be handed to a later acquirer.
        _forget_shared_service(self)
        self._closed.set()
        self._queue.put(None)
        self._worker.join(timeout=2.0)
        # A worker drains every queued job, including ones another process
        # submitted and is still waiting for. A short-lived process (the tick,
        # a mailbox run) that finishes its own job can claim such a job just
        # before it closes. Exiting then took the claim down with it: the job
        # sat `running` until its lease lapsed and failed as interrupted,
        # although its submitter was alive the whole time. So an in-flight
        # job is finished first. It is bounded by the client watchdog, and
        # handing it back mid-request instead would put a second request on
        # llama-server while this one is still running.
        if self._worker.is_alive() and self._current_job is not None:
            _LOGGER.info(
                "local_llm service closing; finishing in-flight job_id=%s first",
                self._current_job,
            )
            while self._worker.is_alive() and self._current_job is not None:
                self._worker.join(timeout=1.0)
        with self._lock:
            self._conn.close()

    def submit(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        session_id: str | None = None,
        response_format: dict[str, Any] | None = None,
        role: str = ROLE_CONTROLLER,
        system: str | None = None,
    ) -> str:
        """Queue one local-model call and return its job id.

        *role* records which Nexus feature the job belongs to (see the
        ``ROLE_*`` constants). It never changes how a job is executed - every
        role shares this one queue and one worker - it only keeps the jobs
        separable in the database and in the logs.
        """
        self._sweep_stuck_jobs()
        job_id = uuid.uuid4().hex
        serialized_response_format = (
            json.dumps(response_format, separators=(",", ":"))
            if response_format is not None
            else None
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO local_llm_jobs
                    (id, status, prompt, temperature, max_tokens, response_format,
                     session_id, created_at, role, system)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    STATUS_QUEUED,
                    prompt,
                    float(temperature),
                    int(max_tokens),
                    serialized_response_format,
                    session_id,
                    _now(),
                    str(role or ROLE_CONTROLLER),
                    system or None,
                ),
            )
            self._conn.commit()
        _LOGGER.info(
            "local_llm job queued job_id=%s role=%s session_id=%s provider=local model=%s "
            "max_tokens=%s prompt_chars=%s",
            job_id, role, session_id, getattr(self.llama, "model", None),
            int(max_tokens), len(prompt),
        )
        self._queue.put(job_id)
        return job_id

    def get(self, job_id: str) -> LocalLlmJob | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM local_llm_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return LocalLlmJob(**dict(row))

    def recent(self, *, role: str | None = None, limit: int = 20) -> list[LocalLlmJob]:
        """Most recent jobs, newest first, optionally narrowed to one role.

        The diagnosability half of the role (#167): an operator asking "what
        did failure analysis ask the local model, and how did those jobs end?"
        gets an answer without having to filter controller traffic by hand.
        """
        sql = f"SELECT {_JOB_COLUMNS} FROM local_llm_jobs"
        params: list[Any] = []
        if role is not None:
            sql += " WHERE role = ?"
            params.append(role)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [LocalLlmJob(**dict(row)) for row in rows]

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> bool:
        """Cancel a queued or running job. Returns True if it was still open.

        A running job's in-flight HTTP request cannot be torn down from here,
        but the row becomes terminal immediately, so every waiter is released
        and the late result is discarded rather than resurrecting the job (see
        the status-guarded terminal writes in :meth:`_run`).
        """
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE local_llm_jobs
                SET status = ?, finished_at = ?, error = ?, error_kind = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    STATUS_CANCELLED,
                    _now(),
                    redact(str(reason))[:1000],
                    ERROR_KIND_CANCELLED,
                    job_id,
                    STATUS_QUEUED,
                    STATUS_RUNNING,
                ),
            )
            self._conn.commit()
            cancelled = cursor.rowcount > 0
        if cancelled:
            _LOGGER.warning("local_llm job cancelled job_id=%s reason=%s", job_id, reason)
        return cancelled

    def _sweep_stuck_jobs(self, *, force: bool = False) -> int:
        """Release `running` jobs no live worker can still finish (#167).

        Two ways a claim dies, and a waiter needs both:

        * the owner stopped heartbeating - its process died, so the claim is
          reclaimed (see :meth:`_reclaim_dead_leases`);
        * the owner is alive but wedged past its own client watchdog, which a
          heartbeat cannot detect because the heartbeat is still beating.

        Rate-limited, and never raises - a sweep failure must not break the
        submit/wait it was called from.
        """
        now = time.monotonic()
        if not force and (now - self._last_stuck_sweep) < _STUCK_SWEEP_INTERVAL_SECONDS:
            return 0
        self._last_stuck_sweep = now
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self._stuck_after_seconds)
        ).isoformat()
        try:
            with self._lock:
                cursor = self._conn.execute(
                    """
                    UPDATE local_llm_jobs
                    SET status = ?, finished_at = ?, error = ?, error_kind = ?
                    WHERE status = ? AND started_at IS NOT NULL AND started_at < ?
                    """,
                    (
                        STATUS_FAILED,
                        _now(),
                        f"local LLM job exceeded the {self._stuck_after_seconds:.0f}s "
                        "liveness window and was classified as stuck",
                        ERROR_KIND_STUCK,
                        STATUS_RUNNING,
                        cutoff,
                    ),
                )
                self._conn.commit()
                stuck = cursor.rowcount
        except sqlite3.Error as exc:
            _LOGGER.warning("local_llm stuck-job sweep failed: %s", exc)
            return 0
        if stuck:
            _LOGGER.error(
                "local_llm stuck jobs detected count=%s stuck_after_seconds=%.0f",
                stuck, self._stuck_after_seconds,
            )
        return stuck + self._reclaim_dead_leases()

    async def wait(self, job_id: str) -> str:
        """Await one job's result. Never imposes a deadline of its own (#167).

        How long a job may legitimately take is the model's business, not the
        waiter's: a caller that needs several minutes of local inference must
        not be cut off by an arbitrary caller-side timeout. Liveness comes
        from the shared stuck-job sweep below instead, which turns a wedged
        worker into a terminal job every waiter observes.
        """
        # Start tight and back off: a fast local reply should not pay a full
        # poll interval, a multi-minute one should not poll hundreds of times.
        delay = min(0.01, self.poll_interval)
        while True:
            job = self.get(job_id)
            if job is None:
                raise LocalLlmJobError(
                    f"unknown local LLM job {job_id}", job_id=job_id, kind=ERROR_KIND_UNKNOWN
                )
            if job.status == STATUS_SUCCEEDED:
                return job.result or ""
            if job.status in {STATUS_FAILED, STATUS_CANCELLED}:
                raise LocalLlmJobError(
                    job.error or f"local LLM job {job.status}",
                    job_id=job_id,
                    kind=job.error_kind or ERROR_KIND_UNKNOWN,
                    status=job.status,
                )
            self._sweep_stuck_jobs()
            await asyncio.sleep(delay)
            delay = min(self.poll_interval, delay * 2)

    def _lease_deadline(self) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)
        ).isoformat()

    def _reclaim_dead_leases(self) -> int:
        """Release claims whose owner stopped stating liveness.

        A `running` row with an expired (or, for rows written before leases
        existed, missing) lease belonged to a process that died mid-request:
        the HTTP call went with it, the job will never finish, and until it is
        released it holds the single inference slot for everyone. Never
        touches a claim that is still being refreshed - that one belongs to a
        live worker, possibly in another process.
        """
        try:
            with self._lock:
                return self._conn.execute(
                    """
                    UPDATE local_llm_jobs
                    SET status = ?, finished_at = ?, error = ?, error_kind = ?
                    WHERE status = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                    """,
                    (
                        STATUS_FAILED,
                        _now(),
                        "local LLM worker stopped heartbeating; claim released",
                        ERROR_KIND_INTERRUPTED,
                        STATUS_RUNNING,
                        _now(),
                    ),
                ).rowcount
        except sqlite3.Error as exc:
            _LOGGER.warning("local_llm lease reclaim failed: %s", exc)
            return 0

    def _claim_next_job(self) -> str | None:
        """Claim the single inference slot for the oldest queued job, if free.

        One IMMEDIATE transaction does the whole decision - release dead
        claims, check that nobody live is running, take the oldest queued job
        - so two workers in two processes cannot both conclude the slot is
        theirs. Returns the claimed job id, or None when there is nothing to
        run or another worker holds the slot.
        """
        # An idle worker polls for work another process queued. Checking that
        # first keeps the common "nothing to do" tick a single read instead of
        # a write transaction every process fights over.
        try:
            with self._lock:
                pending = self._conn.execute(
                    "SELECT 1 FROM local_llm_jobs WHERE status = ? LIMIT 1", (STATUS_QUEUED,)
                ).fetchone()
        except sqlite3.Error as exc:
            _LOGGER.warning("local_llm claim could not look for queued work: %s", exc)
            return None
        if pending is None:
            return None
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                _LOGGER.warning("local_llm claim could not start transaction: %s", exc)
                return None
            try:
                now = _now()
                released = self._conn.execute(
                    """
                    UPDATE local_llm_jobs
                    SET status = ?, finished_at = ?, error = ?, error_kind = ?
                    WHERE status = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                    """,
                    (
                        STATUS_FAILED,
                        now,
                        "local LLM worker stopped heartbeating; claim released",
                        ERROR_KIND_INTERRUPTED,
                        STATUS_RUNNING,
                        now,
                    ),
                ).rowcount
                busy = self._conn.execute(
                    "SELECT id, worker_id FROM local_llm_jobs WHERE status = ? LIMIT 1",
                    (STATUS_RUNNING,),
                ).fetchone()
                if busy is not None:
                    self._conn.execute("COMMIT")
                    return None
                row = self._conn.execute(
                    "SELECT id FROM local_llm_jobs WHERE status = ? "
                    "ORDER BY created_at, rowid LIMIT 1",
                    (STATUS_QUEUED,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                job_id = str(row["id"])
                claimed = self._conn.execute(
                    "UPDATE local_llm_jobs SET status = ?, started_at = ?, worker_id = ?, "
                    "lease_expires_at = ? WHERE id = ? AND status = ?",
                    (
                        STATUS_RUNNING,
                        now,
                        self.worker_id,
                        self._lease_deadline(),
                        job_id,
                        STATUS_QUEUED,
                    ),
                ).rowcount
                self._conn.execute("COMMIT")
            except sqlite3.Error as exc:
                _LOGGER.warning("local_llm claim failed: %s", exc)
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                return None
        if released:
            _LOGGER.error(
                "local_llm released %s claim(s) whose worker stopped heartbeating", released
            )
        return job_id if claimed else None

    def _refresh_lease(self, job_id: str) -> bool:
        """Restate this worker's claim. False once the claim is no longer ours."""
        try:
            with self._lock:
                refreshed = self._conn.execute(
                    "UPDATE local_llm_jobs SET lease_expires_at = ? "
                    "WHERE id = ? AND status = ? AND worker_id = ?",
                    (self._lease_deadline(), job_id, STATUS_RUNNING, self.worker_id),
                ).rowcount
            return bool(refreshed)
        except sqlite3.Error as exc:
            # Losing the database is not the same as losing the claim: keep
            # beating and let the next refresh decide.
            _LOGGER.warning("local_llm lease refresh failed job_id=%s reason=%s", job_id, exc)
            return True

    def _finalize(self, job_id: str, sql: str, params: tuple[Any, ...]) -> int:
        """Apply a terminal update for a job the worker just finished.

        Returns the number of rows written (0 when the job already became
        terminal - cancelled or swept as stuck - while the request was in
        flight). A shutdown that closes the database under a still-running
        worker is reported, not raised: the worker thread has nowhere to
        propagate it to.
        """
        try:
            with self._lock:
                updated = self._conn.execute(sql, params).rowcount
                self._conn.commit()
            return updated
        except sqlite3.Error as exc:
            _LOGGER.warning(
                "local_llm job outcome not recorded job_id=%s reason=%s", job_id, exc
            )
            return 0

    def _worker_loop(self) -> None:
        """Run claimable work, then wait to be woken or for the idle poll.

        The in-process queue is only a wake-up signal now; what may actually
        run is decided by the claim, so this worker also picks up jobs another
        process queued and slots another process released.
        """
        while not self._closed.is_set():
            while not self._closed.is_set():
                job_id = self._claim_next_job()
                if job_id is None:
                    break
                self._current_job = job_id
                try:
                    self._run(job_id)
                finally:
                    self._current_job = None
            if self._closed.is_set():
                return
            try:
                signal = self._queue.get(timeout=self.idle_poll_interval)
            except queue.Empty:
                continue
            if signal is None:
                return

    def _run(self, job_id: str) -> None:
        """Execute a job this worker has already claimed.

        The claim (status, owner, lease) was taken in :meth:`_claim_next_job`;
        here the worker only has to keep stating that it is alive and record
        the outcome under its own ownership.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT prompt,temperature,max_tokens,response_format,session_id,status,role,system,"
                "worker_id FROM local_llm_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None or row["status"] != STATUS_RUNNING or row["worker_id"] != self.worker_id:
            _LOGGER.warning("local_llm job no longer claimed by this worker job_id=%s", job_id)
            return
        session_id = row["session_id"]
        max_tokens = int(row["max_tokens"])
        prompt_chars = len(str(row["prompt"]))
        role = row["role"] or ROLE_CONTROLLER
        model = getattr(self.llama, "model", None)
        _LOGGER.info(
            "local_llm job start job_id=%s role=%s session_id=%s provider=local model=%s "
            "max_tokens=%s prompt_chars=%s",
            job_id, role, session_id, model, max_tokens, prompt_chars,
        )
        started = time.perf_counter()
        # Liveness is stated, not inferred (D-018): while this request is in
        # flight the claim is refreshed, so no other process steals the slot -
        # and if this process dies, the lease lapses and the slot is released.
        lease = heartbeat.Heartbeat(
            lambda: self._refresh_lease(job_id),
            interval_seconds=max(1, int(self.lease_seconds // 3)),
            name=f"local-llm-lease-{job_id[:8]}",
        ).start()
        try:
            completion_options: dict[str, Any] = {}
            if row["response_format"]:
                completion_options["response_format"] = json.loads(
                    str(row["response_format"])
                )
            # Only forwarded when a role actually uses a system prompt, so a
            # client that never needed one keeps its existing call signature.
            if row["system"]:
                completion_options["system"] = str(row["system"])
            result = asyncio.run(
                self.llama.complete(
                    prompt=str(row["prompt"]),
                    temperature=float(row["temperature"]),
                    max_tokens=max_tokens,
                    **completion_options,
                )
            )
        except Exception as exc:  # noqa: BLE001
            lease.stop()
            duration = time.perf_counter() - started
            error_kind = classify_job_error(exc)
            diagnostics = llama_server_failure_diagnostics()
            _LOGGER.error(
                "local_llm job failed job_id=%s role=%s session_id=%s provider=local model=%s "
                "duration=%.3fs max_tokens=%s prompt_chars=%s error_kind=%s exc_type=%s "
                "exc_message=%s llama_server_active_state=%s llama_server_sub_state=%s "
                "llama_server_main_pid=%s llama_server_restart_count=%s "
                "llama_server_recent_journal=%s",
                job_id, role, session_id, model, duration, max_tokens, prompt_chars,
                error_kind, type(exc).__name__, str(exc)[:1000],
                diagnostics.get("active_state"), diagnostics.get("sub_state"),
                diagnostics.get("main_pid"), diagnostics.get("restart_count"),
                diagnostics.get("recent_journal") or diagnostics.get("journalctl_error"),
                exc_info=True,
            )
            # Guarded by status *and* ownership: a job cancelled, swept as
            # stuck, or reclaimed by another worker while this request was in
            # flight stays terminal as it was recorded.
            self._finalize(
                job_id,
                "UPDATE local_llm_jobs SET status = ?, finished_at = ?, error = ?, error_kind = ? "
                "WHERE id = ? AND status = ? AND worker_id = ?",
                (
                    STATUS_FAILED,
                    _now(),
                    f"{type(exc).__name__}: {str(exc)[:1000]}",
                    error_kind,
                    job_id,
                    STATUS_RUNNING,
                    self.worker_id,
                ),
            )
            return
        lease.stop()
        duration = time.perf_counter() - started
        _LOGGER.info(
            "local_llm job succeeded job_id=%s role=%s session_id=%s provider=local model=%s "
            "duration=%.3fs max_tokens=%s prompt_chars=%s result_chars=%s",
            job_id, role, session_id, model, duration, max_tokens, prompt_chars, len(result),
        )
        updated = self._finalize(
            job_id,
            "UPDATE local_llm_jobs SET status = ?, finished_at = ?, result = ?, error = NULL, "
            "error_kind = NULL WHERE id = ? AND status = ? AND worker_id = ?",
            (STATUS_SUCCEEDED, _now(), result, job_id, STATUS_RUNNING, self.worker_id),
        )
        if not updated:
            _LOGGER.warning(
                "local_llm job result discarded job_id=%s role=%s reason=already_terminal",
                job_id, role,
            )


# ---------------------------------------------------------------------------
# Process-wide sharing (#167)
#
# Nexus must have exactly one local-model execution mechanism: queueing,
# serialization, liveness and logging live here, and every feature that needs
# the local model goes through the same worker. Two services against the same
# database would mean two workers and two concurrent llama-server requests -
# precisely the race this component exists to prevent - so callers that do not
# bring their own service acquire the shared one for their database instead.
# ---------------------------------------------------------------------------

_SHARED_LOCK = threading.Lock()
_SHARED_SERVICES: dict[str, tuple[LocalLlmJobService, int]] = {}


def acquire_local_llm_job_service(
    *,
    db_path: str = DEFAULT_DB_PATH,
    llama_client: LlamaCppClient | None = None,
) -> LocalLlmJobService:
    """Return the shared job service for *db_path*, creating it if needed.

    Every acquirer must eventually call :func:`release_local_llm_job_service`;
    the service is closed once the last one does.

    An explicitly supplied *llama_client* is never silently replaced by
    another caller's: if the shared service is already running a different
    client, a private service is returned instead. Production callers pass no
    client and therefore always share; tests that inject a scripted client
    keep getting their own isolated worker.
    """
    key = str(db_path)
    with _SHARED_LOCK:
        entry = _SHARED_SERVICES.get(key)
        if entry is not None:
            service, refcount = entry
            if llama_client is None or service.llama is llama_client:
                _SHARED_SERVICES[key] = (service, refcount + 1)
                return service
            return LocalLlmJobService(db_path=db_path, llama_client=llama_client)
        service = LocalLlmJobService(db_path=db_path, llama_client=llama_client)
        _SHARED_SERVICES[key] = (service, 1)
        return service


def release_local_llm_job_service(service: LocalLlmJobService) -> None:
    """Drop one acquirer's reference; close the service once none are left.

    Safe for a private service too (it is simply closed), so a caller never
    has to remember which kind :func:`acquire_local_llm_job_service` gave it.
    """
    with _SHARED_LOCK:
        for key, (shared, refcount) in list(_SHARED_SERVICES.items()):
            if shared is not service:
                continue
            if refcount > 1:
                _SHARED_SERVICES[key] = (shared, refcount - 1)
                return
            del _SHARED_SERVICES[key]
            break
    service.close()


def _forget_shared_service(service: LocalLlmJobService) -> None:
    """Drop *service* from the shared registry regardless of its refcount."""
    with _SHARED_LOCK:
        for key, (shared, _refcount) in list(_SHARED_SERVICES.items()):
            if shared is service:
                del _SHARED_SERVICES[key]
                return


def reset_local_llm_job_services() -> None:
    """Close and forget every shared service (shutdown paths and tests)."""
    with _SHARED_LOCK:
        services = [service for service, _ in _SHARED_SERVICES.values()]
        _SHARED_SERVICES.clear()
    for service in services:
        service.close()
