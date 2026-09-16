from __future__ import annotations

import codecs
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from modules.verification_progress import (
    VerificationProgressSnapshot,
    VerificationProgressTracker,
    UnittestProgressParser,
)
from modules.verification_env import build_verification_test_env

# Progress-aware watchdog defaults (#209). The stall window - not total
# runtime - is what decides whether a run is failing, so it is generous:
# a suite that is still starting and finishing tests is healthy however
# long it has been going. The safety cap exists only to reap a permanently
# orphaned process and must never be the normal verdict.
DEFAULT_STALL_SECONDS = 600  # 10 minutes with no observable progress
DEFAULT_POLL_SECONDS = 60.0  # the issue's "check roughly once per minute"
DEFAULT_SAFETY_CAP_SECONDS = 75 * 60

# A stall window shorter than this would start declaring slow-but-healthy
# tests dead, which is the failure mode #209 exists to remove.
MIN_STALL_SECONDS = 60
MIN_POLL_SECONDS = 0.01
MIN_SAFETY_CAP_SECONDS = 60

STALL_ENV_VAR = "NEXUS_TEST_STALL_SECONDS"
POLL_ENV_VAR = "NEXUS_TEST_WATCHDOG_POLL_SECONDS"
SAFETY_CAP_ENV_VAR = "NEXUS_TEST_SAFETY_CAP_SECONDS"

# How long a killed process gets to exit on SIGTERM before SIGKILL, and how
# long the readers get to drain after the child is gone (a grandchild can
# hold the pipe open after its parent exits, and nothing may hang on that).
_TERMINATE_GRACE_SECONDS = 5.0
_READER_JOIN_SECONDS = 10.0
_READ_CHUNK_BYTES = 65_536

STALL_NO_PROGRESS = "no_progress"
STALL_SAFETY_CAP = "safety_cap"
STALL_PROCESS_DIED = "process_died"


def _coerce_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class VerificationWatchdogConfig:
    """When to stop believing a local test run is still making progress."""

    stall_seconds: float = DEFAULT_STALL_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    safety_cap_seconds: float = DEFAULT_SAFETY_CAP_SECONDS

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "VerificationWatchdogConfig":
        """Operator-supplied windows, clamped to values that stay safe.

        A too-short stall window would resurrect exactly the failure #209
        removes - declaring a slow but healthy suite dead - so an
        unparsable or too-small value is raised to the floor rather than
        rejected: a typo in the environment must not take verification
        down, and must not shorten the window that protects a healthy run.
        Direct construction is left unclamped so tests can use fast windows.
        """
        source = env if env is not None else os.environ
        defaults = cls()
        stall = max(
            MIN_STALL_SECONDS,
            _coerce_number(source.get(STALL_ENV_VAR)) or defaults.stall_seconds,
        )
        return cls(
            stall_seconds=stall,
            poll_seconds=max(
                MIN_POLL_SECONDS,
                _coerce_number(source.get(POLL_ENV_VAR)) or defaults.poll_seconds,
            ),
            safety_cap_seconds=max(
                MIN_SAFETY_CAP_SECONDS,
                stall,
                _coerce_number(source.get(SAFETY_CAP_ENV_VAR)) or defaults.safety_cap_seconds,
            ),
        )


@dataclass(slots=True)
class VerificationRunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    exception: BaseException | None
    duration_ms: int
    stalled: bool = False
    stall_reason: str | None = None
    diagnostics: str | None = None
    progress: dict[str, Any] | None = None

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


ProgressCallback = Callable[[VerificationProgressSnapshot], None]


def run_verification_test(
    command: Sequence[str],
    *,
    cwd: str,
    timeout_seconds: int | None = None,
    watchdog: VerificationWatchdogConfig | None = None,
    tracker: VerificationProgressTracker | None = None,
    on_progress: ProgressCallback | None = None,
) -> VerificationRunResult:
    """Shared verification subprocess runner for experiment and adoption.

    With no *watchdog* this is the original fixed-timeout run. With one, the
    run is judged on progress instead (#209): it is allowed to continue for
    as long as tests keep starting and finishing, and is stopped only when
    progress stops, the process dies, or the absolute safety cap is reached.
    """
    if watchdog is None:
        return _run_with_fixed_timeout(command, cwd=cwd, timeout_seconds=timeout_seconds)
    return _run_with_watchdog(
        command, cwd=cwd, watchdog=watchdog, tracker=tracker, on_progress=on_progress
    )


