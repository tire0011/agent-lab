"""Centralized, structured, per-experiment logging for Nexus.

Every experiment gets exactly one durable, append-only JSONL log at
``state/experiment-logs/<experiment_id>.jsonl``. Each line is one complete,
self-contained JSON object describing a single event anywhere in the
Mailbox -> Approval -> ExperimentService -> Codex/Git/test pipeline, so a
single file makes an entire task/experiment run understandable end to end.

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
  * The same important events are *also* emitted through the stdlib
    ``logging`` module (already wired to journald via each entrypoint's
    existing ``logging.basicConfig``/uvicorn setup) — this module never
    replaces that, it complements it with the durable structured detail
    that is too large for a syslog line.
  * A failure inside this module (disk full, permission error, …) must
    never propagate to the caller: the actual experiment/mailbox pipeline
    must keep running unaffected.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules import redaction

logger = logging.getLogger("nexus.experiment")

DEFAULT_LOG_DIR = "state/experiment-logs"

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
    """Append-only structured JSONL logger for exactly one experiment.

    Not shared global state: callers construct one per experiment (cheap;
    holds only a path and small config), but concurrent instances pointing
    at the same file are safe because the per-path lock is process-global.
    """

    def __init__(
        self,
        experiment_id: str,
        mailbox_task_id: str | None = None,
        log_dir: str | Path = DEFAULT_LOG_DIR,
        max_bytes: int = DEFAULT_MAX_LOG_FILE_BYTES,
    ) -> None:
        self.experiment_id = experiment_id
        self.mailbox_task_id = mailbox_task_id
        self._dir = Path(log_dir)
        self._path = self._dir / f"{experiment_id}.jsonl"
        self._max_bytes = max_bytes
        self._limit_notice_written = False

    @property
    def path(self) -> Path:
        return self._path

    def bind_mailbox_task_id(self, mailbox_task_id: str | None) -> None:
        """Attach a mailbox task id discovered after this logger was created."""
        if mailbox_task_id:
            self.mailbox_task_id = mailbox_task_id

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
                "experiment_logger: failed to write log entry (experiment=%s phase=%s event=%s)",
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
        if duration_ms is not None:
            entry["duration_ms"] = int(duration_ms)
        if command is not None:
            entry["command"] = redaction.redact(str(command))[:MAX_COMMAND_CHARS]
        if cwd is not None:
            entry["cwd"] = str(cwd)
        if exit_code is not None:
            entry["exit_code"] = int(exit_code)
        if stdout is not None:
            bounded_stdout, out_truncated = _bounded_text(stdout, MAX_STDOUT_CHARS)
            entry["stdout"] = bounded_stdout
            truncated = truncated or out_truncated
        if stderr is not None:
            bounded_stderr, err_truncated = _bounded_text(stderr, MAX_STDERR_CHARS)
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
        self._mirror_to_journald(level, phase, event, message)

    def _append(self, entry: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path_key = str(self._path)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _lock_for(path_key):
            try:
                current_size = self._path.stat().st_size
            except FileNotFoundError:
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


def _format_stacktrace(exc: BaseException) -> str:
    """Bounded, redacted stack trace text (last frames only)."""
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    text = "".join(lines)
    return redaction.redact(text)


# ---------------------------------------------------------------------------
# Read path (used by the API)
# ---------------------------------------------------------------------------


def read_log_entries(
    log_path: str | Path,
    *,
    level: str | None = None,
    phase: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read one experiment's JSONL log, defensively re-sanitized, chronological.

    Malformed lines (should not normally occur) are skipped rather than
    raising. Every string field is redacted again on the way out: a
    defense-in-depth measure independent of the write-path redaction.
    """
    path = Path(log_path)
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            entries.append(_resanitize_entry(entry))

    if level:
        level_upper = level.upper()
        entries = [e for e in entries if e.get("level") == level_upper]
    if phase:
        entries = [e for e in entries if e.get("phase") == phase]

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
)


def _resanitize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(entry)
    for field in _STRING_FIELDS_TO_REDACT:
        value = cleaned.get(field)
        if isinstance(value, str):
            cleaned[field] = redaction.redact(value)
    return cleaned
