"""Centralized structured JSONL logging for Nexus.

Experiments, pre-approval mailbox messages, chat jobs, and system records use
separate durable files under ``state/experiment-logs`` and are merged by the
read path into one chronological feed.

Design goals (see AGENTS.md / the logging task for the full spec):
  * Reuse the *existing* central redaction helpers (``modules.redaction``)
    rather than inventing a second sanitization path.
  * Every entry is written and flushed (+ ``fsync``) immediately, so a
    crash, timeout, or SSH disconnect right after the write can never lose
    it.
  * Safe under concurrent writers (the mailbox worker thread and the
    experiment pipeline thread may log to the same file).
  * Bounded: single messages/outputs are truncated, and each experiment's
    log file has a hard size ceiling; both cases are flagged explicitly
    (``truncated: true``) rather than silently dropping data.
  * Important events are also emitted through stdlib ``logging``, configured
    consistently for every entrypoint by ``modules.logging_config``.
  * A failure inside this module (disk full, permission error, …) must
    never propagate to the caller: the actual experiment/mailbox pipeline
    must keep running unaffected.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules import redaction

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("nexus.experiment")

FALLBACK_LOG_DIR = "state/experiment-logs"


def resolve_log_dir(env: dict[str, str] | None = None) -> str:
    """Where experiment logs are written.

    Configurable for the same reason AGENT_DB_PATH is: a bare relative path
    means every process that imports this module writes into whatever
    ``state/experiment-logs`` resolves to from its working directory. The test
    suite did exactly that - a run dropped its files into the production
    directory (116 MB, 5203 files), mixing test turns into real history. That
    is how a first pass at measuring controller escalation for #158 reported
    test numbers as if they were production.

    The default stays a plain relative path so a fresh checkout works with no
    configuration; only an explicit ``AGENT_LOG_DIR`` overrides it.
    """
    source = os.environ if env is None else env
    return (source.get("AGENT_LOG_DIR") or "").strip() or FALLBACK_LOG_DIR


DEFAULT_LOG_DIR = resolve_log_dir()

# Prefix used for the durable, pre-experiment mailbox task log file
# (``<log_dir>/task-<mailbox_task_id>.jsonl``), so a single directory
# listing/glob naturally covers both families of JSONL log for the
# unified log feed.
TASK_LOG_PREFIX = "task-"
CHAT_LOG_PREFIX = "chat-"
SYSTEM_LOG_KEY = "system"

# Any id used to build a log file path (an experiment id or a mailbox
# task/approval id) must match this allowlist. Both are produced
# internally as ``uuid.uuid4().hex``, but this guards defensively against
# a corrupted/legacy record or a future caller passing something
# unexpected straight through to a filesystem path.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Per-entry bounds. Generous enough to stay diagnostically useful, small
# enough that one bad loop cannot blow up the log file on its own (the
# per-file ``max_bytes`` ceiling below is the actual hard backstop).
MAX_MESSAGE_CHARS = 4000
MAX_COMMAND_CHARS = 1000
MAX_STDOUT_CHARS = 4000
MAX_STDERR_CHARS = 4000
MAX_STACKTRACE_CHARS = 4000

# Hard ceiling on one experiment's JSONL log file. Once exceeded, further
# entries are dropped (a single terminal marker entry is written once)
# rather than growing without bound.
DEFAULT_MAX_LOG_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_SYSTEM_LOG_BACKUP_COUNT = 3

# Rotation applies to every log file, not only the system one. Without
# it, an experiment/task/chat log that hits DEFAULT_MAX_LOG_FILE_BYTES
# stops recording (one notice entry, then nothing), so exactly the long
# runs an operator most needs to inspect lose their tail.
DEFAULT_LOG_BACKUP_COUNT = 3

_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_LEVEL_TO_PY = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

# One lock per absolute log file path so concurrent writers (mailbox worker
# thread + experiment pipeline thread) serialize their appends without
# forcing unrelated experiments' log files to contend with each other.
_locks_guard = threading.Lock()
_file_locks: dict[str, threading.Lock] = {}


def _lock_for(path: str) -> threading.Lock:
    with _locks_guard:
        lock = _file_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _file_locks[path] = lock
        return lock


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _bounded_text(text: str | None, max_chars: int) -> tuple[str | None, bool]:
    """Redact then bound *text*; returns ``(value, truncated)``."""
    if text is None:
        return None, False
    cleaned = redaction.redact(str(text))
    if len(cleaned) <= max_chars:
        return cleaned, False
    return cleaned[:max_chars] + "\u2026", True


_TRUNCATION_MARKER_TEMPLATE = "...[TRUNCATED: {n} characters omitted]..."


def _bounded_head_tail_text(text: str | None, max_chars: int) -> tuple[str | None, bool]:
    """Redact then bound *text*, keeping both the beginning and the end.

    Diagnostic process output (``stdout``/``stderr``) commonly has its most
    useful content at *both* ends: e.g. a unittest run prints the command
    and early setup near the start, but the final failure summary
    (``FAILED (failures=1)``), failing test names, and traceback tail are
    the very last lines. A head-only truncation (as ``_bounded_text`` does
    for other fields) silently discards exactly that summary. This keeps a
    head slice and a tail slice, joined by an explicit, human-readable
    marker naming how many characters were omitted in between, and never
    exceeds *max_chars* in total (the marker's own length is accounted for
    up front, using the input length as a safe upper bound on how many
    digits the omitted-count can have).
    """
    if text is None:
        return None, False
    cleaned = redaction.redact(str(text))
    total_len = len(cleaned)
    if total_len <= max_chars:
        return cleaned, False

    # Reserve enough room for the marker up front: the number of omitted
    # characters can never exceed total_len, so sizing the reservation off
    # total_len's own digit count guarantees the real marker (built from
    # the *actual*, necessarily smaller, omitted count) never overflows it.
    reserved = len(_TRUNCATION_MARKER_TEMPLATE.format(n=total_len))
    available = max_chars - reserved
    if available <= 0:
        # max_chars is too small to fit both a head, a tail, and the
        # marker meaningfully; fall back to the simple head-only scheme
        # used elsewhere rather than producing something unreadable.
        return cleaned[:max_chars] + "\u2026", True

    head_len = available // 2
    tail_len = available - head_len
    omitted = total_len - head_len - tail_len
    marker = _TRUNCATION_MARKER_TEMPLATE.format(n=omitted)
    tail = cleaned[-tail_len:] if tail_len > 0 else ""
    result = cleaned[:head_len] + marker + tail
    return result, True


def sanitize_command(command: list[str] | str | None) -> str | None:
    """Bounded, redacted, human-readable rendering of a command for logs.

    Never returns full unbounded argv (e.g. an entire LLM prompt passed as
    the final argument) — only enough to identify what ran.
    """
    if command is None:
        return None
    if isinstance(command, (list, tuple)):
        text = " ".join(str(part) for part in command)
    else:
        text = str(command)
    cleaned, _ = _bounded_text(text, MAX_COMMAND_CHARS)
    return cleaned


class ExperimentLogger:
    """Append-only structured JSONL logger for one experiment, or for a
    mailbox task's pre-experiment events before any experiment exists.

    Not shared global state: callers construct one per experiment/task
    (cheap; holds only a path and small config), but concurrent instances
    pointing at the same file are safe because the per-path lock is
    process-global.

    When *experiment_id* is given, entries are written to
    ``<log_dir>/<experiment_id>.jsonl`` exactly as before. When it is
    ``None`` (mailbox intake, queueing/orchestration, or a task that never
    reaches an experiment), *mailbox_task_id* is required and entries go
    to ``<log_dir>/task-<mailbox_task_id>.jsonl`` instead, still readable
    through the same ``read_log_entries``/``read_all_log_entries`` path -
    this is what lets the unified Logs feed show pre-experiment-id events
    at all, correlated onward via ``mailbox_task_id``.
    """

    def __init__(
        self,
        experiment_id: str | None,
        mailbox_task_id: str | None = None,
        job_id: str | None = None,
        session_id: str | None = None,
        log_dir: str | Path = DEFAULT_LOG_DIR,
        max_bytes: int = DEFAULT_MAX_LOG_FILE_BYTES,
        *,
        _system: bool = False,
        _mirror: bool = True,
        _rotate_backups: int = DEFAULT_LOG_BACKUP_COUNT,
    ) -> None:
        if experiment_id:
            if not _SAFE_ID_RE.fullmatch(experiment_id):
                raise ValueError(f"unsafe experiment_id for log path: {experiment_id!r}")
            key = experiment_id
        elif job_id:
            if not _SAFE_ID_RE.fullmatch(job_id):
                raise ValueError(f"unsafe job_id for log path: {job_id!r}")
            key = f"{CHAT_LOG_PREFIX}{job_id}"
        elif _system:
            key = SYSTEM_LOG_KEY
        else:
            if not mailbox_task_id or not _SAFE_ID_RE.fullmatch(mailbox_task_id):
                raise ValueError(
                    "ExperimentLogger requires an experiment_id, or a valid "
                    "mailbox_task_id when no experiment_id is known yet"
                )
            key = f"{TASK_LOG_PREFIX}{mailbox_task_id}"
        self.experiment_id = experiment_id
        self.mailbox_task_id = mailbox_task_id
        self.job_id = redaction.redact(job_id) if job_id else None
        self.session_id = redaction.redact(session_id) if session_id else None
        self._dir = Path(log_dir)
        self._key = key
        self._path = self._dir / f"{key}.jsonl"
        self._max_bytes = max_bytes
        self._limit_notice_written = False
        self._mirror = _mirror
        self._rotate_backups = max(0, _rotate_backups)

    @property
    def path(self) -> Path:
        return self._path

    def bind_mailbox_task_id(self, mailbox_task_id: str | None) -> None:
        """Attach a mailbox task id discovered after this logger was created."""
        if mailbox_task_id:
            self.mailbox_task_id = mailbox_task_id

    def bind_session_id(self, session_id: str | None) -> None:
        if session_id:
            self.session_id = redaction.redact(session_id)

    # ------------------------------------------------------------------
    # Convenience level methods
    # ------------------------------------------------------------------

    def debug(self, phase: str, event: str, message: str, **extra: Any) -> None:
        self._safe_log("DEBUG", phase, event, message, **extra)

    def info(self, phase: str, event: str, message: str, **extra: Any) -> None:
        self._safe_log("INFO", phase, event, message, **extra)

    def warning(self, phase: str, event: str, message: str, **extra: Any) -> None:
        self._safe_log("WARNING", phase, event, message, **extra)

    def error(
        self,
        phase: str,
        event: str,
        message: str,
        exc: BaseException | None = None,
        **extra: Any,
    ) -> None:
        if exc is not None:
            extra.setdefault("exception_type", type(exc).__name__)
            extra.setdefault("exception_message", str(exc))
            extra.setdefault("stacktrace", _format_stacktrace(exc))
        self._safe_log("ERROR", phase, event, message, **extra)

    def log_process(
        self,
        phase: str,
        event: str,
        message: str,
        *,
        command: list[str] | str | None = None,
        cwd: str | None = None,
        duration_ms: int | None = None,
        exit_code: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        timed_out: bool = False,
        level: str | None = None,
        **extra: Any,
    ) -> None:
        """Log the outcome of an external process/subprocess invocation.

        Chooses a sensible default level when *level* is not given: ERROR
        on timeout or a non-zero/unknown exit code, INFO otherwise.
        """
        if level is None:
            if timed_out or (exit_code is not None and exit_code != 0):
                level = "ERROR"
            else:
                level = "INFO"
        if command is not None:
            extra["command"] = sanitize_command(command)
        if cwd is not None:
            extra["cwd"] = cwd
        if timed_out:
            extra["timed_out"] = True
        self._safe_log(
            level,
            phase,
            event,
            message,
            duration_ms=duration_ms,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            **extra,
        )

    # ------------------------------------------------------------------
    # Core write path
    # ------------------------------------------------------------------

    def _safe_log(self, level: str, phase: str, event: str, message: str, **extra: Any) -> None:
        """Never let a logging failure escape to the caller."""
        try:
            self._log(level, phase, event, message, **extra)
        except Exception:  # noqa: BLE001 - logging must never crash the pipeline
            logger.exception(
                "central_logging: failed to write log entry (experiment=%s phase=%s event=%s)",
                self.experiment_id[:8] if self.experiment_id else "?",
                phase,
                event,
            )

    def _log(
        self,
        level: str,
        phase: str,
        event: str,
        message: str,
        *,
        duration_ms: int | None = None,
        command: str | None = None,
        cwd: str | None = None,
        exit_code: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        exception_type: str | None = None,
        exception_message: str | None = None,
        stacktrace: str | None = None,
        **extra: Any,
    ) -> None:
        level = level.upper()
        if level not in _VALID_LEVELS:
            level = "INFO"

        truncated = False
        bounded_message, msg_truncated = _bounded_text(message, MAX_MESSAGE_CHARS)
        truncated = truncated or msg_truncated

        entry: dict[str, Any] = {
            "timestamp": _utcnow_iso(),
            "level": level,
            "logger": "nexus.experiment",
            "message": bounded_message,
            "experiment_id": self.experiment_id,
            "phase": phase,
            "event": event,
        }
        if self.mailbox_task_id:
            entry["mailbox_task_id"] = self.mailbox_task_id
        if self.job_id:
            entry["job_id"] = self.job_id
        if self.session_id:
            entry["session_id"] = self.session_id
        if duration_ms is not None:
            entry["duration_ms"] = int(duration_ms)
        if command is not None:
            entry["command"] = redaction.redact(str(command))[:MAX_COMMAND_CHARS]
        if cwd is not None:
            entry["cwd"] = str(cwd)
        if exit_code is not None:
            entry["exit_code"] = int(exit_code)
        if stdout is not None:
            bounded_stdout, out_truncated = _bounded_head_tail_text(stdout, MAX_STDOUT_CHARS)
            entry["stdout"] = bounded_stdout
            truncated = truncated or out_truncated
        if stderr is not None:
            bounded_stderr, err_truncated = _bounded_head_tail_text(stderr, MAX_STDERR_CHARS)
            entry["stderr"] = bounded_stderr
            truncated = truncated or err_truncated
        if exception_type is not None:
            entry["exception_type"] = exception_type
        if exception_message is not None:
            bounded_exc_msg, exc_truncated = _bounded_text(exception_message, MAX_MESSAGE_CHARS)
            entry["exception_message"] = bounded_exc_msg
            truncated = truncated or exc_truncated
        if stacktrace is not None:
            bounded_trace, trace_truncated = _bounded_text(stacktrace, MAX_STACKTRACE_CHARS)
            entry["stacktrace"] = bounded_trace
            truncated = truncated or trace_truncated

        # Any extra explicitly-allowed structured fields the caller passed
        # (e.g. policy_rule, retry_count, sandbox, timed_out). Redact any
        # string value defensively; never accept nested unsanitized blobs.
        for key, value in extra.items():
            if value is None:
                continue
            if isinstance(value, str):
                cleaned, extra_truncated = _bounded_text(value, MAX_MESSAGE_CHARS)
                entry[key] = cleaned
                truncated = truncated or extra_truncated
            elif isinstance(value, (int, float, bool)):
                entry[key] = value
            else:
                # Never write arbitrary/unsanitized structures; stringify
                # and redact rather than risk leaking something unbounded.
                cleaned, extra_truncated = _bounded_text(str(value), MAX_MESSAGE_CHARS)
                entry[key] = cleaned
                truncated = truncated or extra_truncated

        if truncated:
            entry["truncated"] = True

        self._append(entry)
        if self._mirror:
            self._mirror_to_journald(level, phase, event, message)

    def _append(self, entry: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path_key = str(self._path)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _lock_for(path_key), self._interprocess_lock():
            try:
                current_size = self._path.stat().st_size
            except FileNotFoundError:
                current_size = 0

            if (
                self._rotate_backups
                and current_size > 0
                and current_size + len(line.encode("utf-8")) > self._max_bytes
            ):
                self._rotate()
                current_size = 0

            if current_size >= self._max_bytes:
                if not self._limit_notice_written:
                    notice = {
                        "timestamp": _utcnow_iso(),
                        "level": "WARNING",
                        "logger": "nexus.experiment",
                        "message": "experiment log size limit reached; further entries suppressed",
                        "experiment_id": self.experiment_id,
                        "phase": entry.get("phase", "unknown"),
                        "event": "log_size_limit_reached",
                        "truncated": True,
                    }
                    self._write_line(json.dumps(notice, ensure_ascii=False) + "\n")
                    self._limit_notice_written = True
                return

            self._write_line(line)

    @contextmanager
    def _interprocess_lock(self):
        if not self._rotate_backups or fcntl is None:
            yield
            return
        lock_path = self._dir / f".{self._key}.lock"
        with open(lock_path, "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _rotate(self) -> None:
        """Bound retained logs while keeping the active file writable.

        Rotates this logger's *own* file set (``<key>-1.jsonl`` ...
        ``<key>-N.jsonl``), not just the system log's: without it an
        experiment/task log that reaches ``max_bytes`` stops accepting
        entries entirely, so the rest of that run - and anything an
        operator later searches for in it - is simply never recorded.
        Rotated files keep the ``.jsonl`` suffix and therefore stay part
        of the unified read/search path.
        """
        oldest = self._dir / f"{self._key}-{self._rotate_backups}.jsonl"
        oldest.unlink(missing_ok=True)
        for index in range(self._rotate_backups - 1, 0, -1):
            source = self._dir / f"{self._key}-{index}.jsonl"
            if source.exists():
                source.replace(self._dir / f"{self._key}-{index + 1}.jsonl")
        if self._path.exists():
            self._path.replace(self._dir / f"{self._key}-1.jsonl")

    def _write_line(self, line: str) -> None:
        # Open/append/flush/fsync per line: entries must survive a crash
        # or SSH disconnect immediately after being written, not only at
        # process exit or file close.
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def _mirror_to_journald(self, level: str, phase: str, event: str, message: str) -> None:
        # Journald only ever gets a short, safe summary line - large
        # process output/stack traces stay in the JSONL file only.
        short_message = redaction.redact(str(message))[:300]
        task_part = f" task={self.mailbox_task_id[:8]}" if self.mailbox_task_id else ""
        logger.log(
            _LEVEL_TO_PY.get(level, logging.INFO),
            "experiment=%s%s phase=%s event=%s: %s",
            self.experiment_id[:8] if self.experiment_id else "?",
            task_part,
            phase,
            event,
            short_message,
        )


def get_task_logger(
    mailbox_task_id: str,
    log_dir: str | Path = DEFAULT_LOG_DIR,
    max_bytes: int = DEFAULT_MAX_LOG_FILE_BYTES,
) -> "ExperimentLogger":
    """Durable structured logger for one mailbox task's pre-experiment
    events (intake, approval/rejection, queueing/orchestration, GitHub
    lifecycle preparation) - before any experiment id exists, and even
    for tasks that never create one (answered/rejected/needs review).

    Writes to ``<log_dir>/task-<mailbox_task_id>.jsonl``; the same file
    keeps receiving events after an experiment is created (e.g. the
    ``experiment_created`` binding event), while the experiment's own
    subsequent pipeline events go to its own ``<experiment_id>.jsonl``
    via ``ExperimentLogger``/``ExperimentService.get_experiment_logger``.
    Both are merged chronologically by ``read_all_log_entries``.
    """
    return ExperimentLogger(None, mailbox_task_id=mailbox_task_id, log_dir=log_dir, max_bytes=max_bytes)


def get_chat_logger(
    job_id: str,
    session_id: str | None = None,
    log_dir: str | Path = DEFAULT_LOG_DIR,
    max_bytes: int = DEFAULT_MAX_LOG_FILE_BYTES,
) -> "ExperimentLogger":
    """Durable structured logger for a chat job before an experiment exists."""
    return ExperimentLogger(
        None,
        job_id=job_id,
        session_id=session_id,
        log_dir=log_dir,
        max_bytes=max_bytes,
    )


def get_system_logger(
    log_dir: str | Path = DEFAULT_LOG_DIR,
    max_bytes: int = DEFAULT_MAX_LOG_FILE_BYTES,
    backup_count: int = DEFAULT_SYSTEM_LOG_BACKUP_COUNT,
) -> "ExperimentLogger":
    """Structured sink used by the centralized stdlib logging handler."""
    return ExperimentLogger(
        None,
        log_dir=log_dir,
        max_bytes=max_bytes,
        _system=True,
        _mirror=False,
        _rotate_backups=backup_count,
    )


def _format_stacktrace(exc: BaseException) -> str:
    """Bounded, redacted stack trace text (last frames only)."""
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    text = "".join(lines)
    return redaction.redact(text)


# ---------------------------------------------------------------------------
# Read path (used by the API)
# ---------------------------------------------------------------------------


# Per-file byte cap applied only when scanning *many* files at once (the
# unified "Alle Experimente" feed). The per-experiment file itself is
# already capped at DEFAULT_MAX_LOG_FILE_BYTES (5 MB) by the writer, but
# multiplied across ``DEFAULT_MAX_LOG_FILES`` (500) that would still mean
# a single manual refresh could read on the order of gigabytes. Only the
# most recent slice of each file is read for that feed; a single-file,
# single-experiment read (``read_log_entries`` used directly, or the
# unified feed filtered to one ``experiment_id``) is unaffected and still
# reads the complete (already-bounded) file.
DEFAULT_UNIFIED_TAIL_BYTES = 256 * 1024  # 256 KiB per file

# A search switches the tail shortcut off and reads candidate files in
# full (see ``scan_log_entries``), so it needs its own ceiling: enough to
# cover the whole retained history in practice (each file is already
# capped at ``DEFAULT_MAX_LOG_FILE_BYTES``), small enough that one query
# cannot read unbounded data on the 1 GB VM. Files are visited
# newest-modified first, so hitting this bound drops only the oldest
# candidates - and the result is flagged ``truncated``.
DEFAULT_SEARCH_SCAN_BUDGET_BYTES = 64 * 1024 * 1024  # 64 MiB per request


def _tail_read_lines(path: Path, tail_bytes: int | None) -> list[str]:
    """Return *path*'s lines, optionally reading only the last *tail_bytes*
    bytes. A partial first line produced by seeking mid-file is dropped
    rather than risking a corrupt/incomplete JSON object."""
    if tail_bytes is None:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.readlines()

    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        if size > tail_bytes:
            fh.seek(size - tail_bytes)
            data = fh.read()
            newline_index = data.find(b"\n")
            data = data[newline_index + 1 :] if newline_index != -1 else b""
        else:
            fh.seek(0)
            data = fh.read()
    return data.decode("utf-8", errors="replace").splitlines()


_REQUIRED_STRING_FIELDS = ("timestamp", "level", "logger", "phase", "event")
_OPTIONAL_STRING_FIELDS = (
    "message", "experiment_id", "mailbox_task_id", "job_id", "session_id", "command", "cwd",
    "stdout", "stderr", "exception_type", "exception_message", "stacktrace",
)
_OPTIONAL_INT_FIELDS = ("duration_ms", "exit_code")


def _is_well_formed_entry(entry: dict[str, Any]) -> bool:
    """Defensive schema check for one already-JSON-parsed log line.

    A syntactically valid JSON object that is missing a required field or
    has the wrong type for a field must be skipped *here* - the one place
    shared by every reader - rather than surfacing as an unhandled
    validation error once the API layer builds its response model, which
    would otherwise fail the *entire* feed for one bad line.
    """
    for field in _REQUIRED_STRING_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value:
            return False
    for field in _OPTIONAL_STRING_FIELDS:
        value = entry.get(field)
        if value is not None and not isinstance(value, str):
            return False
    for field in _OPTIONAL_INT_FIELDS:
        value = entry.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            return False
    truncated = entry.get("truncated")
    if truncated is not None and not isinstance(truncated, bool):
        return False
    return True


def read_log_entries(
    log_path: str | Path,
    *,
    level: str | None = None,
    phase: str | None = None,
    job_id: str | None = None,
    session_id: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    before: str | None = None,
    tail_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Read one experiment's JSONL log, defensively re-sanitized, chronological.

    Malformed lines (invalid JSON, a non-object, or an object missing a
    required field/of the wrong type) are skipped rather than raising.
    Every string field is redacted again on the way out: a defense-in-depth
    measure independent of the write-path redaction. When *tail_bytes* is
    given, only that many trailing bytes of the file are read (bounded
    I/O for callers scanning many files at once); *before*, if given,
    drops entries whose ``timestamp`` string sorts at or after it (a
    plain string-timestamp cursor - callers that must page correctly
    across entries sharing a timestamp, or across several files, use the
    opaque ``encode_log_cursor``/``decode_log_cursor`` pair together with
    ``_entry_sort_key`` instead; see ``read_experiment_logs`` and
    ``read_all_log_entries``).

    Each returned entry also carries two internal-only keys, ``_source``
    (the file's name) and ``_rseq`` (its distance from the newest parsed
    line in *this* read, 0 = newest): a stable tie-breaker so that two
    entries sharing an identical ``timestamp`` - whether in the same file
    or different files - still have a well-defined, reproducible order
    across separate reads/pages. Both keys are stripped by the API layer
    before a response model is built and are otherwise harmless (an
    unknown key is ignored by the allowlisted Pydantic models).
    """
    path = Path(log_path)
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    for raw_line in _tail_read_lines(path, tail_bytes):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if not _is_well_formed_entry(entry):
            continue
        entries.append(_resanitize_entry(entry))

    # Attach the tie-breaker *before* any filtering below, so it reflects
    # each entry's stable position in the file rather than its position
    # in a level/phase-filtered subset (which could otherwise differ
    # between two requests using different filters against the same
    # underlying file/window).
    source_name = path.name
    total = len(entries)
    for index, entry in enumerate(entries):
        entry["_source"] = source_name
        entry["_rseq"] = total - 1 - index

    if level:
        level_upper = level.upper()
        entries = [e for e in entries if e.get("level") == level_upper]
    if phase:
        entries = [e for e in entries if e.get("phase") == phase]
    if job_id:
        entries = [e for e in entries if e.get("job_id") == job_id]
    if session_id:
        entries = [e for e in entries if e.get("session_id") == session_id]
    if search:
        entries = [e for e in entries if entry_matches_search(e, search)]
    if before:
        entries = [e for e in entries if str(e.get("timestamp") or "\uffff") < before]

    if limit is not None and limit >= 0 and len(entries) > limit:
        # Keep the most recent *limit* entries, still in chronological order.
        entries = entries[-limit:]

    return entries


