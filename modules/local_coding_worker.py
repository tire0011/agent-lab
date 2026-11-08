"""One-shot local-model coding worker for small experiments. The prompt is fitted
into the model's context window (#569): the window is read from the server per run
(``GET /props``; fallback ``LOCAL_MODEL_CONTEXT`` then 8192) and the prompt measured
with the server's tokenizer (``POST /tokenize``; fallback a safe bytes-per-token
estimate). Room is filled by priority: instructions+goal (never cut), then the
named files, then the handover cut from the end. When the first two do not fit, the
model is not called: the run ends ``context_too_small`` and the experiment hands
the task to an external agent.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from modules.coding_worker import WorkerResult, validate_worker_config
from modules.experiment_handover import HANDOVER_HEADING
from modules.llama_cpp_client import LlamaCppClient

logger = logging.getLogger(__name__)

PROVIDER = "local"
# Error prefix the experiment pipeline recognises to hand over at once.
CONTEXT_TOO_SMALL = "context_too_small"
DEFAULT_CONTEXT_TOKENS = 8192
# Head-room for the chat template and tokenizer differences between the
# separately counted sections and the rendered request.
CONTEXT_MARGIN_TOKENS = 256
# /tokenize fallback: code/English average ~4 bytes per token; 3 over-counts on
# purpose so an estimate never overflows the window.
SAFE_BYTES_PER_TOKEN = 3
_MAX_FILES = 8
_MAX_FILE_BYTES = 16 * 1024
_SYSTEM_PROMPT = "You are a coding worker. Produce a minimal correct unified diff."
_CLOSING = "Return only one unified git diff. Do not use Markdown prose or fences."
_CUT_MARKER = "\n...[handover cut to fit the local model's context]"
# Where the handover ends inside the experiment prompt templates
# (modules/experiment_service/prompts.py).
_HANDOVER_END_MARKERS = ("\n\nVerification feedback (bounded):\n", "\n\nInstructions:\n1. ")
# Excerpt levels for named files, from whole file to the essential minimum:
# (lines of context around each relevant line, head lines when none matches).
_FILE_LEVELS: tuple[tuple[int | None, int], ...] = ((None, 0), (40, 120), (15, 60), (5, 25))
_PATH_RE = re.compile(r"(?<![\w/.-])([\w.-]+(?:/[\w.-]+)+\.[A-Za-z0-9]+|[\w.-]+\.[A-Za-z0-9]+)")
_BACKTICK_RE = re.compile(r"`([^`\n]{2,80})`")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


class ContextTooSmallError(RuntimeError):
    """The instructions, the goal and the named files do not fit the context."""


# Splitting the task and reading the named files

def split_handover(task: str) -> tuple[str, str]:
    """``(task without handover, handover)``; the handover is the cuttable part."""
    start = task.find(HANDOVER_HEADING)
    if start < 0:
        return task, ""
    ends = [pos for pos in (task.rfind(m) for m in _HANDOVER_END_MARKERS) if pos > start]
    end = min(ends) if ends else len(task)
    handover = task[start:end].strip()
    essential = task[:start].rstrip()
    if end < len(task):
        essential += "\n\n" + task[end:].lstrip("\n")
    return essential, handover


def _named_files(text: str, working_dir: str) -> list[tuple[str, str]]:
    root = Path(working_dir).resolve()
    files: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for match in _PATH_RE.finditer(text):
        candidate = (root / match.group(1)).resolve()
        if candidate in seen or root not in candidate.parents or not candidate.is_file():
            continue
        seen.add(candidate)
        content = candidate.read_bytes().decode("utf-8", errors="replace")
        files.append((str(candidate.relative_to(root)), content))
        if len(files) >= _MAX_FILES:
            break
    return files


def _keywords(goal: str) -> set[str]:
    """Code-like names in the goal: backticked terms and identifiers with ``_``/case/digits."""
    words: set[str] = set()
    for quoted in _BACKTICK_RE.findall(goal):
        words.update(w for w in _IDENT_RE.findall(quoted) if len(w) >= 3)
    for word in _IDENT_RE.findall(goal):
        mixed_case = word[1:] != word[1:].lower() and word != word.upper()
        if "_" in word or any(c.isdigit() for c in word) or mixed_case:
            words.add(word)
    return words


def _excerpt(content: str, keywords: set[str], radius: int | None, head: int) -> str:
    """The relevant part of a file: lines naming a goal keyword, with context."""
    if radius is None:
        if len(content.encode()) <= _MAX_FILE_BYTES:
            return content
        radius, head = _FILE_LEVELS[1]
    lines = content.splitlines()
    hits = [i for i, line in enumerate(lines) if any(k in line for k in keywords)]
    if not hits:
        kept = lines[:head]
        suffix = f"\n...[{len(lines) - head} more lines]" if len(lines) > head else ""
        return "\n".join(kept) + suffix
    ranges: list[list[int]] = []
    for i in hits:
        lo, hi = max(0, i - radius), min(len(lines), i + radius + 1)
        if ranges and lo <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])
    parts: list[str] = []
    for lo, hi in ranges:
        parts.append(f"...[lines {lo + 1}-{hi}]")
        parts.extend(lines[lo:hi])
    if ranges[-1][1] < len(lines):
        parts.append("...[end of excerpt]")
    return "\n".join(parts)


def _render_files(files: list[tuple[str, str]], keywords: set[str], level: tuple[int | None, int]) -> str:
    return "".join(f"\n--- {path} ---\n{_excerpt(content, keywords, *level)}" for path, content in files)


def _render_prompt(essential: str, files_text: str, handover: str) -> str:
    prompt = (
        f"Goal and instructions:\n{essential}\n\n"
        f"Contents of files named in the goal (relevant parts):{files_text or ' none'}\n\n"
    )
    if handover:
        prompt += f"{handover}\n\n"
    return prompt + _CLOSING


def _extract_diff(text: str) -> str:
    fenced = re.search(r"```(?:diff)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    starts = [p for p in (candidate.find("diff --git "), candidate.find("--- a/")) if p >= 0]
    return candidate[min(starts):].strip() + "\n" if starts else ""


# Context size and token counting

def _env_context() -> int | None:
    try:
        value = int(os.environ.get("LOCAL_MODEL_CONTEXT", ""))
    except ValueError:
        return None
    return value if value > 0 else None


async def discover_context_size(client: Any) -> tuple[int, str]:
    """``(n_ctx, source)`` from the server, ``LOCAL_MODEL_CONTEXT`` or the default."""
    probe = getattr(client, "context_size", None)
    if callable(probe):
        try:
            value = int(await probe())
            if value > 0:
                return value, "server"
        except Exception as exc:  # any failure means: use the configured fallback
            logger.info("llama-server /props unavailable (%s); using fallback context size", exc)
    env = _env_context()
    if env is not None:
        return env, "LOCAL_MODEL_CONTEXT"
    return DEFAULT_CONTEXT_TOKENS, "default"


class TokenCounter:
    """Counts with the server's ``/tokenize``; falls back to a safe estimate for the run."""

    def __init__(self, client: Any) -> None:
        self._exact: Callable[[str], Awaitable[int]] | None = getattr(client, "count_tokens", None)
        if not callable(self._exact):
            self._exact = None
        self.source = "tokenize" if self._exact else "estimate"

    async def __call__(self, text: str) -> int:
        if self._exact is not None:
            try:
                return int(await self._exact(text))
            except Exception as exc:
                logger.info("llama-server /tokenize unavailable (%s); estimating tokens", exc)
                self._exact = None
                self.source = "estimate"
        return math.ceil(len(text.encode("utf-8")) / SAFE_BYTES_PER_TOKEN)


