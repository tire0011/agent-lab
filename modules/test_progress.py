"""Per-test progress tracking for verification test runs (#209).

A fixed wall-clock timeout cannot tell a healthy-but-slow suite from a single
stuck test or a dead process: all three look identical from outside, which is
why experiment ``df97d67bc08e45b4bc5c573db0c84d85`` was reported as
``timed_out`` with no exit code while the suite may well have still been
making progress. This module supplies the missing signal - *what the test
process is actually doing right now* - so the watchdog in
:mod:`modules.verification_runner` can decide on progress instead of on total
runtime.

Two pieces, both pure (no I/O, no threads of their own beyond a lock):

:class:`UnittestStreamParser`
    An incremental parser for ``python -m unittest ... -v`` output. It is fed
    raw chunks as they arrive and emits :class:`ProgressEvent` objects for every
    test start and completion.

:class:`ProgressTracker`
    Thread-safe accumulation of those events into the *test heartbeat*: the
    current test, counts, and when progress was last observed.

Why the parser reads chunks rather than lines
---------------------------------------------
``unittest``'s verbose stream writes a test's description and the separator
``" ... "`` at *start* (flushed, deliberately without a newline) and the
result token plus the newline at *end*. Reading whole lines would therefore
only ever see a test that has already finished, which is exactly the test a
stall diagnostic does not care about. Reading chunks lets the currently
*running* test be named while it runs.

Parsing text is best effort by nature: a test that writes to stderr itself
interleaves with the runner's stream. The design makes that survivable rather
than pretending it cannot happen - an unrecognized line is treated as plain
activity and the current test stays current, so mis-parsing can never invent
a completion or lose the run.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

RESULT_PASSED = "passed"
RESULT_FAILED = "failed"
RESULT_ERROR = "error"
RESULT_SKIPPED = "skipped"
RESULT_EXPECTED_FAILURE = "expected_failure"
RESULT_UNEXPECTED_SUCCESS = "unexpected_success"

#: Results that mean the test did not pass, in the sense the verification
#: pipeline cares about. Used to decide which verbose lines are worth keeping
#: in the captured output (see :attr:`UnittestStreamParser.filtered_output`).
FAILING_RESULTS = frozenset({RESULT_FAILED, RESULT_ERROR, RESULT_UNEXPECTED_SUCCESS})

EVENT_START = "start"
EVENT_END = "end"
EVENT_OUTPUT = "output"

# Written by ``TextTestResult.startTest`` between the description and the
# result token. It is the only in-band marker unittest gives us.
_START_MARKER = " ... "

# ``str(test)`` is ``"<method> (<target>)"`` - where *target* is the dotted
# class path on Python <= 3.10 and the full dotted test path from 3.11 on.
_TEST_ID_RE = re.compile(r"^(?P<method>[A-Za-z_]\w*) \((?P<target>[\w.]+)\)$")

# ``unittest``'s final summary line, the only place a total is stated.
_RAN_TESTS_RE = re.compile(r"^Ran (?P<count>\d+) tests? in ", re.MULTILINE)

# Header of one entry in unittest's closing failure report. The description may
# carry a subtest suffix, and a dotted "method" is how a failed import of a
# nested test module is named.
_FAILURE_HEADER_RE = re.compile(
    r"^(?:FAIL|ERROR|UNEXPECTED SUCCESS): (?P<method>[\w.]+) \((?P<target>[\w.]+)\)",
    re.MULTILINE,
)

# How much trailing text is held back while waiting for a start marker. A
# description is short; this only has to exceed ``len(_START_MARKER)`` so a
# marker can never be split across a drain boundary.
_IDLE_RETAIN_CHARS = 512


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_result(token: str) -> str | None:
    """Map one verbose result token to a stable result name.

    Returns ``None`` for anything unrecognized, which the parser reads as
    "this line is not a test result" rather than as an unknown outcome.
    """
    token = token.strip()
    if token == "ok":
        return RESULT_PASSED
    if token.startswith("FAIL"):
        return RESULT_FAILED
    if token.startswith("ERROR"):
        return RESULT_ERROR
    if token.startswith("skipped"):
        return RESULT_SKIPPED
    if token.startswith("expected failure"):
        return RESULT_EXPECTED_FAILURE
    if token.startswith("unexpected success"):
        return RESULT_UNEXPECTED_SUCCESS
    return None


def parse_total_tests(output: str | None) -> int | None:
    """Total from unittest's ``Ran N tests in ...`` summary, if present."""
    if not output:
        return None
    matches = _RAN_TESTS_RE.findall(output)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
        return None


def parse_failed_test_ids(output: str | None) -> set[str]:
    """Canonical IDs from unittest's closing failure report.

    Stricter than the live stream in both directions: subtest failures appear
    only here, and a passing test that logs a line starting with ``ERROR``
    appears only there.
    """
    failed: set[str] = set()
    for match in _FAILURE_HEADER_RE.finditer(output or ""):
        method, target = match.group("method"), match.group("target")
        failed.add(target if "." in method else canonical_test_id(f"{method} ({target})"))
    return failed