_STRING_FIELDS_TO_REDACT = (
    "message",
    "command",
    "stdout",
    "stderr",
    "exception_message",
    "stacktrace",
    "job_id",
    "session_id",
)


_SEARCHABLE_FIELDS = (
    "message",
    "event",
    "phase",
    "logger",
    "command",
    "cwd",
    "stdout",
    "stderr",
    "exception_type",
    "exception_message",
    "stacktrace",
    "experiment_id",
    "mailbox_task_id",
    "job_id",
    "session_id",
    "source",
)


def entry_matches_search(entry: dict[str, Any], search: str) -> bool:
    """Case-insensitive substring match across one entry's text fields.

    Matching deliberately spans identifiers (experiment/task/job/session)
    and execution detail (command/cwd/stdout/stderr/exception) as well as
    the message, so an operator can find a run by *any* fragment they
    happen to remember - an id prefix, part of a command line, or a
    phrase from an error - rather than only by message text.

    Applied inside the read path (see ``read_log_entries``) rather than
    on an already-paginated page, so a match older than the newest page
    is actually found instead of silently returning nothing.
    """
    needle = search.strip().lower()
    if not needle:
        return True
    for field in _SEARCHABLE_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and needle in value.lower():
            return True
    return False


def _resanitize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(entry)
    for field in _STRING_FIELDS_TO_REDACT:
        value = cleaned.get(field)
        if isinstance(value, str):
            cleaned[field] = redaction.redact(value)
    return cleaned


