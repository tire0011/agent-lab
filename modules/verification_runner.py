"""The verification test subprocess runner.

``run_verification_test_monitored`` streams the test process's output, records
every test start and result to a dedicated per-test log, maintains a test
heartbeat, and runs a watchdog that decides on *progress* rather than on total
runtime (#209). It replaced a fixed-timeout runner outright rather than
sitting beside one: two runners would have meant two answers to "did this run
fail?", and the wall-clock one was the wrong answer.

Why the fixed timeout had to go
-------------------------------
A wall-clock limit cannot tell these apart:

* a healthy but slow suite,
* one stuck test,
* a dead test process,
* a run that is still actively progressing.

On 2026-09-16 experiment ``df97d67bc08e45b4bc5c573db0c84d85`` fell back to
local verification and was reported ``timed_out`` with no exit code, which
says nothing about which of the four actually happened. The watchdog here
answers that question directly: a run continues for as long as tests keep
starting and finishing, and is aborted when progress stops - with diagnostics
naming the test it stopped on.

The absolute cap remains, but only as a final guard against a permanently
orphaned process. It is deliberately large (90 minutes by default) so that it
is never the mechanism that decides whether a healthy run is failing.

GitHub CI verification is untouched by all of this: it does not run a local
subprocess at all.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from modules.test_progress import (
    EVENT_END,
    EVENT_START,
    ProgressTracker,
    UnittestStreamParser,
    format_stall_diagnostics,
    parse_total_tests,
)
from modules.test_run_log import VerificationTestLog, prune_test_logs
from modules.verification_env import build_verification_test_env

logger = logging.getLogger("nexus.verification_runner")

# How long the whole suite may go without a single test starting or finishing
# before it counts as stalled. Sized against the slowest *single* test plus a
# wide margin, not against the suite: this is "nothing happened at all", which
# a healthy run never produces.
DEFAULT_STALL_TIMEOUT_SECONDS = 900  # 15 minutes
# Floor for the configured stall timeout: below a few minutes a genuinely slow
# single test would be killed as a stall, which is the failure mode this whole
# change exists to remove.
MIN_STALL_TIMEOUT_SECONDS = 120

# The watchdog wakes roughly once a minute: often enough to notice a stall
# promptly, rare enough that the heartbeat it publishes to the global Nexus
# log is one entry per minute rather than a flood.
DEFAULT_WATCHDOG_INTERVAL_SECONDS = 60.0
MIN_WATCHDOG_INTERVAL_SECONDS = 1.0

# Final fail-safe only - see the module docstring.
DEFAULT_SAFETY_CAP_SECONDS = 90 * 60
MIN_SAFETY_CAP_SECONDS = 30 * 60

STALL_TIMEOUT_ENV_VAR = "NEXUS_TEST_STALL_SECONDS"
WATCHDOG_INTERVAL_ENV_VAR = "NEXUS_TEST_WATCHDOG_INTERVAL_SECONDS"
SAFETY_CAP_ENV_VAR = "NEXUS_TEST_SAFETY_CAP_SECONDS"

# Grace period between asking the test process group to terminate and killing
# it outright, so an aborted run still gets a chance to flush its output.
_TERMINATE_GRACE_SECONDS = 10.0

_READ_CHUNK_BYTES = 65536


def _coerce_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    """Progress rules for one monitored verification run.

    Constructed directly (tests use sub-second intervals) or through
    :meth:`resolve`, which falls back to the environment and clamps what it
    finds there to a floor - an operator's configuration must not be able to
    reintroduce a short fixed timeout by another name.
    """

    stall_timeout_seconds: float = DEFAULT_STALL_TIMEOUT_SECONDS
    check_interval_seconds: float = DEFAULT_WATCHDOG_INTERVAL_SECONDS
    safety_cap_seconds: float = DEFAULT_SAFETY_CAP_SECONDS

    @classmethod
    def resolve(
        cls,
        *,
        stall_timeout_seconds: object = None,
        check_interval_seconds: object = None,
        safety_cap_seconds: object = None,
        env: Mapping[str, str] | None = None,
    ) -> "WatchdogConfig":
        """Build a config from explicit values, the environment, or defaults.

        The floors apply to what comes out of the *environment*, because that
        is where a typo turns into "declare healthy runs dead": an operator
        who sets ``NEXUS_TEST_STALL_SECONDS=5`` must not thereby restore the
        short fixed timeout #209 removed. An explicit argument is code, and is
        trusted as written - which is what lets a test drive the watchdog in
        seconds instead of minutes.

        One invariant holds either way: the safety cap always stays above the
        stall timeout, so the cap can never become the mechanism that ends a
        run first.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        stall = _resolve_seconds(
            stall_timeout_seconds,
            source.get(STALL_TIMEOUT_ENV_VAR),
            default=DEFAULT_STALL_TIMEOUT_SECONDS,
            floor=MIN_STALL_TIMEOUT_SECONDS,
        )
        interval = _resolve_seconds(
            check_interval_seconds,
            source.get(WATCHDOG_INTERVAL_ENV_VAR),
            default=DEFAULT_WATCHDOG_INTERVAL_SECONDS,
            floor=MIN_WATCHDOG_INTERVAL_SECONDS,
        )
        cap = _resolve_seconds(
            safety_cap_seconds,
            source.get(SAFETY_CAP_ENV_VAR),
            default=DEFAULT_SAFETY_CAP_SECONDS,
            floor=MIN_SAFETY_CAP_SECONDS,
        )
        return cls(stall, interval, max(cap, stall * 2))


