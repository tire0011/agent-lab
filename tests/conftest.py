"""Shared test setup.

This file runs before any test module is imported, which is the only point at
which these can still take effect: both settings are read into module-level
constants and bound as default arguments at import time.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Keep the suite out of the production log directory. `DEFAULT_LOG_DIR` is a
# relative path, so without this a test run writes into
# /agent/state/experiment-logs alongside real history - which is how test
# controller turns were first mistaken for production data while measuring
# escalation rates for #158.
_LOG_DIR = tempfile.mkdtemp(prefix="nexus-test-logs-")
os.environ.setdefault("AGENT_LOG_DIR", _LOG_DIR)

# api.py reads this at module level, and several service defaults resolve the
# database path at import time. Without it a suite run can touch the real
# state database.
#
# The value is not arbitrary. Every API test module sends `x-api-key:
# test-key`, and .github/workflows/tests.yml exports `AGENT_API_KEY: test-key`
# - so in CI this setdefault never fires and the suite is green. Anywhere the
# variable is unset, which is every developer machine and the VM, this line
# won the race with a *different* key and ~90 authenticated tests answered 401
# instead of 200. A suite that passes only under the one environment that
# happens to override it is not passing.
os.environ.setdefault("AGENT_API_KEY", "test-key")

# The services default to AGENT_DB_PATH or /var/lib/nexus/agent.db. The coding
# agent runs this suite inside an experiment worktree on the VM, where a
# lifespan test then marked the running experiment "interrupted by service
# restart" (#309). Assigned, not setdefault: the production value is set there.
os.environ["AGENT_DB_PATH"] = str(Path(_LOG_DIR) / "agent.db")

# Test databases live in temporary directories and are deleted with them, so
# there is nothing for an fsync to protect and the suite pays for it on every
# commit. On the VM that is the dominant cost of a run: 500 commits take 5.01s
# there against 0.37s on a dev box, while pure CPU is only 1.6x slower and
# process spawning 2.8x. WAL + synchronous=NORMAL takes the same 500 commits
# from 0.463s to 0.007s.
#
# Production is untouched: modules/run_store/schema.py only relaxes durability
# when this variable says so, and nothing outside the test suite sets it.
os.environ.setdefault("NEXUS_DB_DURABILITY", "relaxed")

# Mailbox configuration is required at import time by api.py. Point it at an
# isolated canonical-layout checkout so tests can never fall back to /agent.
os.environ.setdefault("AGENT_MAILBOX_REPO_PATH", str(Path(_LOG_DIR) / "mailbox-repo"))

# The suite must never reach a real model provider. It could: the controller
# escalates to `codex_readonly` when a prompt exceeds the local budget, and on
# the VM the codex binary exists - so a test whose injected client was supposed
# to answer instead spent a real ~6s Codex call, returned a different answer,
# and failed. Worse, whether that happened depended on how much memory the host
# had free, so results varied between runs (#158).
#
# Assigned, not setdefault: the isolation must not depend on the variable
# happening to be unset. The service currently relies on the module default
# rather than setting it, but any shell or future deployment that does set it
# would otherwise hand the suite a working binary again. A test that needs a
# codex path sets it through monkeypatch, which applies after this file runs.
os.environ["AGENT_CODEX_BIN"] = str(Path(_LOG_DIR) / "no-codex-binary-in-tests")

# Same isolation for the second coding agent: the claude CLI is installed on
# the VM too, and a task whose provider was drawn at random would otherwise
# spend a real Claude call. With both binaries unreachable, the provider draw
# finds nothing installed and falls back to codex, which keeps the suite
# deterministic as well.
os.environ["AGENT_CLAUDE_BIN"] = str(Path(_LOG_DIR) / "no-claude-binary-in-tests")

# Same isolation for the local model server: llama-server runs on the VM, so
# an accidentally default-constructed client can otherwise make a real request
# and wait for its 300-second timeout. Assigned, not setdefault, because the
# production URL is present in the VM environment. Tests that need a model URL
# set it through monkeypatch after this file runs.
_UNREACHABLE_LOCAL_MODEL_URL = "http://127.0.0.1:9"
os.environ["LOCAL_MODEL_URL"] = _UNREACHABLE_LOCAL_MODEL_URL
os.environ["LLAMA_CPP_BASE_URL"] = _UNREACHABLE_LOCAL_MODEL_URL
os.environ["LLAMA_API_URL"] = _UNREACHABLE_LOCAL_MODEL_URL

# Measured local-model throughput (#642) sizes the local worker's prompts. A
# suite run must neither read the VM's real measurements (results would then
# depend on the host) nor write fake ones into them. Assigned, not setdefault.
os.environ["LOCAL_MODEL_RATES_PATH"] = str(Path(_LOG_DIR) / "local-model-rates.json")

# Streaming (#667) is on by default in production. The suite's many client
# tests mock ``httpx.AsyncClient.post`` (the non-stream wire protocol), so the
# suite runs non-streamed unless a test opts in (``stream=True`` or
# monkeypatch); tests/test_issue_667_llama_streaming.py checks the default.
os.environ["LOCAL_MODEL_STREAM"] = "0"


# Slow-machine simulation (#681). Only when NEXUS_TEST_CLOCK_DRIFT_MS is set,
# which modules/timing_check.py does for its second run of the tests a change
# touches; a normal run is untouched. Every clock read then moves forward by
# the drift on top of real time, so code that derives a value from wall-clock
# deltas (a token budget, a remaining timeout) sees what a slower CI runner
# would see - deterministically. #675 passed on the VM and was reverted after
# CI on main came out one token off.
#
# Only reads made by this repository's own code drift. The standard library
# and installed packages (asyncio's loop clock, pytest, httpx) keep real time:
# drifting them would expire their timeouts early and report tests as
# timing-sensitive whose own code never reads the clock.
#
# A test that by design measures real elapsed time against a fake that sleeps
# in real time (e.g. a streaming server with a per-token delay) cannot be
# simulated this way: only the client clock would speed up, not the fake's
# sleeps. Such a test is marked ``@pytest.mark.real_clock(reason=...)`` and the
# drift pauses while it runs (clocks then advance in real time only, never
# backwards). The marker is an explicit, reviewable exemption; the timing
# check's hint never suggests it.
_CLOCK_DRIFT_PAUSED = [False]


def _install_clock_drift() -> None:
    raw = os.environ.get("NEXUS_TEST_CLOCK_DRIFT_MS", "").strip()
    if not raw:
        return
    try:
        drift_seconds = float(raw) / 1000.0
    except ValueError:
        return
    if not 0 < drift_seconds < float("inf"):
        return

    import sys
    import threading
    import time as _time

    repo_root = str(Path(__file__).resolve().parents[1]) + os.sep
    foreign = ("site-packages", "dist-packages", f"{os.sep}.venv{os.sep}", f"{os.sep}venv{os.sep}")
    project_file: dict[str, bool] = {}

    def from_project(filename: str) -> bool:
        known = project_file.get(filename)
        if known is None:
            known = filename.startswith(repo_root) and not any(part in filename for part in foreign)
            project_file[filename] = known
        return known

    lock = threading.Lock()
    offset = [0.0]

    def drifting(original):
        def clock() -> float:
            caller = sys._getframe(1)
            if not from_project(caller.f_code.co_filename):
                return original()
            with lock:
                if not _CLOCK_DRIFT_PAUSED[0]:
                    offset[0] += drift_seconds
                return original() + offset[0]

        clock.__wrapped__ = original  # type: ignore[attr-defined]
        clock.__name__ = original.__name__
        return clock

    # One shared offset: the three clocks stay consistent with each other for
    # project code. Foreign callers read real time, never the offset, so a
    # project clock read cannot make asyncio's timers fire early either.
    _time.monotonic = drifting(_time.monotonic)
    _time.perf_counter = drifting(_time.perf_counter)
    _time.time = drifting(_time.time)


_install_clock_drift()


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "real_clock(reason): the test measures real elapsed time against a fake that "
        "sleeps in real time; the #681 clock drift pauses while it runs",
    )


try:
    import pytest as _pytest
except ImportError:  # pragma: no cover - conftest is only loaded by pytest
    _pytest = None

if _pytest is not None:
    @_pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(item, nextitem):
        paused = item.get_closest_marker("real_clock") is not None
        previous = _CLOCK_DRIFT_PAUSED[0]
        _CLOCK_DRIFT_PAUSED[0] = previous or paused
        try:
            yield
        finally:
            _CLOCK_DRIFT_PAUSED[0] = previous