def _run_with_fixed_timeout(
    command: Sequence[str], *, cwd: str, timeout_seconds: int | None
) -> VerificationRunResult:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            list(command), cwd=cwd, capture_output=True, text=True,
            timeout=timeout_seconds, shell=False,
            env=build_verification_test_env(),
        )
        return VerificationRunResult(
            proc.returncode, proc.stdout or "", proc.stderr or "",
            False, None, int((time.perf_counter() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return VerificationRunResult(
            None, stdout, stderr + "\n[test command timed out]", True, exc,
            int((time.perf_counter() - started) * 1000),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return VerificationRunResult(
            None, "", f"failed to run test command: {exc}", False, exc,
            int((time.perf_counter() - started) * 1000),
        )


def _run_with_watchdog(
    command: Sequence[str],
    *,
    cwd: str,
    watchdog: VerificationWatchdogConfig,
    tracker: VerificationProgressTracker | None,
    on_progress: ProgressCallback | None,
) -> VerificationRunResult:
    started = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=build_verification_test_env(),
            # Its own process group, so a stalled runner that spawned
            # children is killed whole rather than leaving them orphaned.
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return VerificationRunResult(
            None, "", f"failed to run test command: {exc}", False, exc, elapsed_ms()
        )

    progress = tracker or VerificationProgressTracker(pid=proc.pid)
    progress.snapshot.pid = proc.pid
    streams = {"stdout": proc.stdout, "stderr": proc.stderr}
    collected: dict[str, list[str]] = {"stdout": [], "stderr": []}
    parsers = {name: UnittestProgressParser() for name in streams}
    readers = [
        threading.Thread(
            target=_drain_stream,
            args=(stream, collected[name], parsers[name], progress),
            name=f"verification-{name}",
            daemon=True,
        )
        for name, stream in streams.items()
        if stream is not None
    ]
    for reader in readers:
        reader.start()

    stall_reason: str | None = None
    diagnostics: str | None = None
    try:
        stall_reason, diagnostics = _watch(proc, progress, watchdog, on_progress)
    finally:
        for reader in readers:
            reader.join(timeout=_READER_JOIN_SECONDS)
        for stream in streams.values():
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    for parser in parsers.values():
        progress.note_total(parser.total_from_summary)
    exit_code = proc.returncode
    progress.note_process_alive(False)
    progress.publish()
    snapshot = progress.snapshot

    stdout = "".join(collected["stdout"])
    stderr = "".join(collected["stderr"])
    if stall_reason is None and exit_code is not None and exit_code < 0:
        # Killed by a signal from outside: there *is* an exit status, so this
        # is not a timeout, but the suite never finished and the operator
        # needs to see where it died.
        stall_reason = STALL_PROCESS_DIED
        diagnostics = _describe_stall(stall_reason, snapshot, watchdog)
    if diagnostics is not None:
        # Leading, not trailing: stored test output is truncated head-first,
        # and verbose per-test output is long enough that a diagnostic at the
        # end would be the first thing cut - exactly when it matters most.
        stderr = f"{diagnostics}\n\n{stderr}"

    timed_out = stall_reason in (STALL_NO_PROGRESS, STALL_SAFETY_CAP)
    return VerificationRunResult(
        None if timed_out else exit_code,
        stdout,
        stderr,
        timed_out,
        None,
        elapsed_ms(),
        stalled=stall_reason is not None,
        stall_reason=stall_reason,
        diagnostics=diagnostics,
        progress=snapshot.to_dict(),
    )


def _watch(
    proc: subprocess.Popen,
    progress: VerificationProgressTracker,
    watchdog: VerificationWatchdogConfig,
    on_progress: ProgressCallback | None,
) -> tuple[str | None, str | None]:
    """Poll for progress until the run ends.

    Returns why the run was stopped and the diagnostic for it. The
    diagnostic is rendered *before* the kill on purpose: whether the
    process was still alive while making no progress is what separates a
    deadlock from a dead process, and killing it destroys that answer.
    """
    started = time.monotonic()
    while True:
        if _wait_for_exit(proc, watchdog.poll_seconds):
            return None, None
        progress.note_process_alive(True)
        progress.note_process_stats(proc.pid)
        progress.publish()
        if on_progress is not None:
            try:
                on_progress(progress.snapshot)
            except Exception:  # noqa: BLE001 - reporting never breaks the run
                pass

        reason: str | None = None
        if time.monotonic() - started >= watchdog.safety_cap_seconds:
            reason = STALL_SAFETY_CAP
        elif progress.snapshot.age_seconds() >= watchdog.stall_seconds:
            reason = STALL_NO_PROGRESS
        if reason is not None:
            diagnostics = _describe_stall(reason, progress.snapshot, watchdog)
            _kill(proc)
            return reason, diagnostics


def _wait_for_exit(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _drain_stream(
    stream: Any,
    sink: list[str],
    parser: UnittestProgressParser,
    progress: VerificationProgressTracker,
) -> None:
    """Read bytes as they arrive and turn them into progress.

    ``read``/``readline`` both block until a full buffer or a newline, and
    unittest flushes the test id *without* a trailing newline before running
    it - so only raw reads can tell us which test is currently running, which
    is exactly what a stall diagnostic has to name.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    fileno = stream.fileno()
    while True:
        try:
            chunk = os.read(fileno, _READ_CHUNK_BYTES)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        text = decoder.decode(chunk)
        if not text:
            continue
        sink.append(text)
        progress.note_output(text)
        progress.note_events(parser.feed(text))
    trailing = decoder.decode(b"", True)
    if trailing:
        sink.append(trailing)
        progress.note_events(parser.feed(trailing))


def _kill(proc: subprocess.Popen) -> None:
    """Stop the whole process group, politely first."""
    for send, wait in ((_signal_group, _TERMINATE_GRACE_SECONDS), (_kill_group, 1.0)):
        if proc.poll() is not None:
            return
        send(proc)
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _signal_group(proc: subprocess.Popen) -> None:
    _send(proc, signal.SIGTERM, proc.terminate)


def _kill_group(proc: subprocess.Popen) -> None:
    _send(proc, signal.SIGKILL, proc.kill)


def _send(proc: subprocess.Popen, sig: int, fallback: Callable[[], None]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
        return
    except (OSError, AttributeError, ProcessLookupError):
        pass
    try:
        fallback()
    except (OSError, ProcessLookupError):
        pass


def _describe_stall(
    reason: str, snapshot: VerificationProgressSnapshot, watchdog: VerificationWatchdogConfig
) -> str:
    headline = {
        STALL_NO_PROGRESS: (
            f"Test run stalled (no progress for {int(watchdog.stall_seconds)}s)"
        ),
        STALL_SAFETY_CAP: (
            "Test run hit the absolute safety cap "
            f"({int(watchdog.safety_cap_seconds)}s)"
        ),
        STALL_PROCESS_DIED: "Test process died before the suite finished",
    }.get(reason, "Test run stopped")
    return f"{headline}\n{snapshot.describe()}"
