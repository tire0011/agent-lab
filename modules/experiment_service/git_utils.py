from __future__ import annotations

import subprocess
import time

from modules.experiment_logger import ExperimentLogger


# ---------------------------------------------------------------------------
# Git helpers (fixed argv lists only, never shell=True)
# ---------------------------------------------------------------------------


def _run_git(
    args: list[str],
    cwd: str,
    timeout: int = 60,
    env: dict[str, str] | None = None,
    elog: ExperimentLogger | None = None,
    phase: str = "verification",
) -> subprocess.CompletedProcess:
    command = ["git", *args]
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        if elog is not None:
            elog.log_process(
                phase, "git_command_timeout", f"git {' '.join(args)} timed out",
                command=command, cwd=cwd,
                duration_ms=int((time.perf_counter() - started) * 1000),
                timed_out=True, level="ERROR",
            )
        raise
    if elog is not None:
        elog.log_process(
            phase, "git_command", f"git {' '.join(args)}",
            command=command, cwd=cwd,
            duration_ms=int((time.perf_counter() - started) * 1000),
            exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr,
        )
    return proc

