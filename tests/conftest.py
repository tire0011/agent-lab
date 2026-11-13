"""Shared test setup.

This file runs before any test module is imported, which is the only point at
which these can still take effect: both settings are read into module-level
constants and bound as default arguments at import time.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


# Capture these before any test can replace them.  Besides being the clocks
# exposed by ``real_clock``, their identities make the no-drift path a genuine
# no-op (important for tests which patch clocks themselves).
_REAL_CLOCK = SimpleNamespace(
    monotonic=time.monotonic,
    perf_counter=time.perf_counter,
    time=time.time,
)


@pytest.fixture
def real_clock():
    """Return the process clocks and opt this test out of clock drift."""

    return _REAL_CLOCK


@pytest.fixture(autouse=True)
def _clock_drift(request, monkeypatch):
    """Advance each wall-clock read by the configured amount per call."""

    configured_ms = os.environ.get("NEXUS_TEST_CLOCK_DRIFT_MS")
    if configured_ms is None or "real_clock" in request.fixturenames:
        yield
        return

    drift_seconds = float(configured_ms) / 1000.0

    def make_drifting_clock(original):
        calls = 0

        def drifting_clock():
            nonlocal calls
            calls += 1
            return original() + calls * drift_seconds

        return drifting_clock

    for name in ("monotonic", "perf_counter", "time"):
        original = getattr(_REAL_CLOCK, name)
        monkeypatch.setattr(time, name, make_drifting_clock(original))

    yield

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
