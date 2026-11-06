"""One-shot local-model coding worker for small experiments."""

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
_MAX_FILES = 8
_MAX_FILE_BYTES = 16 * 1024
_MAX_CONTEXT_BYTES = 64 * 1024
_PATH_RE = re.compile(r"(?<![\w/.-])([\w.-]+(?:/[\w.-]+)+\.[A-Za-z0-9]+|[\w.-]+\.[A-Za-z0-9]+)")


def _named_file_context(task: str, working_dir: str) -> str:
    root = Path(working_dir).resolve()
    chunks: list[str] = []
    total = 0
    seen: set[Path] = set()
    for match in _PATH_RE.finditer(task):
        candidate = (root / match.group(1)).resolve()
        if candidate in seen or root not in candidate.parents or not candidate.is_file():
            continue
        seen.add(candidate)
        data = candidate.read_bytes()[:_MAX_FILE_BYTES]
        text = data.decode("utf-8", errors="replace")
        chunk = f"\n--- {candidate.relative_to(root)} ---\n{text}"
        encoded = chunk.encode()
        if total + len(encoded) > _MAX_CONTEXT_BYTES:
            break
        chunks.append(chunk)
        total += len(encoded)
        if len(chunks) >= _MAX_FILES:
            break
    return "".join(chunks)


def _extract_diff(text: str) -> str:
    fenced = re.search(r"```(?:diff)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    starts = [p for p in (candidate.find("diff --git "), candidate.find("--- a/")) if p >= 0]
    return candidate[min(starts):].strip() + "\n" if starts else ""


class LocalCodingWorker:
    provider = PROVIDER

    def __init__(self, *, client: LlamaCppClient | None = None, max_tokens: int = 4096, **_: Any) -> None:
        self.client = client or LlamaCppClient()
        self.max_tokens = max_tokens

    def run(self, *, task: str, working_dir: str, sandbox: str,
            timeout_seconds: float = 300, **_: Any) -> WorkerResult:
        started = time.monotonic()
        try:
            validate_worker_config(task, working_dir, sandbox)
            context = _named_file_context(task, working_dir)
            prompt = (
                f"Goal and Nexus handover:\n{task}\n\n"
                f"Contents of files explicitly named in that request (bounded):{context or ' none'}\n\n"
                "Return only one unified git diff. Do not use Markdown prose or fences."
            )
            output = asyncio.run(self.client.complete(
                prompt,
                system="You are a coding worker. Produce a minimal correct unified diff.",
                temperature=0.0,
                max_tokens=self.max_tokens,
            ))
            patch = _extract_diff(output)
            if not patch:
                raise RuntimeError("local model did not return a unified diff")
            check = subprocess.run(
                ["git", "apply", "--check", "-"], cwd=working_dir, input=patch,
                text=True, capture_output=True, timeout=timeout_seconds,
            )
            if check.returncode != 0:
                reason = check.stderr.strip() or check.stdout.strip() or "unknown git apply error"
                raise RuntimeError(f"local model patch does not apply: {reason}")
            applied = subprocess.run(
                ["git", "apply", "-"], cwd=working_dir, input=patch,
                text=True, capture_output=True, timeout=timeout_seconds,
            )
            if applied.returncode != 0:
                reason = applied.stderr.strip() or applied.stdout.strip() or "unknown git apply error"
                raise RuntimeError(f"local model patch failed to apply: {reason}")
            return self._result(task, working_dir, output, None, started, success=True, exit_code=0)
        except subprocess.TimeoutExpired:
            return self._result(task, working_dir, "", "local model patch application timed out", started,
                                success=False, exit_code=None, timed_out=True)
        except Exception as exc:  # worker failures are represented, never leaked into the pipeline
            return self._result(task, working_dir, "", str(exc), started, success=False, exit_code=1)

    @staticmethod
    def _result(task: str, working_dir: str, stdout: str, error: str | None,
                started: float, *, success: bool, exit_code: int | None,
                timed_out: bool = False) -> WorkerResult:
        return WorkerResult(
            success=success, exit_code=exit_code, stdout=stdout, stderr="",
            runtime_ms=int((time.monotonic() - started) * 1000), timed_out=timed_out,
            stdout_truncated=False, stderr_truncated=False, thread_id=None,
            final_message=stdout or None, usage=None, task=task, working_dir=working_dir,
            sandbox="workspace-write", error=error, provider=PROVIDER,
        )