def _resolve_seconds(
    explicit: object,
    from_env: object,
    *,
    default: float,
    floor: float,
) -> float:
    """Explicit value as given; otherwise the environment, clamped, or the default."""
    value = _coerce_number(explicit)
    if value is not None and value > 0:
        return value
    value = _coerce_number(from_env)
    if value is not None and value > 0:
        return max(floor, value)
    return default


# Abort reasons, in the order the watchdog checks them.
ABORT_SAFETY_CAP = "safety_cap_exceeded"
ABORT_STALLED = "stalled"


@dataclass(slots=True)
class VerificationRunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    exception: BaseException | None
    duration_ms: int
    #: Set when the watchdog aborted the run because progress stopped.
    stalled: bool = False
    #: Set when the absolute safety cap - not progress - ended the run.
    safety_cap_exceeded: bool = False
    #: Set when the test process was killed by a signal instead of exiting.
    process_died: bool = False
    #: Human-readable stall report; present whenever the watchdog aborted.
    stall_diagnostics: str | None = None
    #: Final test heartbeat snapshot, for logging and failure analysis.
    progress: dict[str, Any] | None = None
    #: Path of the dedicated per-test log for this run, when one was written.
    test_log_path: str | None = None
    #: Total tests reported by the suite itself, when it got that far.
    tests_total: int | None = None
    #: Exact unittest IDs which completed unsuccessfully, plus the current
    #: test when the watchdog stopped a stalled run.
    failed_test_ids: tuple[str, ...] = ()
    #: Result by exact ID for tests which reached a unittest verdict.
    test_results: dict[str, str] | None = None

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


