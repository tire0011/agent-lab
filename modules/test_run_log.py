"""Dedicated per-test log and test heartbeat file for verification runs (#209).

``<log_dir>/test-runs/test-<run_id>.jsonl`` holds one entry per test start and
completion; ``test-<run_id>.heartbeat.json`` is the current heartbeat,
replaced atomically each watchdog tick. The subdirectory keeps them out of
the global feed, which only gets one progress summary per tick. Nothing here
may break a test run: I/O errors are logged, never raised.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    """Where per-test logs go: ``NEXUS_TEST_LOG_DIR``, else ``test-runs`` under the log dir."""
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
        """Replace the heartbeat file atomically, so a reader never sees half of it."""
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
    """An ``on_heartbeat`` callback that logs one summary per tick to the global log.

    That entry is what :func:`modules.nexus_diagnostics.assess_progress` reads
    as "alive and moving"; per-test entries stay in the test log.
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