def _entry_sort_key(entry: dict[str, Any]) -> tuple[str, str, str, int]:
    """Total, reproducible chronological order for one entry.

    Primary key is the ISO-8601 ``timestamp`` string (missing timestamps
    sort last via the ``\uffff`` sentinel). Ties - two entries with an
    identical timestamp, whether from the same file or different files -
    are broken by ``experiment_id`` (entries recorded before an
    experiment id exists - e.g. mailbox intake - carry no ``experiment_id``
    and therefore sort first, matching the real task path where those
    events happen earlier), then by ``_source`` (file name) for
    determinism across two different files sharing both a timestamp and
    an ``experiment_id``, and finally by ``-_rseq`` so that, within one
    file, the entry appended *earlier* (further from the end, i.e. a
    larger ``_rseq``) always sorts first. The ``_source``/``_rseq``
    fields are attached by ``read_log_entries``; an entry missing them
    (e.g. a hand-built dict in a test) is treated as the newest possible
    entry for its file/timestamp so it never silently masks real data.
    """
    timestamp = str(entry.get("timestamp") or "\uffff")
    experiment_id = str(entry.get("experiment_id") or "")
    source = str(entry.get("_source") or "")
    rseq = entry.get("_rseq")
    rseq = rseq if isinstance(rseq, int) and not isinstance(rseq, bool) else 0
    return (timestamp, experiment_id, source, -rseq)


