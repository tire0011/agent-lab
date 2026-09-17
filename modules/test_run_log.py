"""Dedicated per-test log and test heartbeat file for verification runs (#209).

The global Nexus feed (``modules.central_logging``) is what an operator reads
to follow an experiment: intake, planning, implementation, verification,
adoption. A suite of ~1900 tests would add ~3800 entries per verification run
to it, which does not make the run more observable - it makes everything else
unreadable. So per-test detail gets its own durable file, in its own
subdirectory:

``<log_dir>/test-runs/test-<run_id>.jsonl``
    one JSON object per test start and per test completion.

``<log_dir>/test-runs/test-<run_id>.heartbeat.json``
    the current test heartbeat, rewritten atomically on every watchdog tick,
    so a second process (a diagnostic command, a human with ``cat``) can see
    what the test run is doing without reading the whole log.

The subdirectory is what keeps the two apart: the unified feed globs
``*.jsonl`` in the log directory itself and does not recurse, so these files
are never merged into it. What *does* reach the global feed is the periodic
progress summary the runner emits through the caller's own logger - one entry
per watchdog tick, not per test.

Like ``central_logging``, nothing in here may break a test run: every public
method swallows its own I/O errors and reports them through stdlib logging.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from modules.central_logging import resolve_log_dir

logger = logging.getLogger("nexus.test_run_log")

#: Subdirectory (under the experiment log directory) holding test-run files.
TEST_LOG_SUBDIR = "test-runs"
TEST_LOG_PREFIX = "test-"
TEST_LOG_DIR_ENV_VAR = "NEXUS_TEST_LOG_DIR"

# A verification run writes two entries per test; ~1900 tests is well under a
# megabyte. The ceiling only exists so a pathological run (a test looping on
# output) cannot fill the disk.
DEFAULT_MAX_TEST_LOG_BYTES = 32 * 1024 * 1024

# How many past test-run logs are kept. Enough to compare a stalled run with
# the last few healthy ones, bounded so the directory cannot grow forever.
DEFAULT_KEEP_TEST_LOGS = 50

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def resolve_test_log_dir(env: dict[str, str] | None = None) -> str:
    """Where per-test logs are written.

    ``NEXUS_TEST_LOG_DIR`` overrides it outright; otherwise it is the
    ``test-runs`` subdirectory of whatever ``resolve_log_dir`` resolves to, so
    a test run's detail always lands next to - but never inside - the
    experiment logs of the same deployment.
    """
    source = os.environ if env is None else env
    explicit = (source.get(TEST_LOG_DIR_ENV_VAR) or "").strip()
    if explicit:
        return explicit
    return str(Path(resolve_log_dir(env)) / TEST_LOG_SUBDIR)


def safe_run_id(value: object) -> str:
    """A run id that is safe to use as a file name, never empty."""
    text = str(value or "").strip()
    if _SAFE_ID_RE.fullmatch(text):
        return text
    # Dots are legal in a run id but must not be able to start one: a
    # name like "../../etc/passwd" has to become a plain file name here.
    cleaned = _UNSAFE_CHARS_RE.sub("-", text).strip("-.")[:128]
    return cleaned or "unknown"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VerificationTestLog:
    """Durable per-test log plus heartbeat file for one verification run."""

    def __init__(
        self,
        run_id: str,
        *,
        log_dir: str | Path | None = None,
        max_bytes: int = DEFAULT_MAX_TEST_LOG_BYTES,
    ) -> None:
        self.run_id = safe_run_id(run_id)
        self._dir = Path(log_dir) if log_dir is not None else Path(resolve_test_log_dir())
        self._path = self._dir / f"{TEST_LOG_PREFIX}{self.run_id}.jsonl"
        self._heartbeat_path = self._dir / f"{TEST_LOG_PREFIX}{self.run_id}.heartbeat.json"
        self._max_bytes = max(0, max_bytes)
        self._lock = threading.Lock()
        self._limit_notice_written = False
        self._disabled = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def heartbeat_path(self) -> Path:
        return self._heartbeat_path

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------

    def log_run_event(self, event: str, message: str, **extra: Any) -> None:
        """A run-level event (started, finished, stalled, safety cap, ...)."""
        self._append({"kind": "run", "event": event, "message": message, **extra})

    def log_test_start(self, test_id: str, *, at: str | None = None) -> None:
        self._append({"kind": "test", "event": "test_started", "test": test_id, "at": at})

    def log_test_end(
        self,
        test_id: str,
        *,
        result: str,
        started_at: str | None = None,
        at: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self._append({
            "kind": "test",
            "event": "test_finished",
            "test": test_id,
            "result": result,
            "started_at": started_at,
            "at": at,
            "duration_ms": duration_ms,
        })

    def write_heartbeat(self, snapshot: dict[str, Any]) -> None:
        """Replace the heartbeat file atomically.

        Atomically, because a reader must never see half a heartbeat and
        conclude the run is broken: the temporary file is written and replaced
        in one ``os.replace``.
        """
        if self._disabled:
            return
        payload = dict(snapshot)
        payload["run_id"] = self.run_id
        payload["written_at"] = _utcnow_iso()
        temp_path = self._heartbeat_path.with_suffix(f".{os.getpid()}.tmp")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._heartbeat_path)
        except OSError:
            logger.exception("test_run_log: failed to write heartbeat for %s", self.run_id)
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort cleanup
                pass

    def read_heartbeat(self) -> dict[str, Any] | None:
        try:
            with self._heartbeat_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def read_entries(self) -> list[dict[str, Any]]:
        """Every well-formed entry in this run's test log, in order."""
        return list(read_test_log_entries(self._path))

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _append(self, entry: dict[str, Any]) -> None:
        if self._disabled:
            return
        entry = {"ts": _utcnow_iso(), "run_id": self.run_id, **entry}
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                self._dir.mkdir(parents=True, exist_ok=True)
                if self._max_bytes and self._path.exists():
                    if self._path.stat().st_size >= self._max_bytes:
                        if self._limit_notice_written:
                            return
                        self._limit_notice_written = True
                        line = json.dumps({
                            "ts": _utcnow_iso(),
                            "run_id": self.run_id,
                            "kind": "run",
                            "event": "test_log_limit_reached",
                            "message": (
                                f"test log reached its {self._max_bytes} byte ceiling; "
                                "further per-test entries are dropped"
                            ),
                        }, ensure_ascii=False) + "\n"
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError:
            # Instrumentation must never be the reason a test run fails.
            logger.exception("test_run_log: failed to append to %s", self._path)


