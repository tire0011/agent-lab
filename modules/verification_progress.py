"""Per-test progress, logging and heartbeat for local verification runs (#209).

A fixed wall-clock timeout cannot tell a healthy-but-slow suite from a stuck
one: experiment ``df97d67b`` was reported ``timed_out`` with no exit code
while the suite may still have been making progress. The fix is to watch
*progress* instead of elapsed time, which means the run has to produce a
progress signal in the first place.

``unittest`` in verbose mode flushes ``<test id> ... `` before each test runs
and appends the outcome when it finishes, so reading the stream as it arrives
yields a start and an end event per test. This module turns that stream into

* a dedicated per-test JSONL log, written to a ``tests/`` subdirectory so the
  unified Nexus feed (which globs ``*.jsonl`` one level up) is not flooded
  with thousands of entries;
* a small heartbeat snapshot, rewritten on every event, that says what is
  running right now and when progress was last observed.

Parsing is deliberately forgiving. Output a test prints itself lands in the
same stream, and a line this module cannot classify is *not* an error: it
still counts as output activity, which is itself evidence of progress. The
watchdog therefore keeps working even when the format changes underneath it.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "TESTS_LOG_DIRNAME",
    "ProgressEvent",
    "VerificationProgressSnapshot",
    "VerificationProgressTracker",
    "UnittestProgressParser",
    "read_heartbeat",
    "read_process_stats",
    "heartbeat_path",
    "progress_log_path",
]

# Per-test artefacts live one level below the experiment logs on purpose:
# ``central_logging.scan_log_entries`` globs ``<log_dir>/*.jsonl`` without
# recursing, so a subdirectory is what keeps the unified feed clean.
TESTS_LOG_DIRNAME = "tests"

RESULT_PASSED = "passed"
RESULT_FAILED = "failed"
RESULT_ERROR = "error"
RESULT_SKIPPED = "skipped"
RESULT_EXPECTED_FAILURE = "expected_failure"
RESULT_UNEXPECTED_SUCCESS = "unexpected_success"

# unittest's verbose outcome tokens, longest first so "unexpected success"
# is never matched as "ok" by a shorter prefix.
_RESULT_TOKENS: tuple[tuple[str, str], ...] = (
    ("unexpected success", RESULT_UNEXPECTED_SUCCESS),
    ("expected failure", RESULT_EXPECTED_FAILURE),
    ("skipped", RESULT_SKIPPED),
    ("ERROR", RESULT_ERROR),
    ("FAIL", RESULT_FAILED),
    ("ok", RESULT_PASSED),
)

_START_MARKER = " ... "
# The dotted path unittest prints in parentheses, e.g.
# "test_foo (tests.test_x.SomeTest.test_foo)".
_TEST_ID_RE = re.compile(r"\(([A-Za-z0-9_.]+)\)")
_RAN_TESTS_RE = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)

_MAX_TEST_ID_CHARS = 300
_MAX_DETAIL_CHARS = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One observed transition: a test started, or a test finished."""

    kind: str  # "start" | "end"
    test_id: str
    result: str | None = None
    detail: str | None = None


