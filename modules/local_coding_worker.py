"""One-shot local-model coding worker for small, bounded changes."""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from modules.coding_worker import WorkerResult, validate_worker_config
from modules.llama_cpp_client import LlamaCppClient

PROVIDER = "local"
_MAX_FILES = 2
_MAX_FILE_BYTES = 48 * 1024
_PATH_RE = re.compile(r"(?<![\w./-])(?:`)?([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9_-]+)(?:`)?")


def _named_files(task: str, working_dir: Path) -> list[Path]:
    """Return a bounded set of repository-contained file paths named in the task."""
    root = working_dir.resolve()
    found: list[Path] = []
    for match in _PATH_RE.finditer(task):
        candidate = (root / match.group(1)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if (not candidate.exists() or candidate.is_file()) and candidate not in found:
            found.append(candidate)
        if len(found) >= _MAX_FILES:
            break
    return found


def _extract_diff(output: str) -> str:
    text = output.strip()
    fenced = re.search(r"```(?:diff)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("diff --git ")
    if start < 0:
        start = text.find("--- ")
    return text[start:].strip() if start >= 0 else ""


def _patched_files(patch: str) -> set[str]:
    return {
        match.group(1)
        for match in re.finditer(r"^diff --git a/(.+?) b/(.+?)$", patch, re.MULTILINE)
        if match.group(1) == match.group(2)
    }


class LocalCodingWorker:
    """Ask the local model for exactly one unified diff, then apply it."""

    provider = PROVIDER

    def __init__(self, *, client: Any | None = None, max_tokens: int = 4096, **_: Any) -> None:
        self.client = client or LlamaCppClient()
        self.max_tokens = max_tokens

    def run(
        self,
        *,
        task: str,
        working_dir: str,
        sandbox: str,
        timeout_seconds: float | None = None,
        **_: Any,
    ) -> WorkerResult:
        started = time.monotonic()
        try:
            validate_worker_config(task, working_dir, sandbox)
            root = Path(working_dir).resolve()
            files = _named_files(task, root)
            if not files:
                raise ValueError("local worker found no repository files named in the task")
            context: list[str] = []
            for path in files:
                data = path.read_bytes()[:_MAX_FILE_BYTES] if path.exists() else b"[new file]"
                context.append(
                    f"FILE {path.relative_to(root)}:\n{data.decode('utf-8', errors='replace')}"
                )
            prompt = (
                "Implement the small repository change below. Return only a unified diff "
                "that applies from the repository root. Do not add prose or Markdown fences. "
                "Only modify the files supplied below.\n\nGOAL:\n"
                + task
                + "\n\nFILES:\n"
                + "\n\n".join(context)
            )
            output = asyncio.run(
                self.client.complete(
                    prompt=prompt,
                    system="You are a precise coding worker that emits one valid unified diff.",
                    temperature=0.0,
                    max_tokens=self.max_tokens,
                )
            )
            patch = _extract_diff(output)
            if not patch:
                raise ValueError("local model did not return a unified diff")
            allowed = {str(path.relative_to(root)) for path in files}
            patched = _patched_files(patch)
            if not patched or not patched.issubset(allowed):
                raise ValueError("local model patch modifies files outside the bounded input set")
            proc = subprocess.run(
                ["git", "apply", "--check", "-"],
                cwd=root,
                input=patch,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip()[:1000]
                raise ValueError(f"local model patch does not apply: {detail}")
            proc = subprocess.run(
                ["git", "apply", "-"], cwd=root, input=patch, text=True,
                capture_output=True, timeout=60, check=False,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip()[:1000]
                raise ValueError(f"local model patch could not be applied: {detail}")
            return self._result(True, task, str(root), sandbox, started, stdout=patch)
        except Exception as exc:  # one failed patch is an ordinary failed worker run
            return self._result(False, task, working_dir, sandbox, started, error=str(exc))

    @staticmethod
    def _result(
        success: bool, task: str, working_dir: str, sandbox: str, started: float,
        *, stdout: str = "", error: str | None = None,
    ) -> WorkerResult:
        return WorkerResult(
            success=success, exit_code=0 if success else 1, stdout=stdout, stderr="",
            runtime_ms=int((time.monotonic() - started) * 1000), timed_out=False,
            stdout_truncated=False, stderr_truncated=False, thread_id=None,
            final_message=None, usage=None, task=task, working_dir=working_dir,
            sandbox=sandbox, error=error, provider=PROVIDER,
        )
