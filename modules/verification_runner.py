from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Sequence

from modules.verification_env import build_verification_test_env


@dataclass(slots=True)
class VerificationRunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    exception: BaseException | None
    duration_ms: int

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


def run_verification_test(command: Sequence[str], *, cwd: str, timeout_seconds: int) -> VerificationRunResult:
    """Shared verification subprocess runner for experiment and adoption."""
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
