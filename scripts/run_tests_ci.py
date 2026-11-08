"""Run the repository test suite for CI, with bounded failure output. This used to run ``unittest
discover``, which only collects tests in ``unittest.TestCase`` subclasses, so files written as plain pytest
functions contributed zero tests while CI reported green - a hole directly under the gate adoption treats
as authoritative evidence (DECISIONS.md D-018). pytest runs both styles, so nothing that ran before stops
running."""

from __future__ import annotations

import os
import subprocess
import sys


_MAX_OUTPUT_CHARS = 24000

# How many pytest processes to run the suite across. The suite is dominated by spawning and waiting,
# not computation (146 git invocations across 32 test files plus service threads), so workers help past
# the core count (measured on a 4-core box: 228s serial, 119s at 2, 73s at 4). "auto" is one worker per
# core; lower it - or set 1 - where memory is the binding constraint, since each worker is a full
# interpreter with the project imported and more workers on a swapping host make it slower, not faster; 1
# disables xdist and runs in this process, which is also what you want when bisecting (single ordered output).
_WORKERS_ENV_VAR = "NEXUS_TEST_WORKERS"
_DEFAULT_WORKERS = "auto"


def _workers() -> str:
    return (os.environ.get(_WORKERS_ENV_VAR) or _DEFAULT_WORKERS).strip() or _DEFAULT_WORKERS


def _test_command() -> list[str]:
    command = [
        sys.executable, "-m", "pytest",
        "-q",
        "--tb=short",
        # One line per failure at the end, so a long log stays diagnosable.
        "-rf",
    ]
    workers = _workers()
    if workers not in {"1", "0", "serial", "off"}:
        command += ["-n", workers]
        # Keep every test in a file on one worker: test modules carry real import-time state (os.environ.setdefault
        # calls, module-level clients, api.py imported once), and splitting a file across processes would make which
        # worker ran a test part of its setup. Measured the same speed as per-test distribution (72.5s vs 72.9s), so
        # the isolation is free.
        command += ["--dist", "loadfile"]
    return command


def main() -> int:
    command = _test_command()
    proc = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = proc.stdout or ""

    if proc.returncode == 0:
        # Quiet on success: the last line carries the counts.
        print(output.strip().splitlines()[-1] if output.strip() else "OK")
        return 0

    # On failure the tail holds the short test summary and the tracebacks that precede it, which is what a
    # reader actually needs.
    if len(output) > _MAX_OUTPUT_CHARS:
        print(f"... output truncated to the last {_MAX_OUTPUT_CHARS} characters ...")
        output = output[-_MAX_OUTPUT_CHARS:]
    print(output)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
