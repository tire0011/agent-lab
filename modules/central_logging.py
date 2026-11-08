"""Centralized structured JSONL logging for Nexus.

One durable file per experiment/mailbox task/chat job, merged by the read path
into one chronological feed. Entries are redacted via ``modules.redaction``,
flushed+fsynced on write, and bounded with explicit truncation flags. A failure
in this module must never propagate to the caller.
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

    Overridable via ``AGENT_LOG_DIR``: a bare relative default would make every
    process write to whatever its working directory resolves to.
    """
    source = os.environ if env is None else env
    return (source.get("AGENT_LOG_DIR") or "").strip() or FALLBACK_LOG_DIR


DEFAULT_LOG_DIR = resolve_log_dir()

# "task-" prefix keeps mailbox task logs in the same directory, so one glob
# covers both log families in the unified feed.
TASK_LOG_PREFIX = "task-"
CHAT_LOG_PREFIX = "chat-"
SYSTEM_LOG_KEY = "system"

# Ids used to build log file paths must match this allowlist (internally uuid4
# hex); guards against a corrupted/legacy record reaching a filesystem path.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def is_safe_log_id(value: object) -> bool:
    """Is *value* usable as the id half of a log file name?

    Shared with path-building callers (diagnostic backend, #188) so they
    validate against the same allowlist instead of a drifting copy.
    """
    return isinstance(value, str) and bool(_SAFE_ID_RE.fullmatch(value))

# Per-entry bounds; the per-file max_bytes ceiling below is the hard backstop.
MAX_MESSAGE_CHARS = 4000
MAX_COMMAND_CHARS = 1000
MAX_STDOUT_CHARS = 4000
MAX_STDERR_CHARS = 4000
MAX_STACKTRACE_CHARS = 4000

# Hard ceiling on one log file: past it, entries are dropped (one terminal
# marker entry once) instead of growing without bound.
DEFAULT_MAX_LOG_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_SYSTEM_LOG_BACKUP_COUNT = 3

# Rotate every log file, not just the system one: a log at its ceiling stops
# recording, and the long runs an operator most needs would lose their tail.
DEFAULT_LOG_BACKUP_COUNT = 3

_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_LEVEL_TO_PY = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

# One lock per absolute log path: concurrent writers serialize their appends
# without unrelated log files contending with each other.
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
    """Redact then bound *text*, keeping both head and tail.

    Head-only truncation would drop the failure summary at the end of process
    output; the head+tail slices are joined by an explicit omitted-count marker
    and never exceed *max_chars*.
    """
    if text is None:
        return None, False
    cleaned = redaction.redact(str(text))
    total_len = len(cleaned)
    if total_len <= max_chars:
        return cleaned, False

    # Size the marker reservation off total_len's digit count: the omitted count
    # never exceeds total_len, so the real marker never overflows it.
    reserved = len(_TRUNCATION_MARKER_TEMPLATE.format(n=total_len))
    available = max_chars - reserved
    if available <= 0:
        # max_chars too small for head+tail+marker; fall back to head-only.
        return cleaned[:max_chars] + "\u2026", True

    head_len = available // 2
    tail_len = available - head_len
    omitted = total_len - head_len - tail_len
    marker = _TRUNCATION_MARKER_TEMPLATE.format(n=omitted)
    tail = cleaned[-tail_len:] if tail_len > 0 else ""
    result = cleaned[:head_len] + marker + tail
    return result, True


def sanitize_command(command: list[str] | str | None) -> str | None:
    """Bounded, redacted rendering of a command for logs - never the full argv
    (an entire LLM prompt may be the final argument)."""
    if command is None:
        return None
    if isinstance(command, (list, tuple)):
        text = " ".join(str(part) for part in command)
    else:
        text = str(command)
    cleaned, _ = _bounded_text(text, MAX_COMMAND_CHARS)
    return cleaned


class ExperimentLogger:
    """Append-only structured JSONL logger for one experiment, or for a mailbox
    task's pre-experiment events (``task-<id>.jsonl`` when *experiment_id* is
    None), both readable through the same unified feed path. Concurrent
    instances on one file are safe via the process-global per-path lock.
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

    def bind_session_id(self, session_id: str | None) -> None:
        if session_id:
            self.session_id = redaction.redact(session_id)

    # Convenience level methods

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
        """Log the outcome of an external process invocation; *level* defaults
        to ERROR on timeout/non-zero exit, INFO otherwise."""
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

    # Core write path

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

        # Redact extra string fields defensively; never accept nested unsanitized blobs.
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
                # Never write unsanitized structures; stringify, redact, and bound.
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

        Rotates this logger's own file set, not just the system log's: a log
        at ``max_bytes`` stops accepting entries, so without rotation the rest
        of the run would never be recorded. Rotated files keep the ``.jsonl``
        suffix and stay in the unified read path.
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
        # Open/append/flush/fsync per line: entries must survive a crash or SSH
        # disconnect right after the write, not just process exit.
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def _mirror_to_journald(self, level: str, phase: str, event: str, message: str) -> None:
        # Journald gets only a short redacted summary; large output stays in the JSONL file.
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
    """Durable logger for one mailbox task's pre-experiment events, written to
    ``<log_dir>/task-<mailbox_task_id>.jsonl`` and merged chronologically with
    the experiment's own log by ``read_all_log_entries``."""
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


