import asyncio
import os
import shlex
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodingToolResult:
    tool: str
    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    runtime_ms: int
    timed_out: bool = False


class CodingTool(ABC):
    name: str

    def __init__(self, executable: str, timeout_seconds: int = 600):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    @abstractmethod
    def build_args(self, task: str, working_dir: Path) -> list[str]:
        raise NotImplementedError

    async def run(self, task: str, working_dir: str | Path) -> CodingToolResult:
        task = task.strip()
        if not task:
            raise ValueError("Task cannot be empty")

        cwd = self._validate_working_dir(working_dir)
        args = self.build_args(task, cwd)
        started = time.perf_counter()

        process = await asyncio.create_subprocess_exec(
            self.executable,
            *args,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
            timed_out = False
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            timed_out = True

        runtime_ms = int(round((time.perf_counter() - started) * 1000))
        exit_code = process.returncode

        return CodingToolResult(
            tool=self.name,
            success=not timed_out and exit_code == 0,
            exit_code=exit_code,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            runtime_ms=runtime_ms,
            timed_out=timed_out,
        )

    @staticmethod
    def extra_args(env_name: str) -> list[str]:
        value = os.getenv(env_name, "").strip()
        return shlex.split(value) if value else []

    @staticmethod
    def _validate_working_dir(working_dir: str | Path) -> Path:
        cwd = Path(working_dir).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError(f"Working directory does not exist: {cwd}")

        root = Path(os.getenv("CODING_TOOL_ROOT", os.getenv("AGENT_FS_ROOT", "/agent")))
        root = root.expanduser().resolve()
        try:
            cwd.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Working directory must stay under {root}") from exc

        return cwd