def read_test_log_entries(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield well-formed JSON objects from a test log, skipping bad lines."""
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


def prune_test_logs(
    log_dir: str | Path | None = None,
    *,
    keep: int = DEFAULT_KEEP_TEST_LOGS,
) -> int:
    """Delete all but the *keep* newest test runs. Returns how many went.

    Both the ``.jsonl`` and its ``.heartbeat.json`` are removed together, so a
    heartbeat can never outlive the log it summarizes.
    """
    directory = Path(log_dir) if log_dir is not None else Path(resolve_test_log_dir())
    try:
        logs = sorted(
            directory.glob(f"{TEST_LOG_PREFIX}*.jsonl"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0
    removed = 0
    for stale in logs[max(0, keep):]:
        for path in (stale, stale.with_suffix(".heartbeat.json")):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort cleanup
                continue
        removed += 1
    return removed


def global_heartbeat_bridge(
    elog: Any,
    phase: str,
    *,
    event: str = "test_progress",
) -> Any:
    """Build an ``on_heartbeat`` callback that publishes to the global log.

    This is the propagation the global Nexus heartbeat needs. While local
    verification runs, the experiment's own log keeps receiving one entry per
    watchdog tick, and that entry is what
    :func:`modules.nexus_diagnostics.assess_progress` reads as "this work is
    alive and moving" - the same contract every other long-running Nexus
    subsystem uses. Only the summary crosses over: the per-test entries stay
    in the dedicated test log, so the global feed gains one line a minute
    rather than two per test.
    """

    def _publish(snapshot: dict[str, Any]) -> None:
        completed = snapshot.get("tests_completed")
        total = snapshot.get("tests_total")
        counted = f"{completed}/{total}" if total else str(completed)
        current = snapshot.get("current_test") or "<between tests>"
        elog.info(
            phase,
            event,
            f"Verification tests progressing: {counted} completed, current test {current}",
            **snapshot,
        )

    return _publish
