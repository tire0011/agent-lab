"""Shared building blocks for the external coding-agent workers (Codex, Claude),
so both are interchangeable for their callers. A worker must: run its CLI as a
subprocess, never via ``shell=True`` or a shell-composed string; reject invalid
config before starting anything (``validate_worker_config``); cap stdout/stderr
(``truncate_output``); pass only the variables its CLI needs (``filtered_env``) -
application secrets such as ``AGENT_API_KEY`` are never handed to an external
agent; and return a :class:`WorkerResult` naming the provider.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

# Sandbox vocabulary shared by every worker (each CLI maps these to its own flags;
# the meaning is fixed here): read-only may inspect but must not change the
# working directory; workspace-write may change files inside it; danger-full-access
# has no confinement and is only ever used on explicit request.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

# Environment variables every CLI needs regardless of provider; provider-specific
# additions (auth, endpoints, config homes) are passed by the worker.
BASE_ENV_PASSTHROUGH: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    }
)


class WorkerConfigError(ValueError):
    """Raised for invalid worker configuration (bad path, unknown sandbox, …)."""


@dataclass
class WorkerResult:
    """Outcome of one external coding-agent call. ``provider`` names the agent
    (defaults to ``"codex"``, the only provider older code knew)."""

    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    runtime_ms: int
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    # Parsed from the CLI's structured event stream
    thread_id: str | None
    final_message: str | None
    usage: dict[str, int] | None  # keys: input_tokens, output_tokens, …
    # Internal metadata
    task: str
    working_dir: str
    sandbox: str
    error: str | None  # worker-level error (binary not found, invalid dir, …)
    provider: str = "codex"

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "runtime_ms": self.runtime_ms,
            "timed_out": self.timed_out,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "thread_id": self.thread_id,
            "final_message": self.final_message,
            "usage": self.usage,
            "task": self.task,
            "working_dir": self.working_dir,
            "sandbox": self.sandbox,
            "error": self.error,
            "provider": self.provider,
        }


def truncate_output(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cap *text* at *max_bytes* UTF-8 bytes; report whether it was cut."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return truncated + "\n[... output truncated]", True


def validate_worker_config(task: str, working_dir: str, sandbox: str) -> None:
    """Reject an unusable call before any subprocess starts: empty task, unknown
    sandbox, relative or non-existing working directory -> ``WorkerConfigError``."""
    if not task or not task.strip():
        raise WorkerConfigError("task must not be empty")

    if sandbox not in SANDBOX_MODES:
        raise WorkerConfigError(
            f"invalid sandbox {sandbox!r}; must be one of {SANDBOX_MODES}"
        )

    if not os.path.isabs(working_dir):
        raise WorkerConfigError(
            f"working_dir must be an absolute path, got: {working_dir!r}"
        )

    resolved = str(Path(working_dir).resolve())
    if not os.path.isdir(resolved):
        raise WorkerConfigError(
            f"working_dir does not exist or is not a directory: {resolved!r}"
        )


def filtered_env(extra_keys: frozenset[str] | set[str] | tuple[str, ...] = ()) -> dict[str, str]:
    """Minimal environment for a worker subprocess: only ``BASE_ENV_PASSTHROUGH``
    plus the provider's *extra_keys*; application secrets (``AGENT_API_KEY`` and
    friends) are never handed to an external agent."""
    allowed = BASE_ENV_PASSTHROUGH | frozenset(extra_keys)
    return {key: value for key, value in os.environ.items() if key in allowed}


# Progress-aware agent runs (#327): every output line counts as progress, so the
# run stops only after a silent stall window, or at the absolute cap as a final
# guard (a fixed timeout cut off finished work, #319/#326).
AGENT_STALL_ENV_VAR = "NEXUS_AGENT_STALL_SECONDS"
AGENT_MAX_ENV_VAR = "NEXUS_AGENT_MAX_SECONDS"
# 30 min default: an agent says nothing while its own test run is going (#340 was
# stopped after 26 min of good work during a 10 min silent stretch).
DEFAULT_AGENT_STALL_SECONDS = 1800
DEFAULT_AGENT_MAX_SECONDS = 3 * 60 * 60
# Floors for values from the environment, like NEXUS_TEST_STALL_SECONDS (#209).
MIN_AGENT_STALL_SECONDS = 120
MIN_AGENT_MAX_SECONDS = 30 * 60

STOP_STALLED = "stalled"
STOP_MAX_RUNTIME = "max_runtime"
STOP_REQUESTED = "requested"

# Output kept while the agent runs (#328 review): head and tail only, never all.
CAPTURE_HEAD_BYTES = 64 * 1024
CAPTURE_TAIL_BYTES = 1024 * 1024
# Time the agent's process group gets to exit after SIGTERM before SIGKILL.
TERMINATE_GRACE_SECONDS = 10.0
# Progress is logged at least this often while the agent lives (#400), so the
# stall sweep has something to read and a silent worker stands out.
PROGRESS_HEARTBEAT_SECONDS = 10 * 60


def _env_seconds(env: Mapping[str, str], key: str, *, default: float, floor: float) -> float:
    try:
        value = float(str(env.get(key, "")).strip())
    except ValueError:
        return default
    return max(floor, value) if value > 0 else default


def agent_watchdog_limits(env: Mapping[str, str] | None = None) -> tuple[float, float]:
    """(stall_seconds, max_seconds) from the environment or the defaults."""
    source = os.environ if env is None else env
    stall = _env_seconds(source, AGENT_STALL_ENV_VAR, default=DEFAULT_AGENT_STALL_SECONDS, floor=MIN_AGENT_STALL_SECONDS)
    cap = _env_seconds(source, AGENT_MAX_ENV_VAR, default=DEFAULT_AGENT_MAX_SECONDS, floor=MIN_AGENT_MAX_SECONDS)
    return stall, max(cap, stall * 2)


@dataclass(slots=True)
class WatchedRun:
    exit_code: int | None
    stdout: str
    stderr: str
    stop_reason: str | None  # None or one of the STOP_* constants


class _BoundedCapture:
    """Keeps the first and the last lines of a stream, within a byte budget."""

    def __init__(self, head_bytes: int = CAPTURE_HEAD_BYTES, tail_bytes: int = CAPTURE_TAIL_BYTES) -> None:
        self._head: list[str] = []
        self._head_left = head_bytes
        self._tail: deque[str] = deque()
        self._tail_size = 0
        self._tail_bytes = tail_bytes
        self.dropped = False

    def add(self, line: str) -> None:
        size = len(line.encode("utf-8", errors="replace"))
        if size <= self._head_left and not self._tail:
            self._head.append(line)
            self._head_left -= size
            return
        self._tail.append(line)
        self._tail_size += size
        # The newest line always stays, even if it alone exceeds the budget.
        while self._tail_size > self._tail_bytes and len(self._tail) > 1:
            dropped = self._tail.popleft()
            self._tail_size -= len(dropped.encode("utf-8", errors="replace"))
            self.dropped = True

    def text(self) -> str:
        middle = "[... output truncated]\n" if self.dropped else ""
        return "".join(self._head) + middle + "".join(self._tail)


def _terminate_group(proc: subprocess.Popen, grace_seconds: float) -> None:
    """Stop the agent and everything it started (its own process group):
    SIGTERM, up to *grace_seconds* for the agent to exit, then SIGKILL."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_watched(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    stall_seconds: float,
    max_seconds: float,
    input_text: str | None = None,
    poll_seconds: float = 1.0,
    grace_seconds: float = TERMINATE_GRACE_SECONDS,
    on_process: Callable[[subprocess.Popen | None], None] | None = None,
    on_heartbeat: Callable[[float, float], None] | None = None,
    on_stdout_line: Callable[[str], bool] | None = None,
    heartbeat_seconds: float = PROGRESS_HEARTBEAT_SECONDS,
) -> WatchedRun:
    """Run *cmd*, stopping it only on a stall or at the absolute cap. Any output
    line is progress. The agent runs in its own process group, so stopping it
    also stops its children; leftovers are stopped after a normal exit too.
    Raises ``OSError`` (incl. ``FileNotFoundError``) if the process cannot start.
    Calls *on_heartbeat* every *heartbeat_seconds* with runtime and silence time.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if on_process is not None:
        on_process(proc)
    last_output = [time.monotonic()]
    stdout_capture = _BoundedCapture()
    stderr_capture = _BoundedCapture()

    stop_requested = threading.Event()

    def pump(stream: Any, sink: _BoundedCapture, line_callback: Callable[[str], bool] | None = None) -> None:
        for line in iter(stream.readline, ""):
            sink.add(line)
            last_output[0] = time.monotonic()
            if line_callback is not None:
                try:
                    if line_callback(line):
                        stop_requested.set()
                        break
                except Exception:  # noqa: BLE001 - observers must not break capture
                    pass
        stream.close()

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, stdout_capture, on_stdout_line), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, stderr_capture), daemon=True),
    ]
    for reader in readers:
        reader.start()
    if input_text is not None:
        try:
            proc.stdin.write(input_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    started = time.monotonic()
    last_heartbeat = started
    stop_reason: str | None = None
    while proc.poll() is None:
        now = time.monotonic()
        if on_heartbeat is not None and now - last_heartbeat >= heartbeat_seconds:
            last_heartbeat = now
            try:
                on_heartbeat(now - started, now - last_output[0])
            except Exception:  # noqa: BLE001 - logging must not stop the watchdog
                pass
        if stop_requested.is_set():
            stop_reason = STOP_REQUESTED
        elif now - last_output[0] > stall_seconds:
            stop_reason = STOP_STALLED
        elif now - started > max_seconds:
            stop_reason = STOP_MAX_RUNTIME
        if stop_reason:
            break
        time.sleep(poll_seconds)
    try:
        _terminate_group(proc, grace_seconds if stop_reason else 0)
        exit_code = proc.wait()
    finally:
        if on_process is not None:
            on_process(None)
    for reader in readers:
        reader.join(timeout=5)
    return WatchedRun(
        exit_code=None if stop_reason else exit_code,
        stdout=stdout_capture.text(),
        stderr=stderr_capture.text(),
        stop_reason=stop_reason,
    )


def progress_heartbeat(
    experiment_logger: Any | None, phase: str, agent: str
) -> Callable[[float, float], None] | None:
    """``on_heartbeat`` that logs ``coding_agent_progress`` to an experiment log."""
    if experiment_logger is None:
        return None

    def beat(runtime_seconds: float, silent_seconds: float) -> None:
        experiment_logger.info(
            phase, "coding_agent_progress",
            f"{agent} still running after {int(runtime_seconds)}s; "
            f"last output {int(silent_seconds)}s ago",
            agent=agent,
            runtime_seconds=int(runtime_seconds),
            silent_seconds=int(silent_seconds),
        )

    return beat


def watchdog_error(agent: str, run: WatchedRun, stall_seconds: float, max_seconds: float) -> str:
    if run.stop_reason == STOP_STALLED:
        return f"{agent} stalled: no output for {int(stall_seconds)}s"
    if run.stop_reason == STOP_REQUESTED:
        return f"{agent} stopped by the run observer"
    return f"{agent} reached the absolute cap of {int(max_seconds)}s"