def run_verification_test_monitored(
    command: Sequence[str],
    *,
    cwd: str,
    run_id: str,
    config: WatchdogConfig | None = None,
    expected_total: int | None = None,
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
    test_log_dir: str | Path | None = None,
    prune_logs: bool = True,
    on_process: Callable[[subprocess.Popen | None], None] | None = None,
) -> VerificationRunResult:
    """Run the verification tests under a progress-aware watchdog.

    *on_heartbeat* is invoked once per watchdog tick with the current test
    heartbeat. That is how the global Nexus heartbeat learns that verification
    is alive and progressing: the caller writes one summary entry per tick to
    its own experiment log, while the per-test detail stays in the dedicated
    test log. A failing callback is logged and never propagates - the callers'
    loggers are instrumentation, not part of the test run.
    """
    config = config or WatchdogConfig.resolve()
    started = time.perf_counter()
    test_log = VerificationTestLog(run_id, log_dir=test_log_dir)
    if prune_logs:
        prune_test_logs(test_log_dir)

    parser = UnittestStreamParser()
    tracker = ProgressTracker(expected_total=expected_total)
    test_results: dict[str, str] = {}
    stdout_chunks: list[str] = []

    env = build_verification_test_env()
    # Without this the child's stderr can sit in a block buffer when it is a
    # pipe, and "no progress" would then mean "no flush" - the watchdog would
    # abort healthy runs.
    env["PYTHONUNBUFFERED"] = "1"

    test_log.log_run_event(
        "test_run_started", "Verification test run started",
        command=list(command), cwd=str(cwd),
        stall_timeout_seconds=config.stall_timeout_seconds,
        safety_cap_seconds=config.safety_cap_seconds,
        check_interval_seconds=config.check_interval_seconds,
        expected_total=expected_total,
    )

    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
            list(command), cwd=cwd, shell=False, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if on_process is not None:
            on_process(process)
    except (OSError, subprocess.SubprocessError) as exc:
        test_log.log_run_event("test_run_spawn_failed", f"failed to run test command: {exc}")
        return VerificationRunResult(
            None, "", f"failed to run test command: {exc}", False, exc,
            int((time.perf_counter() - started) * 1000),
            test_log_path=str(test_log.path),
        )

    parser_lock = threading.Lock()

    def _drain_stderr() -> None:
        for chunk in _iter_chunks(process.stderr):
            with parser_lock:
                events = parser.feed(chunk)
            tracker.note_activity()
            for event in events:
                tracker.record(event)
                _log_test_event(test_log, event)
                if event.kind == EVENT_END and event.result:
                    test_results[event.test_id] = event.result

    def _drain_stdout() -> None:
        for chunk in _iter_chunks(process.stdout):
            stdout_chunks.append(chunk)
            tracker.note_activity()

    readers = [
        threading.Thread(target=_drain_stderr, name=f"verif-stderr-{test_log.run_id}", daemon=True),
        threading.Thread(target=_drain_stdout, name=f"verif-stdout-{test_log.run_id}", daemon=True),
    ]
    for reader in readers:
        reader.start()

    abort_reason: str | None = None
    stall_snapshot: dict[str, Any] | None = None
    while True:
        try:
            process.wait(timeout=config.check_interval_seconds)
            break
        except subprocess.TimeoutExpired:
            pass

        with parser_lock:
            current_test = parser.current_test
        snapshot = tracker.snapshot(
            current_test_running=current_test,
            **_process_state(process),
        )
        test_log.write_heartbeat(snapshot)
        _notify(on_heartbeat, snapshot)

        if snapshot["elapsed_seconds"] >= config.safety_cap_seconds:
            abort_reason, stall_snapshot = ABORT_SAFETY_CAP, snapshot
            break
        if tracker.seconds_since_progress() >= config.stall_timeout_seconds:
            abort_reason, stall_snapshot = ABORT_STALLED, snapshot
            break

    if abort_reason is not None:
        _terminate(process)

    for reader in readers:
        reader.join(timeout=_TERMINATE_GRACE_SECONDS)
    for stream in (process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:  # pragma: no cover - the process is already gone
            pass

    with parser_lock:
        parser.flush()
        stderr_text = parser.filtered_output
    stdout_text = "".join(stdout_chunks)
    total_tests = parse_total_tests(stderr_text) or parse_total_tests(stdout_text)
    tracker.set_total_tests(total_tests)

    exit_code = process.returncode
    if on_process is not None:
        on_process(None)
    # A negative return code means a signal, not a verdict: the suite never
    # got to decide anything. Reporting it as an exit status would let an
    # OOM-killed or externally terminated run be filed as "tests failed".
    process_died = abort_reason is None and exit_code is not None and exit_code < 0
    duration_ms = int((time.perf_counter() - started) * 1000)
    final_snapshot = tracker.snapshot(**_process_state(process))
    test_log.write_heartbeat(final_snapshot)

    diagnostics: str | None = None
    if process_died:
        diagnostics = format_stall_diagnostics(
            final_snapshot,
            title=f"Test process died: killed by signal {-exit_code}",
        )
        stderr_text += f"\n[{diagnostics}]\n"
        test_log.log_run_event("test_process_died", diagnostics, **final_snapshot)
    elif abort_reason is not None:
        title = (
            "Test run stalled" if abort_reason == ABORT_STALLED
            else "Test run hit the absolute safety cap"
        )
        diagnostics = format_stall_diagnostics(stall_snapshot or final_snapshot, title=title)
        stderr_text += f"\n[{diagnostics}]\n"
        test_log.log_run_event(abort_reason, diagnostics, **(stall_snapshot or final_snapshot))
    else:
        test_log.log_run_event(
            "test_run_finished",
            f"Verification test run finished with exit code {exit_code}",
            exit_code=exit_code, duration_ms=duration_ms, **final_snapshot,
        )

    failed_ids = [
        test_id for test_id, result in test_results.items()
        if result in {"failed", "error", "unexpected_success"}
    ]
    if abort_reason == ABORT_STALLED:
        stalled_test = (stall_snapshot or {}).get("current_test")
        if stalled_test and stalled_test not in failed_ids:
            failed_ids.append(str(stalled_test))

    return VerificationRunResult(
        None if (abort_reason is not None or process_died) else exit_code,
        stdout_text,
        stderr_text,
        abort_reason is not None,
        None,
        duration_ms,
        stalled=abort_reason == ABORT_STALLED,
        safety_cap_exceeded=abort_reason == ABORT_SAFETY_CAP,
        process_died=process_died,
        stall_diagnostics=diagnostics,
        progress=final_snapshot,
        test_log_path=str(test_log.path),
        tests_total=total_tests,
        failed_test_ids=tuple(failed_ids),
        test_results=test_results,
    )


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _iter_chunks(stream: Any):
    """Yield decoded chunks as soon as the pipe has any bytes.

    ``os.read`` rather than ``readline``: unittest writes a test's description
    and ``" ... "`` without a newline at start, and that unterminated fragment
    is precisely what names the *currently running* test.
    """
    if stream is None:
        return
    fileno = stream.fileno()
    while True:
        try:
            raw = os.read(fileno, _READ_CHUNK_BYTES)
        except (OSError, ValueError):
            return
        if not raw:
            return
        yield raw.decode("utf-8", errors="replace")


def _process_state(process: subprocess.Popen) -> dict[str, Any]:
    """PID, liveness and - where the platform offers it - CPU/memory.

    CPU and memory are what distinguish a busy process from a deadlocked one,
    so they are worth reading; they come from ``/proc`` directly rather than
    from a new dependency, and their absence is never an error.
    """
    alive = process.poll() is None
    state: dict[str, Any] = {"pid": process.pid, "process_alive": alive}
    if not alive:
        return state
    try:
        stat_fields = Path(f"/proc/{process.pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)
        fields = stat_fields[1].split()
        ticks = os.sysconf("SC_CLK_TCK") or 100
        # utime + stime, fields 14 and 15 of proc(5), i.e. index 11 and 12 here.
        state["cpu_seconds"] = round((int(fields[11]) + int(fields[12])) / ticks, 2)
        state["process_state"] = fields[0]
        state["rss_bytes"] = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        # Not Linux, or the process exited between poll and read. Neither is a
        # problem: liveness alone already answers the important question.
        pass
    return state


def _terminate(process: subprocess.Popen) -> None:
    """Stop the test process group, escalating to SIGKILL after a grace period.

    The group, not the process: ``unittest`` runs are free to spawn children,
    and killing only the parent is how orphans that outlive the run are made.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, AttributeError):
        process.terminate()
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, AttributeError):
        process.kill()
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
        logger.error("verification test process %s survived SIGKILL", process.pid)


def _log_test_event(test_log: VerificationTestLog, event) -> None:
    if event.kind == EVENT_START:
        test_log.log_test_start(event.test_id, at=event.at)
    elif event.kind == EVENT_END:
        test_log.log_test_end(
            event.test_id,
            result=event.result or "unknown",
            started_at=event.started_at,
            at=event.at,
            duration_ms=event.duration_ms,
        )


def _notify(callback: Callable[[dict[str, Any]], None] | None, snapshot: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(snapshot)
    except Exception:  # noqa: BLE001 - a heartbeat must not break the run
        logger.exception("verification heartbeat callback failed")