# Read path (used by the API)

# Per-file byte cap when scanning many files at once: each file is already
# capped by the writer, but 500 files x 5 MB is still gigabytes per refresh.
# Single-experiment reads are unaffected and read the full (bounded) file.
DEFAULT_UNIFIED_TAIL_BYTES = 256 * 1024

# A search reads candidate files in full, so it needs its own ceiling: small
# enough that one query cannot read unbounded data on the 1 GB VM. Files are
# visited newest-modified first, so hitting the bound drops only the oldest
# candidates, and the result is flagged ``truncated``.
DEFAULT_SEARCH_SCAN_BUDGET_BYTES = 64 * 1024 * 1024


def _tail_read_lines(path: Path, tail_bytes: int | None) -> list[str]:
    """Return *path*'s lines, optionally only the last *tail_bytes* bytes; a
    partial first line from seeking mid-file is dropped to avoid a corrupt entry."""
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

    A bad line must be skipped here - the one place shared by every reader -
    rather than failing the entire feed at the API layer.
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
    """Read one log file defensively: malformed lines are skipped, string fields
    are re-redacted on the way out (defense-in-depth), *tail_bytes* reads only
    that trailing window, and *before* is an opaque cursor (``encode_log_cursor``).
    Entries carry internal ``_source``/``_rseq`` tie-breakers (stripped by the
    API layer) for a reproducible order across equal timestamps.
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

    # Attach the tie-breaker before filtering, so it reflects the entry's
    # stable position in the file, not in a filtered subset.
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
    """Case-insensitive substring match across one entry's text fields, so a run
    can be found by id, command fragment, or error phrase. Applied inside the
    read path, not on a paginated page, so older matches are still found."""
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
    """Total, reproducible chronological order: timestamp (missing sorts last),
    then experiment_id (pre-experiment entries first), then _source, then -_rseq
    (earlier entries in one file first). An entry missing _source/_rseq is
    treated as newest so it never masks real data.
    """
    timestamp = str(entry.get("timestamp") or "\uffff")
    experiment_id = str(entry.get("experiment_id") or "")
    source = str(entry.get("_source") or "")
    rseq = entry.get("_rseq")
    rseq = rseq if isinstance(rseq, int) and not isinstance(rseq, bool) else 0
    return (timestamp, experiment_id, source, -rseq)


def entry_sort_key(entry: dict[str, Any]) -> tuple[str, str, str, int]:
    """Public alias of :func:`_entry_sort_key`, so the Logs API can merge and
    paginate non-structured journal entries with the same total order."""
    return _entry_sort_key(entry)


def encode_log_cursor(entry: dict[str, Any]) -> str:
    """Opaque URL-safe pagination cursor encoding the same (timestamp,
    experiment_id, source, rseq) quadruple as the sort key, so paging resumes
    exactly after that entry even when timestamps tie - a bare timestamp cursor
    cannot disambiguate ties (#page-boundary drops)."""
    timestamp = str(entry.get("timestamp") or "")
    experiment_id = str(entry.get("experiment_id") or "")
    source = str(entry.get("_source") or "")
    rseq = entry.get("_rseq")
    rseq = rseq if isinstance(rseq, int) and not isinstance(rseq, bool) else 0
    payload = json.dumps([timestamp, experiment_id, source, rseq], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_log_cursor(cursor: str) -> tuple[str, str, str, int] | None:
    """Inverse of ``encode_log_cursor``; anything unrecognized is "no cursor"
    (restart from newest) rather than a failed request."""
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


# Hard ceiling on how many log files one unified-feed read opens, newest first.
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
    """Chronological view of one experiment merged with its correlated mailbox
    task log, so one experiment shows the same complete task path as the feed.

    *mailbox_task_id* must be resolved/validated server-side, never taken from
    unvalidated request input, since it builds a second file path; *experiment_id*
    is validated against the safe-id pattern first - an unsafe value yields an
    empty result rather than touching the filesystem. *before* is an opaque
    cursor from ``encode_log_cursor`` (a bare timestamp cannot page correctly).
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
    """Entries from one unified read, plus whether the read was complete.

    ``truncated`` means the scan stopped early (byte budget or max_files), so
    "no older matches" is distinguishable from "stopped looking".
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
    """Unified, chronological view across every log file under *log_dir*.

    The first ("no cursor") request reads only the last *tail_bytes* of each
    file; paging back or searching reads candidates in full, bounded by
    *scan_budget_bytes* and *max_files*, flagging ``truncated`` when stopped
    early - a tail-only read can never reach older entries, but correctness
    beats bounding I/O once the caller explicitly pages back. *before* must be
    an opaque cursor from ``encode_log_cursor``; unrecognized cursors are
    treated as "no cursor". With *experiment_id*, only that experiment (merged
    with its task log) is read.
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
    # A search must read whole files: a tail-only read would report "no matches"
    # for anything older than the window, indistinguishable from "never existed".
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
            # Budget spent; files were visited newest-modified first, so what
            # remains unscanned is strictly older than what was collected.
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

    # Missing timestamps sort last, so a malformed entry never masks real ones.
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
    """Entries-only view of :func:`scan_log_entries` for callers that do not
    need to know whether the scan was complete."""
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