class UnittestProgressParser:
    """Incrementally turn verbose unittest output into start/end events.

    The caller feeds whatever arrived from the pipe, in whatever chunk sizes
    the OS produced; only complete markers are consumed, so a test id split
    across two reads is still parsed exactly once.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._current: str | None = None
        self.total_from_summary: int | None = None

    def feed(self, chunk: str) -> list[ProgressEvent]:
        if not chunk:
            return []
        self._buffer += chunk
        events: list[ProgressEvent] = []
        while True:
            consumed, event = self._consume_one()
            if event is not None:
                events.append(event)
            if not consumed:
                break
        self._note_summary()
        self._trim()
        return events

    def _consume_one(self) -> tuple[bool, ProgressEvent | None]:
        """Consume one marker; returns (consumed anything, event if any).

        Dropping a noise line consumes buffer without producing an event, and
        the caller must keep draining - otherwise a test's own ``print`` would
        defer that test's outcome until the *next* chunk arrived, which for a
        run that then stalls means the outcome is never recorded at all.
        """
        if self._current is None:
            index = self._buffer.find(_START_MARKER)
            if index < 0:
                return False, None
            description = self._buffer[:index]
            self._buffer = self._buffer[index + len(_START_MARKER):]
            self._current = _extract_test_id(description)
            return True, ProgressEvent("start", self._current)

        newline = self._buffer.find("\n")
        if newline < 0:
            return False, None
        line = self._buffer[:newline].strip()
        result, detail = _classify_result(line)
        self._buffer = self._buffer[newline + 1:]
        if result is None:
            # Output the test itself printed. Drop the line and keep waiting
            # for the real outcome rather than inventing one.
            return True, None
        finished, self._current = self._current, None
        return True, ProgressEvent("end", finished, result=result, detail=detail)

    def _note_summary(self) -> None:
        match = _RAN_TESTS_RE.search(self._buffer)
        if match:
            self.total_from_summary = int(match.group(1))

    def _trim(self) -> None:
        # Between tests the buffer only ever holds a partial marker or the
        # trailing summary; an unbounded buffer would otherwise grow with
        # every line a test prints.
        if len(self._buffer) > 64_000:
            self._buffer = self._buffer[-8_000:]


def _extract_test_id(description: str) -> str:
    match = _TEST_ID_RE.search(description)
    if match:
        return match.group(1)[:_MAX_TEST_ID_CHARS]
    for line in reversed(description.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:_MAX_TEST_ID_CHARS]
    return "<unknown test>"


def _classify_result(line: str) -> tuple[str | None, str | None]:
    for token, result in _RESULT_TOKENS:
        if line == token:
            return result, None
        if line.startswith(token) and line[len(token):len(token) + 1] in {" ", ":"}:
            return result, line[len(token):].strip()[:_MAX_DETAIL_CHARS] or None
    return None, None


@dataclass(slots=True)
class VerificationProgressSnapshot:
    """What the watchdog and the global heartbeat read to judge liveness."""

    started_at: str
    last_progress_at: str
    current_test: str | None = None
    last_test_started_at: str | None = None
    last_test_completed_at: str | None = None
    tests_started: int = 0
    tests_completed: int = 0
    tests_total: int | None = None
    results: dict[str, int] = field(default_factory=dict)
    pid: int | None = None
    process_alive: bool = True
    output_bytes: int = 0
    cpu_seconds: float | None = None
    cpu_seconds_delta: float | None = None
    rss_kb: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def age_seconds(self, *, now: datetime | None = None) -> float:
        parsed = _parse_iso(self.last_progress_at)
        if parsed is None:
            return 0.0
        return max(0.0, ((now or _utcnow()) - parsed).total_seconds())

    def describe(self, *, now: datetime | None = None) -> str:
        """The stall diagnostic from the issue, as one quotable block."""
        completed = str(self.tests_completed)
        if self.tests_total:
            completed = f"{self.tests_completed} / {self.tests_total}"
        lines = [
            f"current_test: {self.current_test or 'unknown'}",
            f"last_progress: {_humanize(self.age_seconds(now=now))} ago",
            f"process_alive: {str(bool(self.process_alive)).lower()}",
            f"tests_completed: {completed}",
        ]
        # What separates "deadlocked" from "slow but working": a process
        # burning CPU while making no test progress is stuck in a loop; one
        # using none is blocked on something.
        if self.cpu_seconds is not None:
            recent = (
                f", +{self.cpu_seconds_delta:.1f}s since last check"
                if self.cpu_seconds_delta is not None
                else ""
            )
            lines.append(f"cpu_time: {self.cpu_seconds:.1f}s{recent}")
        if self.rss_kb is not None:
            lines.append(f"memory_rss: {self.rss_kb // 1024} MiB")
        return "\n".join(lines)


def _humanize(seconds: float) -> str:
    total = int(max(0.0, seconds))
    minutes, remainder = divmod(total, 60)
    if minutes:
        return f"{minutes}m {remainder:02d}s"
    return f"{remainder}s"


def _parse_iso(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_process_stats(pid: int | None) -> tuple[float, int | None] | None:
    """(CPU seconds, RSS KiB) for *pid* from procfs, or ``None``.

    Linux-only by design: the VM this runs on is Ubuntu, and on any other
    platform the stall diagnostic simply omits these two lines rather than
    taking a dependency on a process-inspection library for them.
    """
    if not pid:
        return None
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").rsplit(") ", 1)
        values = fields[-1].split()
        ticks = float(values[11]) + float(values[12])  # utime + stime
        cpu_seconds = ticks / (os.sysconf("SC_CLK_TCK") or 100)
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    rss_kb: int | None = None
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        rss_kb = None
    return cpu_seconds, rss_kb


def progress_log_path(log_dir: str | Path, key: str) -> Path:
    return Path(log_dir) / TESTS_LOG_DIRNAME / f"{key}.jsonl"


def heartbeat_path(log_dir: str | Path, key: str) -> Path:
    return Path(log_dir) / TESTS_LOG_DIRNAME / f"{key}.heartbeat.json"


def read_heartbeat(log_dir: str | Path, key: str) -> dict[str, Any] | None:
    """Last published test heartbeat, or ``None`` when there is none.

    Readers are observers (the global heartbeat, diagnostics): a missing,
    half-written or unparsable file means "no progress signal", never an
    error that should propagate into their own answer.
    """
    path = heartbeat_path(log_dir, key)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


class VerificationProgressTracker:
    """Owns the live snapshot, the per-test log and the heartbeat file.

    Instrumentation must never be the reason a verification run fails, so
    every write is best-effort: a full disk costs the operator visibility,
    not the test result.
    """

    def __init__(
        self,
        *,
        log_dir: str | Path | None = None,
        key: str | None = None,
        pid: int | None = None,
        tests_total: int | None = None,
        now: Any = None,
    ) -> None:
        self._now = now or _utcnow
        self._lock = threading.Lock()
        self._log_path = progress_log_path(log_dir, key) if log_dir and key else None
        self._heartbeat_path = heartbeat_path(log_dir, key) if log_dir and key else None
        self._key = key
        self._open_tests: dict[str, datetime] = {}
        started = self._now()
        self.snapshot = VerificationProgressSnapshot(
            started_at=_iso(started),
            last_progress_at=_iso(started),
            tests_total=tests_total,
            pid=pid,
        )
        self._prepare_directory()
        self.publish()

    # -- observation ---------------------------------------------------

    def note_output(self, text: str) -> None:
        """Raw output arrived: progress even when nothing parsed out of it."""
        if not text:
            return
        with self._lock:
            self.snapshot.output_bytes += len(text)
            self.snapshot.last_progress_at = _iso(self._now())

    def note_events(self, events: list[ProgressEvent]) -> None:
        for event in events:
            self._note_event(event)
        if events:
            self.publish()

    def note_total(self, total: int | None) -> None:
        if total and total > 0:
            with self._lock:
                self.snapshot.tests_total = total

    def note_process_alive(self, alive: bool) -> None:
        with self._lock:
            self.snapshot.process_alive = bool(alive)

    def note_process_stats(self, pid: int | None) -> None:
        """Sample CPU/RSS for the stall diagnostic.

        Never treated as progress: a spinning deadlock burns CPU forever and
        would keep resetting the stall window if it counted. It is recorded
        only so the diagnostic can say which kind of stuck this is.
        """
        stats = read_process_stats(pid)
        if stats is None:
            return
        cpu_seconds, rss_kb = stats
        with self._lock:
            previous = self.snapshot.cpu_seconds
            self.snapshot.cpu_seconds = cpu_seconds
            self.snapshot.cpu_seconds_delta = (
                max(0.0, cpu_seconds - previous) if previous is not None else None
            )
            self.snapshot.rss_kb = rss_kb

    def _note_event(self, event: ProgressEvent) -> None:
        moment = self._now()
        stamp = _iso(moment)
        record: dict[str, Any] | None = None
        with self._lock:
            self.snapshot.last_progress_at = stamp
            if event.kind == "start":
                self._open_tests[event.test_id] = moment
                self.snapshot.current_test = event.test_id
                self.snapshot.last_test_started_at = stamp
                self.snapshot.tests_started += 1
                record = {
                    "event": "test_started",
                    "test_id": event.test_id,
                    "started_at": stamp,
                }
            else:
                started = self._open_tests.pop(event.test_id, None)
                duration_ms = (
                    int((moment - started).total_seconds() * 1000)
                    if started is not None
                    else None
                )
                result = event.result or RESULT_PASSED
                self.snapshot.tests_completed += 1
                self.snapshot.results[result] = self.snapshot.results.get(result, 0) + 1
                self.snapshot.last_test_completed_at = stamp
                if self.snapshot.current_test == event.test_id:
                    self.snapshot.current_test = None
                record = {
                    "event": "test_completed",
                    "test_id": event.test_id,
                    "started_at": _iso(started) if started is not None else None,
                    "ended_at": stamp,
                    "result": result,
                    "duration_ms": duration_ms,
                    "detail": event.detail,
                }
        if record is not None:
            self._append(record)

    # -- publication ---------------------------------------------------

    def publish(self) -> None:
        if self._heartbeat_path is None:
            return
        with self._lock:
            payload = self.snapshot.to_dict()
        payload["key"] = self._key
        payload["published_at"] = _iso(self._now())
        try:
            # Written whole and renamed so a reader never sees half a file.
            temporary = self._heartbeat_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self._heartbeat_path)
        except (OSError, ValueError, TypeError):
            pass

    def clear_heartbeat(self) -> None:
        """Drop the heartbeat once the run is over so nothing reads it as live."""
        if self._heartbeat_path is None:
            return
        try:
            self._heartbeat_path.unlink()
        except OSError:
            pass

    def _prepare_directory(self) -> None:
        for path in (self._log_path, self._heartbeat_path):
            if path is None:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def _append(self, record: dict[str, Any]) -> None:
        if self._log_path is None:
            return
        record.setdefault("key", self._key)
        try:
            with open(self._log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except (OSError, ValueError, TypeError):
            pass
