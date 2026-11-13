from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "fixtures" / "clock_drift_samples.py"


def _run_sample(test_name: str, drift_ms: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if drift_ms is None:
        env.pop("NEXUS_TEST_CLOCK_DRIFT_MS", None)
    else:
        env["NEXUS_TEST_CLOCK_DRIFT_MS"] = drift_ms
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"{SAMPLES}::{test_name}"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_process_clock_budget_exposes_drift():
    assert _run_sample("test_elapsed_budget_uses_process_clock", None).returncode == 0
    drifted = _run_sample("test_elapsed_budget_uses_process_clock", "50")
    assert drifted.returncode == 1, drifted.stdout + drifted.stderr


def test_injected_clock_is_unaffected_by_drift():
    assert _run_sample("test_elapsed_budget_with_injected_clock", None).returncode == 0
    drifted = _run_sample("test_elapsed_budget_with_injected_clock", "50")
    assert drifted.returncode == 0, drifted.stdout + drifted.stderr


def test_drift_wrapping_is_undone_after_each_test():
    env = os.environ.copy()
    env["NEXUS_TEST_CLOCK_DRIFT_MS"] = "50"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"{SAMPLES}::test_1_remember_drifted_clock",
            f"{SAMPLES}::test_2_drift_patch_was_undone",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_without_configuration_clocks_are_original(real_clock):
    assert time.monotonic is real_clock.monotonic
    assert time.perf_counter is real_clock.perf_counter
    assert time.time is real_clock.time


def test_real_clock_opts_out_of_configured_drift(real_clock, monkeypatch):
    # The autouse fixture decides whether to patch before this test body runs;
    # changing the environment here also proves the originals remain installed.
    monkeypatch.setenv("NEXUS_TEST_CLOCK_DRIFT_MS", "50")
    assert time.monotonic is real_clock.monotonic
    assert time.perf_counter is real_clock.perf_counter
    assert time.time is real_clock.time