def entry_sort_key(entry: dict[str, Any]) -> tuple[str, str, str, int]:
    """Public alias of :func:`_entry_sort_key`.

    ``GET /logs`` (``api.py``) merges non-structured entries (systemd
    journal lines, normalized into this same shape by
    ``modules.log_reader``) into the same timeline/pagination cursor as
    structured JSONL entries and must sort/paginate both with the exact
    same total order used here - reaching into a private helper from
    another module would be worse than exposing this one intentionally.
    """
    return _entry_sort_key(entry)


def encode_log_cursor(entry: dict[str, Any]) -> str:
    """Opaque, URL-safe pagination cursor anchored to one entry.

    Encodes the same ``(timestamp, experiment_id, source, rseq)``
    quadruple used by ``_entry_sort_key`` so a follow-up call can resume
    *exactly* after this entry - no earlier, no later - even when several
    entries share an identical timestamp. Never a bare timestamp string:
    that alone cannot disambiguate ties and previously caused entries to
    be skipped or duplicated at a page boundary.
    """
    timestamp = str(entry.get("timestamp") or "")
    experiment_id = str(entry.get("experiment_id") or "")
    source = str(entry.get("_source") or "")
    rseq = entry.get("_rseq")
    rseq = rseq if isinstance(rseq, int) and not isinstance(rseq, bool) else 0
    payload = json.dumps([timestamp, experiment_id, source, rseq], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_log_cursor(cursor: str) -> tuple[str, str, str, int] | None:
    """Inverse of ``encode_log_cursor``; ``None`` for anything else (an
    empty/legacy/corrupt cursor is treated as "no cursor" - restart from
    the newest entries - rather than failing the whole request)."""
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        decoded = json.loads(payload)
        timestamp, experiment_id, source, rseq = decoded
        if not isinstance(timestamp, str) or not isinstance(experiment_id, str) or not isinstance(source, str):
            return None
        if not isinstance(rseq, int) or isinstance(rseq, bool):
            return None
        return (timestamp, experiment_id, source, -rseq)
    except Exception:
        return None


# Hard ceiling on how many per-experiment JSONL files a single unified-feed
# read will open, newest-modified first. Bounds worst-case I/O for the
# "Alle Experimente" view regardless of how many experiments have ever run.
DEFAULT_MAX_LOG_FILES = 500


def read_experiment_logs(
    log_dir: str | Path,
    experiment_id: str,
    *,
    mailbox_task_id: str | None = None,
    level: str | None = None,
    phase: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """Chronological view of one experiment, merged with its correlated
    mailbox task's pre-experiment log (mailbox intake, approval/rejection,
    queueing/orchestration) when one exists, so selecting a single
    experiment shows the same complete task path as the unified feed.

    *mailbox_task_id* must already be resolved/validated server-side (e.g.
    via ``ExperimentService.mailbox_task_id_for_experiment``), never taken
    directly from unvalidated request input, since it is used to build a
    second file path. *experiment_id* is defense-in-depth validated against
    the same safe-id pattern before being used to build a path; an unsafe
    value yields an empty result rather than touching the filesystem.

    *before*, if given, is an opaque cursor from ``encode_log_cursor``
    (see ``read_all_log_entries`` for why a bare timestamp cannot page
    correctly); an unrecognized/legacy cursor is treated as "no cursor".
    """
    if not _SAFE_ID_RE.fullmatch(experiment_id or ""):
        return []

    directory = Path(log_dir)
    if not directory.is_dir():
        raise OSError(f"log directory is unavailable: {directory}")
    paths = [directory / f"{experiment_id}.jsonl"]
    if mailbox_task_id and _SAFE_ID_RE.fullmatch(mailbox_task_id):
        paths.append(directory / f"{TASK_LOG_PREFIX}{mailbox_task_id}.jsonl")

    entries: list[dict[str, Any]] = []
    for path in paths:
        entries.extend(read_log_entries(path, level=level, phase=phase, search=search))

    entries.sort(key=_entry_sort_key)

    if before:
        cursor_key = decode_log_cursor(before)
        if cursor_key is not None:
            entries = [e for e in entries if _entry_sort_key(e) < cursor_key]

    if limit is not None and limit >= 0 and len(entries) > limit:
        entries = entries[-limit:]
    return entries


@dataclass(frozen=True)
class LogScanResult:
    """Entries from one unified read, plus how complete that read was.

    ``truncated`` is True when the scan stopped early - because the
    per-request byte budget ran out or ``max_files`` capped the candidate
    list - so a caller can tell "no older matches exist" apart from "I
    stopped looking", and say so instead of implying the history is
    exhausted.
    """

    entries: list[dict[str, Any]]
    truncated: bool = False
    files_scanned: int = 0
    bytes_scanned: int = 0


def scan_log_entries(
    log_dir: str | Path,
    *,
    experiment_id: str | None = None,
    mailbox_task_id: str | None = None,
    level: str | None = None,
    phase: str | None = None,
    job_id: str | None = None,
    session_id: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    before: str | None = None,
    max_files: int = DEFAULT_MAX_LOG_FILES,
    tail_bytes: int | None = DEFAULT_UNIFIED_TAIL_BYTES,
    scan_budget_bytes: int | None = DEFAULT_SEARCH_SCAN_BUDGET_BYTES,
) -> LogScanResult:
    """Unified, chronological view across every experiment's (and every
    pre-experiment mailbox task's) JSONL log.

    When *search* is given, the newest-tail shortcut described below is
    switched off and each candidate file is read in full, so a match
    older than the default window is actually found rather than silently
    missing. That whole-history scan is bounded by *scan_budget_bytes*
    (and still by *max_files*); if either bound stops the scan early the
    result is flagged ``truncated`` instead of being passed off as a
    complete answer.

    Backs the Logs tab's default "Alle Experimente" feed: an operator must
    be able to see a task's complete, real structured log entries (mailbox
    intake through GitHub lifecycle/adoption) without first knowing or
    selecting an experiment id. Reuses ``read_log_entries`` per file, so
    the same defensive re-sanitization and malformed-line skipping apply
    uniformly. Mailbox intake/queueing events recorded before an
    experiment id exists live in ``task-<mailbox_task_id>.jsonl`` files
    (see ``get_task_logger``), which this glob picks up automatically.

    When *experiment_id* is given, only that experiment (merged with its
    correlated task log, via ``read_experiment_logs``) is read. Otherwise,
    every file under *log_dir* is scanned, bounded by *max_files*
    (newest-modified first).

    *before*, if given, must be an opaque cursor produced by
    ``encode_log_cursor`` for a previously-returned entry (an
    unrecognized/legacy cursor - e.g. a bare timestamp - is treated as
    "no cursor", restarting from the newest entries, rather than failing
    the request). A plain timestamp cannot disambiguate two entries that
    share it, which previously caused the entry exactly at a page
    boundary to be silently dropped forever.

    The very first ("no cursor") request only reads the last *tail_bytes*
    of each file - bounded I/O for the common "just refresh and see the
    latest" case. As soon as a caller pages further back (*before* is a
    valid cursor), each candidate file is read in full instead: an
    explicit, comparatively rare "load older" action may cost more I/O
    than the default view, but only reading a bounded tail can never
    reach entries older than that window, however far back the cursor
    points (this was the *"pagination can never reach entries before the
    current tail"* case) - correctness beats bounding I/O once the
    operator has explicitly asked to go back further. *max_files* still
    caps how many distinct experiments/tasks are scanned either way.
    """
    directory = Path(log_dir)
    if not directory.is_dir():
        raise OSError(f"log directory is unavailable: {directory}")

    if experiment_id:
        entries = read_experiment_logs(
            directory, experiment_id,
            mailbox_task_id=mailbox_task_id,
            level=level, phase=phase, search=search,
            limit=None if (job_id or session_id) else limit,
            before=before,
        )
        if job_id:
            entries = [entry for entry in entries if entry.get("job_id") == job_id]
        if session_id:
            entries = [entry for entry in entries if entry.get("session_id") == session_id]
        if limit is not None and limit >= 0 and len(entries) > limit:
            entries = entries[-limit:]
        return LogScanResult(entries=entries, files_scanned=1)

    cursor_key: tuple[str, str, str, int] | None = decode_log_cursor(before) if before else None
    searching = bool(search and search.strip())
    # A search must be answered from the whole retained history: reading
    # only each file's newest tail would report "no matches" for anything
    # older than that window, which is indistinguishable from "no such
    # entry ever existed".
    effective_tail_bytes = None if (cursor_key is not None or searching) else tail_bytes

    candidates_with_mtime: list[tuple[float, Path]] = []
    for path in directory.glob("*.jsonl"):
        try:
            if path.is_file():
                candidates_with_mtime.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue
    candidates_with_mtime.sort(key=lambda item: item[0], reverse=True)
    candidates = [path for _, path in candidates_with_mtime]
    truncated = False
    if max_files is not None and max_files >= 0 and len(candidates) > max_files:
        candidates = candidates[:max_files]
        truncated = True
    paths = candidates

    entries: list[dict[str, Any]] = []
    bytes_scanned = 0
    files_scanned = 0
    for path in paths:
        if (
            effective_tail_bytes is None
            and scan_budget_bytes is not None
            and bytes_scanned >= scan_budget_bytes
        ):
            # Budget spent. Files are visited newest-modified first, so
            # what remains unscanned is strictly older than everything
            # already collected.
            truncated = True
            break
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            entries.extend(
                read_log_entries(
                    path,
                    level=level,
                    phase=phase,
                    job_id=job_id,
                    session_id=session_id,
                    search=search,
                    tail_bytes=effective_tail_bytes,
                )
            )
        except FileNotFoundError:
            continue
        files_scanned += 1
        bytes_scanned += size if effective_tail_bytes is None else min(size, effective_tail_bytes)

    if mailbox_task_id:
        entries = [e for e in entries if e.get("mailbox_task_id") == mailbox_task_id]

    # Missing timestamps (should not occur in practice - every write path
    # always sets one) sort last rather than first, so a malformed entry
    # can never mask genuinely older/newer ones at the top of the feed.
    entries.sort(key=_entry_sort_key)

    if cursor_key is not None:
        entries = [e for e in entries if _entry_sort_key(e) < cursor_key]

    if limit is not None and limit >= 0 and len(entries) > limit:
        entries = entries[-limit:]

    return LogScanResult(
        entries=entries,
        truncated=truncated,
        files_scanned=files_scanned,
        bytes_scanned=bytes_scanned,
    )


def read_all_log_entries(
    log_dir: str | Path,
    *,
    experiment_id: str | None = None,
    mailbox_task_id: str | None = None,
    level: str | None = None,
    phase: str | None = None,
    job_id: str | None = None,
    session_id: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    before: str | None = None,
    max_files: int = DEFAULT_MAX_LOG_FILES,
    tail_bytes: int | None = DEFAULT_UNIFIED_TAIL_BYTES,
) -> list[dict[str, Any]]:
    """Entries-only view of :func:`scan_log_entries`.

    Kept as the plain-list entry point for callers that do not need to
    know whether the scan was complete.
    """
    return scan_log_entries(
        log_dir,
        experiment_id=experiment_id,
        mailbox_task_id=mailbox_task_id,
        level=level,
        phase=phase,
        job_id=job_id,
        session_id=session_id,
        search=search,
        limit=limit,
        before=before,
        max_files=max_files,
        tail_bytes=tail_bytes,
    ).entries