@dataclass
class PromptPlan:
    prompt: str
    n_ctx: int
    n_ctx_source: str
    budget_tokens: int
    prompt_tokens: int
    token_source: str
    named_files: list[str] = field(default_factory=list)
    file_level: int = 0
    handover_tokens_kept: int = 0
    handover_cut: bool = False

    def log_fields(self) -> dict[str, Any]:
        return {
            "n_ctx": self.n_ctx, "n_ctx_source": self.n_ctx_source,
            "budget_tokens": self.budget_tokens, "prompt_tokens": self.prompt_tokens,
            "token_source": self.token_source, "named_files": self.named_files,
            "file_level": self.file_level, "handover_tokens_kept": self.handover_tokens_kept,
            "handover_cut": self.handover_cut,
        }


async def _cut_to(text: str, limit: int, count: TokenCounter) -> str:
    """The longest prefix of *text* (plus a cut marker) within *limit* tokens."""
    if limit <= 0 or not text:
        return ""
    tokens = await count(text)
    if tokens <= limit:
        return text
    keep = len(text)
    for _ in range(12):
        keep = int(keep * min(0.95, limit / max(tokens, 1) * 0.95))
        if keep <= 0:
            return ""
        candidate = text[:keep].rstrip() + _CUT_MARKER
        tokens = await count(candidate)
        if tokens <= limit:
            return candidate
    return ""


