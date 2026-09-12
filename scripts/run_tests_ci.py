"""Run the repository's test suite for CI, with bounded failure output.

This used to run ``unittest discover``, which only collects tests inside
``unittest.TestCase`` subclasses. Six test files are written as plain pytest
functions, so 120 tests - covering the multi-provider orchestrator, the local
llama client and router, adaptive routing, the prompt library and the
maintenance service - were collected zero times and CI reported green for
files it had never executed. Since adoption treats a green CI run as
authoritative evidence, that hole sat directly under the lifecycle's main
gate. See DECISIONS.md D-018.

pytest runs both styles, so nothing that ran before stops running.
"""

from __future__ import annotations

import subprocess
import sys


TEST_COMMAND = [
    sys.executable, "-m", "pytest",
    "-q",
    "--tb=short",
    # One line per failure at the end, so a long log stays diagnosable.
    "-rf",
]

_MAX_OUTPUT_CHARS = 24000


def main() -> int:
    proc = subprocess.run(
        TEST_COMMAND,
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

    # On failure the tail holds the short test summary and the tracebacks that
    # precede it, which is what a reader actually needs.
    if len(output) > _MAX_OUTPUT_CHARS:
        print(f"... output truncated to the last {_MAX_OUTPUT_CHARS} characters ...")
        output = output[-_MAX_OUTPUT_CHARS:]
    print(output)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