def canonical_test_id(description: str) -> str:
    """Canonical dotted test id from one verbose description block."""
    first_line = description.split("\n", 1)[0].strip()
    match = _TEST_ID_RE.match(first_line)
    if match is None:
        return first_line[:200] or "<unknown test>"
    method, target = match.group("method"), match.group("target")
    if target == method or target.endswith(f".{method}"):
        return target
    return f"{target}.{method}"


@dataclass(slots=True)
class ProgressEvent:
    """One observed transition in the test process."""

    kind: str
    test_id: str
    at: str
    result: str | None = None
    duration_ms: int | None = None
    started_at: str | None = None


class UnittestStreamParser:
    """Incremental parser for a verbose ``unittest`` output stream.

    Besides the events, the parser rebuilds a *filtered* copy of the stream
    with the per-test ``"<description> ... ok"`` lines removed. That keeps the
    output the verification pipeline persists (and truncates head-first, and
    tails for the failure summary) the same size it was under ``-q``, so
    switching the command to ``-v`` buys progress visibility without pushing
    the failure summary past the output cap. Lines for failing tests are kept.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._pending_description = ""
        self._current_test: str | None = None
        self._current_started_at: str | None = None
        self._current_started_monotonic: float | None = None
        self._kept: list[str] = []

    @property
    def current_test(self) -> str | None:
        return self._current_test

    @property
    def filtered_output(self) -> str:
        return "".join(self._kept)

    def feed(self, chunk: str) -> list[ProgressEvent]:
        """Consume a chunk of raw output and return the events it completes."""
        if not chunk:
            return []
        events: list[ProgressEvent] = []
        self._buffer += chunk
        while True:
            if self._current_test is None:
                index = self._buffer.find(_START_MARKER)
                if index < 0:
                    self._drain_idle()
                    break
                head, description = _split_description(self._buffer[:index])
                if head:
                    self._kept.append(head)
                self._pending_description = description + _START_MARKER
                self._buffer = self._buffer[index + len(_START_MARKER):]
                self._current_test = canonical_test_id(description)
                self._current_started_at = utcnow_iso()
                self._current_started_monotonic = time.monotonic()
                events.append(
                    ProgressEvent(EVENT_START, self._current_test, self._current_started_at)
                )
                continue

            newline = self._buffer.find("\n")
            if newline < 0:
                break
            line = self._buffer[:newline]
            self._buffer = self._buffer[newline + 1:]
            result = classify_result(line)
            if result is None:
                # Output the running test produced itself. It is progress, and
                # it must be kept, but it does not end the test.
                self._kept.append(line + "\n")
                events.append(ProgressEvent(EVENT_OUTPUT, self._current_test, utcnow_iso()))
                continue
            events.append(self._finish_current(result, line))
        return events

    def flush(self) -> list[ProgressEvent]:
        """Close the stream: keep whatever is left, without inventing a result."""
        remainder = self._pending_description + self._buffer
        if remainder:
            self._kept.append(remainder)
        self._pending_description = ""
        self._buffer = ""
        return []

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _finish_current(self, result: str, line: str) -> ProgressEvent:
        assert self._current_test is not None  # guarded by the caller
        duration_ms: int | None = None
        if self._current_started_monotonic is not None:
            duration_ms = int((time.monotonic() - self._current_started_monotonic) * 1000)
        if result in FAILING_RESULTS:
            self._kept.append(self._pending_description + line + "\n")
        event = ProgressEvent(
            EVENT_END,
            self._current_test,
            utcnow_iso(),
            result=result,
            duration_ms=duration_ms,
            started_at=self._current_started_at,
        )
        self._pending_description = ""
        self._current_test = None
        self._current_started_at = None
        self._current_started_monotonic = None
        return event

    def _drain_idle(self) -> None:
        """Move text that cannot belong to a pending description into output.

        Safe because the whole buffer has just been searched for the marker
        without a hit, so only the retained tail can still grow into one.
        """
        if len(self._buffer) > _IDLE_RETAIN_CHARS:
            self._kept.append(self._buffer[:-_IDLE_RETAIN_CHARS])
            self._buffer = self._buffer[-_IDLE_RETAIN_CHARS:]


def _split_description(text: str) -> tuple[str, str]:
    """Split text preceding a start marker into (earlier output, description).

    ``TextTestResult.getDescription`` returns ``str(test)`` alone, or
    ``str(test)`` and the docstring's first line joined by a newline, so the
    description is the last line - plus the line before it when that line is
    itself a well-formed ``str(test)``.
    """
    lines = text.split("\n")
    take = 1
    if len(lines) >= 2 and _TEST_ID_RE.match(lines[-2].strip()):
        take = 2
    head_lines = lines[: len(lines) - take]
    head = "\n".join(head_lines)
    if head_lines:
        head += "\n"
    return head, "\n".join(lines[len(lines) - take:])


class ProgressTracker:
    """The test heartbeat: what is running, how far along, and since when.

    Progress is judged on *test* events (a start or a completion) whenever any
    have been seen, and only falls back to raw output activity before the
    first one - so a process that keeps printing while wedged between tests is
    still recognized as stalled, while a run whose output this parser cannot
    read at all is not declared dead for that reason alone.
    """

    def __init__(self, *, expected_total: int | None = None) -> None:
        self.expected_total = expected_total if (expected_total or 0) > 0 else None
        self.started_at = utcnow_iso()
        self._started_monotonic = time.monotonic()
        self._lock = threading.Lock()
        self._current_test: str | None = None
        self._current_test_started_at: str | None = None
        self._last_completed_test: str | None = None
        self._tests_started = 0
        self._tests_completed = 0
        self._results: dict[str, int] = {}
        self._total_tests: int | None = None
        self._last_test_event_at: str | None = None
        self._last_test_event_monotonic: float | None = None
        self._last_activity_monotonic = self._started_monotonic
        self._last_activity_at = self.started_at

    def note_activity(self) -> None:
        """Record that the process produced output, whatever it was."""
        with self._lock:
            self._last_activity_monotonic = time.monotonic()
            self._last_activity_at = utcnow_iso()

    def record(self, event: ProgressEvent) -> None:
        with self._lock:
            self._last_activity_monotonic = time.monotonic()
            self._last_activity_at = event.at
            if event.kind == EVENT_OUTPUT:
                return
            self._last_test_event_monotonic = time.monotonic()
            self._last_test_event_at = event.at
            if event.kind == EVENT_START:
                self._tests_started += 1
                self._current_test = event.test_id
                self._current_test_started_at = event.at
            elif event.kind == EVENT_END:
                self._tests_completed += 1
                self._last_completed_test = event.test_id
                self._current_test = None
                self._current_test_started_at = None
                if event.result:
                    self._results[event.result] = self._results.get(event.result, 0) + 1

    def record_all(self, events: Iterable[ProgressEvent]) -> None:
        for event in events:
            self.record(event)

    def set_total_tests(self, total: int | None) -> None:
        if total is not None and total >= 0:
            with self._lock:
                self._total_tests = total

    def seconds_since_progress(self) -> float:
        """Seconds since the newest signal the stall rule trusts."""
        with self._lock:
            reference = (
                self._last_test_event_monotonic
                if self._last_test_event_monotonic is not None
                else self._last_activity_monotonic
            )
        return max(0.0, time.monotonic() - reference)

    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started_monotonic)

    def snapshot(self, **extra: object) -> dict[str, object]:
        """A JSON-serializable view of the heartbeat, plus caller extras."""
        with self._lock:
            total = self._total_tests if self._total_tests is not None else self.expected_total
            snapshot: dict[str, object] = {
                "started_at": self.started_at,
                "current_test": self._current_test,
                "current_test_started_at": self._current_test_started_at,
                "last_completed_test": self._last_completed_test,
                "tests_started": self._tests_started,
                "tests_completed": self._tests_completed,
                "tests_total": total,
                "results": dict(self._results),
                "last_progress_at": self._last_test_event_at or self._last_activity_at,
                "last_activity_at": self._last_activity_at,
            }
        snapshot["last_progress_age_seconds"] = round(self.seconds_since_progress(), 3)
        snapshot["elapsed_seconds"] = round(self.elapsed_seconds(), 3)
        snapshot.update(extra)
        return snapshot


def format_duration(seconds: float) -> str:
    """``7m 12s`` - the shape the issue's example diagnostic uses."""
    total = int(max(0.0, seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_stall_diagnostics(snapshot: dict[str, object], *, title: str = "Test run stalled") -> str:
    """Render the stall report an operator or failure analysis will read."""
    completed = snapshot.get("tests_completed")
    total = snapshot.get("tests_total")
    lines = [
        title,
        f"current_test: {snapshot.get('current_test') or '<between tests>'}",
        f"last_progress: {format_duration(float(snapshot.get('last_progress_age_seconds') or 0.0))} ago",
        f"process_alive: {str(bool(snapshot.get('process_alive'))).lower()}",
        f"tests_completed: {completed}" + (f" / {total}" if total else ""),
        f"elapsed: {format_duration(float(snapshot.get('elapsed_seconds') or 0.0))}",
    ]
    if snapshot.get("pid") is not None:
        lines.append(f"pid: {snapshot['pid']}")
    if snapshot.get("cpu_seconds") is not None:
        lines.append(f"cpu_seconds: {snapshot['cpu_seconds']}")
    if snapshot.get("rss_bytes") is not None:
        lines.append(f"rss_bytes: {snapshot['rss_bytes']}")
    if snapshot.get("process_state"):
        lines.append(f"process_state: {snapshot['process_state']}")
    return "\n".join(lines)