async def build_prompt(task: str, working_dir: str, client: Any, *, max_output_tokens: int,
                       margin_tokens: int = CONTEXT_MARGIN_TOKENS) -> PromptPlan:
    """Fit the local prompt into the server's context, or raise :class:`ContextTooSmallError`."""
    n_ctx, n_ctx_source = await discover_context_size(client)
    count = TokenCounter(client)
    budget = n_ctx - max_output_tokens - margin_tokens
    essential, handover = split_handover(task)
    files = _named_files(essential, working_dir)
    keywords = _keywords(essential)
    names = [path for path, _ in files]

    async def total(prompt: str) -> int:
        return await count(_SYSTEM_PROMPT) + await count(prompt)

    def too_small(detail: str, tokens: int) -> ContextTooSmallError:
        return ContextTooSmallError(
            f"{CONTEXT_TOO_SMALL}: {detail} need {tokens} tokens, but n_ctx {n_ctx} ({n_ctx_source}) "
            f"leaves {budget} for the prompt after {max_output_tokens} output and {margin_tokens} "
            "margin tokens; the local model was not called"
        )

    # 1. Instructions and goal - never cut.
    base_tokens = await total(_render_prompt(essential, "", ""))
    if base_tokens > budget:
        raise too_small("the instructions and the goal", base_tokens)

    # 2. Named files, from whole to the essential excerpt.
    files_text = ""
    level_used = 0
    if files:
        for level_used, level in enumerate(_FILE_LEVELS):
            files_text = _render_files(files, keywords, level)
            with_files = await total(_render_prompt(essential, files_text, ""))
            if with_files <= budget:
                break
        else:
            raise too_small(
                f"the instructions, the goal and the relevant parts of {', '.join(names)}",
                with_files,
            )

    # 3. The handover, cut from the end, re-checked on the rendered prompt.
    kept = ""
    if handover:
        used = await total(_render_prompt(essential, files_text, ""))
        limit = budget - used - 8  # the two blank lines joining it
        for _ in range(6):
            kept = await _cut_to(handover, limit, count)
            if not kept or await total(_render_prompt(essential, files_text, kept)) <= budget:
                break
            limit -= max(16, limit // 10)
        else:
            kept = ""

    prompt = _render_prompt(essential, files_text, kept)
    prompt_tokens = await total(prompt)
    if prompt_tokens > budget:  # only reachable if the counter is not additive at all
        raise too_small("the rendered prompt", prompt_tokens)
    return PromptPlan(
        prompt=prompt, n_ctx=n_ctx, n_ctx_source=n_ctx_source, budget_tokens=budget,
        prompt_tokens=prompt_tokens, token_source=count.source, named_files=names,
        file_level=level_used, handover_tokens_kept=await count(kept) if kept else 0,
        handover_cut=bool(handover) and kept != handover,
    )


class LocalCodingWorker:
    provider = PROVIDER

    def __init__(self, *, client: LlamaCppClient | None = None, max_tokens: int = 4096, **_: Any) -> None:
        self.client = client or LlamaCppClient()
        self.max_tokens = max_tokens
        self.last_plan: PromptPlan | None = None

    def run(self, *, task: str, working_dir: str, sandbox: str,
            timeout_seconds: float = 300, experiment_logger: Any | None = None,
            phase: str = "implementation", **_: Any) -> WorkerResult:
        started = time.monotonic()
        self.last_plan = None
        try:
            validate_worker_config(task, working_dir, sandbox)
            try:
                # n_ctx is read once here and holds for this whole run.
                plan = asyncio.run(build_prompt(task, working_dir, self.client,
                                                max_output_tokens=self.max_tokens))
            except ContextTooSmallError as exc:
                self._log(experiment_logger, phase, "local_context_too_small", str(exc), warning=True)
                return self._result(task, working_dir, "", str(exc), started, success=False, exit_code=1)
            self.last_plan = plan
            self._log(
                experiment_logger, phase, "local_prompt_budget",
                f"Local prompt: {plan.prompt_tokens} tokens of n_ctx {plan.n_ctx} "
                f"({plan.n_ctx_source}, counted by {plan.token_source})",
                **plan.log_fields(),
            )
            output = asyncio.run(self.client.complete(
                plan.prompt,
                system=_SYSTEM_PROMPT,
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
    def _log(experiment_logger: Any | None, phase: str, event: str, message: str,
             *, warning: bool = False, **fields: Any) -> None:
        (logger.warning if warning else logger.info)("%s: %s", event, message)
        if experiment_logger is None:
            return
        method = getattr(experiment_logger, "warning" if warning else "info", None)
        if callable(method):
            try:
                method(phase, event, message, **fields)
            except Exception:  # logging must never fail the run
                logger.debug("experiment logger rejected %s", event, exc_info=True)

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


def is_context_too_small(error: str | None) -> bool:
    return bool(error) and str(error).startswith(CONTEXT_TOO_SMALL)
