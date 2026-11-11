"""Local-model coding worker for small experiments. The prompt is fitted
into the model's context window (#569): the window is read from the server per run
(``GET /props``; fallback ``LOCAL_MODEL_CONTEXT`` then 8192) and the prompt measured
with the server's tokenizer (``POST /tokenize``; fallback a safe bytes-per-token
estimate). Room is filled by priority: instructions+goal (never cut), then the
named files, then the handover cut from the end. When the first two do not fit, the
model is not called: the run ends ``context_too_small`` and the experiment hands
the task to an external agent.

Correction loop (#597): each answer must be a unified diff that passes
``git apply --check``, applies, and leaves every changed ``.py`` file passing
``python -m py_compile``. On a failure the round's changes are rolled back to the
pre-round working tree (pre-existing changes kept, new files removed) and the model
is asked again with the same fitted prompt plus a bounded feedback section: the
last error (up to ~1500 chars) and the previous diff, cut to fit. Room for it is
made by cutting the handover; the request still fits the run's ``n_ctx``. Feedback
is never accumulated: only the last round's is sent. After ``MAX_PATCH_ROUNDS``
failed rounds the run fails with the last reason and the number of rounds. Each
round is logged as ``local_patch_round``.

Subtasks (#598, ``split_tasks=True``; the production factory sets it from
``LOCAL_TASK_SPLIT``, default on): before patching, the local model is asked for
an ordered JSON plan of small subtasks (each one or two files, optional named
tests). If its plan is unusable, exactly one free-tier registry model is asked
once (:func:`free_tier_plan`); a paid model is never called and a failed call is
not retried elsewhere. Without a usable plan of at least two subtasks the task
runs one-shot as before. Subtasks run in order on the same working tree, each
through the correction loop with its own prompt (its files, the diff of the
earlier subtasks, the overall goal as cuttable context); a patch may only touch
the subtask's one or two declared files, and the named tests must pass. Named
tests are verification only: a patch may not edit them unless the subtask also
declares them in ``files`` (so a model cannot weaken its own checks). The first
failing subtask stops the run: the result fails and names it, earlier subtasks'
changes stay in the tree but never count as success. Every run logs
``local_task_split`` (when planned) and ``local_task_split_result`` (split,
subtasks, subtasks_succeeded, failed_subtask, success).
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from modules.coding_worker import WorkerResult, validate_worker_config
from modules.experiment_handover import HANDOVER_HEADING
from modules.llama_cpp_client import LlamaCppClient
from modules.local_model_rates import LocalModelRates
from modules.local_test_first import generate_failing_tests

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
_DIFF_CUT_MARKER = "\n...[previous diff cut to fit the local model's context]"
_ERROR_CUT_MARKER = "\n...[error cut]"
MAX_PATCH_ROUNDS = 3
# #642: a local call should finish within this share of the client timeout.
TIME_BUDGET_FRACTION = 0.8
# At most this share of that time is reserved for generation, so a slow
# generation rate or a large max_tokens can never leave the prompt nothing.
MAX_GENERATION_TIME_SHARE = 0.5
# Never ask a time-fitted call for fewer output tokens than this.
MIN_OUTPUT_TOKENS = 256
FEEDBACK_ERROR_CHARS = 1500
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


def _render_feedback(round_no: int, error: str, previous_diff: str) -> str:
    text = (
        f"Correction (round {round_no} of {MAX_PATCH_ROUNDS}): your previous answer was rejected.\n"
        f"Error:\n{error}\n"
    )
    if previous_diff:
        text += f"\nPrevious diff (fix it, do not repeat the mistake):\n{previous_diff}\n"
    return text


def _render_prompt(essential: str, files_text: str, handover: str, feedback: str = "") -> str:
    prompt = (
        f"Goal and instructions:\n{essential}\n\n"
        f"Contents of files named in the goal (relevant parts):{files_text or ' none'}\n\n"
    )
    if handover:
        prompt += f"{handover}\n\n"
    if feedback:
        prompt += f"{feedback}\n"
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
    time_budget_tokens: int | None = None
    time_budget_limited: bool = False
    time_budget_exceeded: bool = False
    # max_tokens for the model calls on this prompt; the time budget may lower it.
    output_tokens: int = 0
    # Parts of the fitted prompt, kept for the correction rounds (#597).
    essential: str = field(default="", repr=False)
    files_text: str = field(default="", repr=False)
    handover: str = field(default="", repr=False)
    counter: "TokenCounter | None" = field(default=None, repr=False)

    def log_fields(self) -> dict[str, Any]:
        return {
            "n_ctx": self.n_ctx, "n_ctx_source": self.n_ctx_source,
            "budget_tokens": self.budget_tokens, "prompt_tokens": self.prompt_tokens,
            "token_source": self.token_source, "named_files": self.named_files,
            "file_level": self.file_level, "handover_tokens_kept": self.handover_tokens_kept,
            "handover_cut": self.handover_cut,
            "time_budget_tokens": self.time_budget_tokens,
            "time_budget_limited": self.time_budget_limited,
            "time_budget_exceeded": self.time_budget_exceeded,
            "output_tokens": self.output_tokens,
        }


@dataclass(frozen=True)
class TimeBudget:
    """What one local call can afford within ``TIME_BUDGET_FRACTION`` of the timeout."""

    prompt_tokens: int
    output_tokens: int
    seconds: float


def time_budget(timeout: Any, max_output_tokens: int,
                rates: tuple[float | None, float | None]) -> TimeBudget | None:
    """The prompt/output allowance from measured rates; ``None`` without both rates.

    The generation reservation is capped at ``MAX_GENERATION_TIME_SHARE`` of the
    time, and ``output_tokens`` is lowered to what that reservation can produce.
    A slow generation rate therefore shortens the answer instead of zeroing the
    prompt budget (the bug that blocked experiment 689600ce).
    """
    prompt_rate, generation_rate = rates
    if not (isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0):
        return None
    if not all(isinstance(r, (int, float)) and math.isfinite(r) and r > 0
               for r in (prompt_rate, generation_rate)):
        return None
    seconds = float(timeout) * TIME_BUDGET_FRACTION
    generation_seconds = min(max_output_tokens / generation_rate, seconds * MAX_GENERATION_TIME_SHARE)
    output_tokens = min(max_output_tokens,
                        max(MIN_OUTPUT_TOKENS, int(generation_seconds * generation_rate)))
    prompt_tokens = max(0, int((seconds - generation_seconds) * prompt_rate))
    return TimeBudget(prompt_tokens=prompt_tokens, output_tokens=output_tokens, seconds=seconds)


async def _cut_to(text: str, limit: int, count: TokenCounter, marker: str = _CUT_MARKER) -> str:
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
        candidate = text[:keep].rstrip() + marker
        tokens = await count(candidate)
        if tokens <= limit:
            return candidate
    return ""


async def _fit_part(text: str, budget: int, measure: Callable[[str], Awaitable[int]],
                    count: TokenCounter, marker: str = _CUT_MARKER) -> str:
    """The longest prefix of *text* whose rendered prompt (``measure``) stays in *budget*."""
    if not text:
        return ""
    limit = budget - await measure("") - 8  # the blank lines joining it
    for _ in range(6):
        kept = await _cut_to(text, limit, count, marker)
        if not kept or await measure(kept) <= budget:
            return kept
        limit -= max(16, limit // 10)
    return ""


async def build_prompt(task: str, working_dir: str, client: Any, *, max_output_tokens: int,
                       margin_tokens: int = CONTEXT_MARGIN_TOKENS,
                       rates: tuple[float | None, float | None] = (None, None)) -> PromptPlan:
    """Fit the local prompt into the server's context, or raise :class:`ContextTooSmallError`."""
    n_ctx, n_ctx_source = await discover_context_size(client)
    essential, handover = split_handover(task)
    return await build_prompt_parts(
        essential, handover, working_dir, client, max_output_tokens=max_output_tokens,
        margin_tokens=margin_tokens, n_ctx=n_ctx, n_ctx_source=n_ctx_source, rates=rates,
    )


async def build_prompt_parts(essential: str, handover: str, working_dir: str, client: Any, *,
                             max_output_tokens: int, n_ctx: int, n_ctx_source: str,
                             margin_tokens: int = CONTEXT_MARGIN_TOKENS,
                             counter: TokenCounter | None = None,
                             rates: tuple[float | None, float | None] = (None, None)) -> PromptPlan:
    """:func:`build_prompt` for an already split task and an already known ``n_ctx``."""
    count = counter or TokenCounter(client)
    # n_ctx is a hard limit; the time budget (#642) is a soft one. It cuts the
    # handover first, then the files, but never refuses the call: if even the
    # minimal prompt is over it, the minimal prompt is sent anyway
    # (``time_budget_exceeded``). Refusing would stop the worker from ever
    # making the call that measures a better rate - it could never recover.
    context_budget = n_ctx - max_output_tokens - margin_tokens
    allowance = time_budget(getattr(client, "timeout", None), max_output_tokens, rates)
    time_budget_tokens = allowance.prompt_tokens if allowance else None
    output_tokens = allowance.output_tokens if allowance else max_output_tokens
    budget = context_budget if time_budget_tokens is None else min(context_budget, time_budget_tokens)
    exceeded = False
    files = _named_files(essential, working_dir)
    keywords = _keywords(essential)
    names = [path for path, _ in files]

    async def total(prompt: str) -> int:
        return await count(_SYSTEM_PROMPT) + await count(prompt)

    def too_small(detail: str, tokens: int) -> ContextTooSmallError:
        return ContextTooSmallError(
            f"{CONTEXT_TOO_SMALL}: {detail} need {tokens} tokens, but n_ctx {n_ctx} ({n_ctx_source}) "
            f"leaves {context_budget} for the prompt after {max_output_tokens} output and {margin_tokens} "
            "margin tokens; the local model was not called"
        )

    # 1. Instructions and goal - never cut.
    base_tokens = await total(_render_prompt(essential, "", ""))
    if base_tokens > context_budget:
        raise too_small("the instructions and the goal", base_tokens)
    if base_tokens > budget:
        budget, exceeded = base_tokens, True

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
            if with_files > context_budget:
                raise too_small(
                    f"the instructions, the goal and the relevant parts of {', '.join(names)}",
                    with_files,
                )
            budget, exceeded = with_files, True  # smallest file level, over the time budget

    # 3. The handover, cut from the end, re-checked on the rendered prompt.
    kept = await _fit_part(
        handover, budget, lambda h: total(_render_prompt(essential, files_text, h)), count,
    )

    prompt = _render_prompt(essential, files_text, kept)
    prompt_tokens = await total(prompt)
    if prompt_tokens > budget:  # only reachable if the counter is not additive at all
        if prompt_tokens > context_budget:
            raise too_small("the rendered prompt", prompt_tokens)
        exceeded = True
    handover_cut = bool(handover) and kept != handover
    # The full prompt is over the time budget: the time budget cut something
    # (it is the tighter limit) or the minimal prompt is already over it.
    limited = time_budget_tokens is not None and (
        exceeded or (time_budget_tokens < context_budget and (handover_cut or level_used > 0))
    )
    return PromptPlan(
        prompt=prompt, n_ctx=n_ctx, n_ctx_source=n_ctx_source, budget_tokens=budget,
        prompt_tokens=prompt_tokens, token_source=count.source, named_files=names,
        file_level=level_used, handover_tokens_kept=await count(kept) if kept else 0,
        handover_cut=handover_cut,
        time_budget_tokens=time_budget_tokens, time_budget_limited=limited,
        time_budget_exceeded=exceeded, output_tokens=output_tokens,
        essential=essential, files_text=files_text, handover=handover, counter=count,
    )


@dataclass
class FeedbackPrompt:
    prompt: str
    prompt_tokens: int
    error_chars: int
    diff_cut: bool
    handover_tokens_kept: int


async def build_feedback_prompt(plan: PromptPlan, client: Any, *, round_no: int, error: str,
                                previous_diff: str) -> FeedbackPrompt:
    """The fitted prompt plus one bounded feedback section, within ``plan.budget_tokens``.

    Priority: instructions+goal and named files exactly as fitted, then the error
    (at most ``FEEDBACK_ERROR_CHARS``), then the previous diff cut from the end, then
    whatever handover still fits. ``n_ctx`` is not read again.
    """
    count = plan.counter or TokenCounter(client)
    budget = plan.budget_tokens
    system_tokens = await count(_SYSTEM_PROMPT)

    async def total(err: str, diff: str, handover: str) -> int:
        feedback = _render_feedback(round_no, err, diff) if err else ""
        return system_tokens + await count(_render_prompt(plan.essential, plan.files_text, handover, feedback))

    err = error.strip() or "unknown error"
    if len(err) > FEEDBACK_ERROR_CHARS:
        err = err[:FEEDBACK_ERROR_CHARS].rstrip() + _ERROR_CUT_MARKER
    if await total(err, "", "") > budget:
        err = await _fit_part(err, budget, lambda e: total(e, "", ""), count, _ERROR_CUT_MARKER)
    diff = ""
    if err:
        diff = await _fit_part(previous_diff, budget, lambda d: total(err, d, ""), count, _DIFF_CUT_MARKER)
    handover = await _fit_part(plan.handover, budget, lambda h: total(err, diff, h), count)
    tokens = await total(err, diff, handover)
    if tokens > budget:  # counter not additive: fall back to the smallest feedback that fits
        diff, handover = "", ""
        tokens = await total(err, "", "")
        if tokens > budget:
            err = ""
            tokens = await total("", "", "")
    feedback = _render_feedback(round_no, err, diff) if err else ""
    return FeedbackPrompt(
        prompt=_render_prompt(plan.essential, plan.files_text, handover, feedback),
        prompt_tokens=tokens, error_chars=len(err),
        diff_cut=bool(previous_diff) and diff != previous_diff,
        handover_tokens_kept=await count(handover) if handover else 0,
    )


# Splitting a task into small subtasks (#598)

@dataclass(frozen=True)
class Subtask:
    goal: str
    files: tuple[str, ...]
    tests: tuple[str, ...] = ()


class PlanUnusable(ValueError):
    """A planner's answer is not a usable ordered list of small subtasks."""


@dataclass
class SplitOutcome:
    """What was logged as ``local_task_split`` / ``local_task_split_result``."""

    split: bool = False
    planner: str = ""  # "local", "free_tier" or "" (not split)
    planner_model: str = ""
    reason: str = ""
    subtasks: list[Subtask] = field(default_factory=list)
    succeeded: int = 0
    failed_subtask: int | None = None
    free_tier_calls: int = 0

    def log_fields(self) -> dict[str, Any]:
        return {
            "split": self.split, "planner": self.planner, "planner_model": self.planner_model,
            "reason": self.reason, "subtasks": len(self.subtasks),
            "subtasks_succeeded": self.succeeded, "failed_subtask": self.failed_subtask,
            "free_tier_calls": self.free_tier_calls,
        }


ENV_TASK_SPLIT = "LOCAL_TASK_SPLIT"
ENV_TEST_FIRST = "LOCAL_TEST_FIRST"
MAX_SUBTASKS = 6
MAX_SUBTASK_FILES = 2
MAX_SUBTASK_TESTS = 3
PLAN_MAX_TOKENS = 1024
NAMED_TEST_TIMEOUT_SECONDS = 300.0
_PLAN_SYSTEM_PROMPT = "You are a planning worker. Split coding tasks into small ordered steps. Answer only JSON."
_PLAN_INSTRUCTIONS = (
    "Split the coding task below into an ordered list of small subtasks for a small local "
    f"model. At most {MAX_SUBTASKS} subtasks. Each subtask is one small change to one or two "
    "files (repository-relative paths) and must leave the code compiling. A later subtask "
    "sees the changes of the earlier ones. Name test files only if they already exist or an "
    "earlier or the same subtask creates them. Tests are only run, never edited: a "
    "subtask that must change a test file lists it in files.\n"
    "Answer with JSON only, no prose, in exactly this shape:\n"
    '{"subtasks": [{"goal": "what to change", "files": ["path/a.py"], '
    '"tests": ["tests/test_a.py"]}]}'
)
_FREE_TIER_ESSENTIAL_CHARS = 12000
_FREE_TIER_HANDOVER_CHARS = 6000


def task_split_enabled() -> bool:
    """``LOCAL_TASK_SPLIT`` (default on); ``0``/``false``/``off`` runs one-shot."""
    return os.environ.get(ENV_TASK_SPLIT, "1").strip().lower() not in {"0", "false", "no", "off"}


def test_first_enabled() -> bool:
    """``LOCAL_TEST_FIRST`` (default off); explicit truthy values enable it."""
    return os.environ.get(ENV_TEST_FIRST, "").strip().lower() in {"1", "true", "yes", "on"}


def _render_plan_prompt(essential: str, names: list[str], handover: str) -> str:
    prompt = f"{_PLAN_INSTRUCTIONS}\n\nTask:\n{essential}\n\n"
    prompt += f"Files named in the task: {', '.join(names) if names else 'none'}\n\n"
    if handover:
        prompt += f"{handover}\n\n"
    return prompt + "Return only the JSON object."


def _relative_path(value: Any, root: Path, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanUnusable(f"{what} is not a path")
    path = value.strip().replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    if path.startswith("/") or ".." in path.split("/") or _inside(root, path) is None:
        raise PlanUnusable(f"{what} {value!r} is not inside the repository")
    return path


def parse_plan(text: str, working_dir: str) -> list[Subtask]:
    """The ordered subtasks in a planner answer, or raise :class:`PlanUnusable`."""
    import json

    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text or "", re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else (text or "")
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        raise PlanUnusable("no JSON object in the answer")
    try:
        data = json.loads(candidate[start:end + 1])
    except ValueError as exc:
        raise PlanUnusable(f"invalid JSON: {exc}") from exc
    raw = data.get("subtasks") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise PlanUnusable("no subtasks list")
    if len(raw) > MAX_SUBTASKS:
        raise PlanUnusable(f"{len(raw)} subtasks, at most {MAX_SUBTASKS} allowed")
    root = Path(working_dir).resolve()
    subtasks: list[Subtask] = []
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise PlanUnusable(f"subtask {number} is not an object")
        goal = item.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise PlanUnusable(f"subtask {number} has no goal")
        files = item.get("files")
        if isinstance(files, str):
            files = [files]
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_SUBTASK_FILES:
            raise PlanUnusable(f"subtask {number} must name 1 to {MAX_SUBTASK_FILES} files")
        paths = tuple(dict.fromkeys(_relative_path(f, root, f"subtask {number} file") for f in files))
        tests = item.get("tests") or []
        if isinstance(tests, str):
            tests = [tests]
        if not isinstance(tests, list) or len(tests) > MAX_SUBTASK_TESTS:
            raise PlanUnusable(f"subtask {number} names more than {MAX_SUBTASK_TESTS} tests")
        named_tests: list[str] = []
        for test in tests:
            if not isinstance(test, str):
                raise PlanUnusable(f"subtask {number} test is not a path")
            path, _, node = test.strip().partition("::")
            path = _relative_path(path, root, f"subtask {number} test")
            if not path.endswith(".py"):
                raise PlanUnusable(f"subtask {number} test {test!r} is not a .py file")
            named_tests.append(f"{path}::{node}" if node else path)
        subtasks.append(Subtask(goal=goal.strip()[:2000], files=paths, tests=tuple(named_tests)))
    return subtasks


def free_tier_plan(prompt: str, *, models: list[Any] | None = None, transport: Any | None = None) -> Any:
    """Exactly one call to the first available free registry model, never a paid one.

    Returns the :class:`modules.model_registry.ChatResult`, or ``None`` when no free
    model is configured. A failed call is not retried on another model: the codex
    finding on experiment 26c4f98d was a loop over paid providers.
    """
    from modules import model_registry

    pool = models if models is not None else model_registry.load_models()
    free = [m for m in model_registry.candidates(pool, role="planning", max_cost="free") if m.cost == "free"]
    if not free:
        return None
    return model_registry.chat(free[0], prompt, max_tokens=PLAN_MAX_TOKENS, transport=transport)


# Applying, checking and rolling back one round's patch

class RollbackError(RuntimeError):
    """The working tree could not be restored to its pre-round state."""


class _PatchRejected(Exception):
    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason


_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _unquote(path: str) -> str:
    """Undo git's C-style path quoting (``"a/t\\303\\244b"``)."""
    path = path.strip()
    match = _QUOTED_RE.fullmatch(path)
    if not match:
        return path
    raw = match.group(1).encode("latin-1", errors="backslashreplace")
    return codecs.escape_decode(raw)[0].decode("utf-8", errors="replace")


def _strip_prefix(path: str) -> str | None:
    """Strip git apply's default ``-p1`` component; ``None`` for ``/dev/null``."""
    path = _unquote(path)
    if path == "/dev/null" or not path:
        return None
    return path.split("/", 1)[1] if "/" in path else None


def patch_paths(patch: str) -> set[str]:
    """Every path a patch may touch: old and new names, created and deleted files."""
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith(("--- ", "+++ ")):
            target = line[4:]
            if not target.startswith('"'):
                target = target.split("\t", 1)[0]
            found = _strip_prefix(target)
            if found:
                paths.add(found)
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            paths.add(_unquote(line.split(" ", 2)[2]))
        elif line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            quoted = _QUOTED_RE.findall(rest)
            if len(quoted) == 2:
                candidates = [f'"{q}"' for q in quoted]
            else:
                match = re.match(r"^(a/.+?) (b/.+)$", rest)
                candidates = list(match.groups()) if match else []
            for candidate in candidates:
                found = _strip_prefix(candidate)
                if found:
                    paths.add(found)
    return paths


def _inside(root: Path, rel: str) -> Path | None:
    full = (root / rel)
    try:
        resolved = full.parent.resolve() / full.name
    except OSError:
        return None
    return full if root == resolved or root in resolved.parents else None


@dataclass
class _Snapshot:
    """The pre-round state of the paths a patch touches."""

    root: Path
    files: dict[str, tuple[str, bytes, int] | None]
    new_dirs: list[Path]
    status: str


def _git_status(working_dir: str, timeout: float) -> str:
    done = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=working_dir, capture_output=True, timeout=timeout,
    )
    if done.returncode != 0:
        raise RollbackError(f"git status failed: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout.decode("utf-8", errors="replace")


def _read_state(full: Path) -> tuple[str, bytes, int] | None:
    if full.is_symlink():
        return ("link", os.readlink(full).encode(), 0)
    if full.is_file():
        return ("file", full.read_bytes(), stat.S_IMODE(full.stat().st_mode))
    if full.exists():
        return ("other", b"", 0)
    return None


def take_snapshot(working_dir: str, paths: set[str], timeout: float) -> _Snapshot:
    root = Path(working_dir).resolve()
    files: dict[str, tuple[str, bytes, int] | None] = {}
    new_dirs: set[Path] = set()
    for rel in paths:
        full = _inside(root, rel)
        if full is None:
            continue  # git apply refuses paths outside the tree
        state = _read_state(full)
        files[rel] = state
        if state is None:
            parent = full.parent
            while parent != root and root in parent.parents and not parent.exists():
                new_dirs.add(parent)
                parent = parent.parent
    ordered = sorted(new_dirs, key=lambda d: len(d.parts), reverse=True)
    return _Snapshot(root=root, files=files, new_dirs=ordered, status=_git_status(working_dir, timeout))


def restore_snapshot(snapshot: _Snapshot, timeout: float) -> None:
    """Put every touched path back as it was before the round, then verify it."""
    try:
        for rel, state in snapshot.files.items():
            full = snapshot.root / rel
            if state is not None and state[0] == "other":
                continue  # a directory or special file: git apply does not replace those
            if full.is_symlink() or full.is_file():
                full.unlink()
            elif full.is_dir():
                shutil.rmtree(full)
            if state is None:
                continue
            kind, data, mode = state
            full.parent.mkdir(parents=True, exist_ok=True)
            if kind == "link":
                os.symlink(data.decode(), full)
            else:
                full.write_bytes(data)
                os.chmod(full, mode)
        for directory in snapshot.new_dirs:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
    except OSError as exc:
        raise RollbackError(f"could not restore the working tree: {exc}") from exc
    changed = [rel for rel, state in snapshot.files.items() if _read_state(snapshot.root / rel) != state]
    if changed:
        raise RollbackError(f"working tree differs after rollback: {', '.join(sorted(changed))}")
    if _git_status(str(snapshot.root), timeout) != snapshot.status:
        raise RollbackError("git status differs after rollback")


def _compile_check(working_dir: str, paths: list[str], timeout: float) -> str | None:
    """``None`` when every file compiles, else the compiler's error text."""
    if not paths:
        return None
    with tempfile.TemporaryDirectory(prefix="local-worker-pyc-") as cache:
        # -I: the repo's own modules cannot shadow py_compile; bytecode goes to *cache*,
        # so no __pycache__ is left in the working tree.
        done = subprocess.run(
            [sys.executable, "-I", "-X", f"pycache_prefix={cache}", "-m", "py_compile", *paths],
            cwd=working_dir, text=True, capture_output=True, timeout=timeout,
        )
    if done.returncode == 0:
        return None
    return (done.stderr.strip() or done.stdout.strip() or f"py_compile exited {done.returncode}")


def _git_apply(args: list[str], working_dir: str, patch: str, timeout: float) -> str | None:
    done = subprocess.run(["git", "apply", *args, "-"], cwd=working_dir, input=patch,
                          text=True, capture_output=True, timeout=timeout)
    if done.returncode == 0:
        return None
    return done.stderr.strip() or done.stdout.strip() or "unknown git apply error"


def run_named_tests(working_dir: str, tests: list[str] | tuple[str, ...], timeout: float) -> str | None:
    """``None`` when the named tests pass, else why not (#598).

    No bytecode or pytest cache is written, so a rollback leaves the tree as it was.
    """
    if not tests:
        return None
    root = Path(working_dir).resolve()
    pytest_tests = [test for test in tests if "::" in test or test.endswith(".py")]
    unittest_tests = [test for test in tests if test not in pytest_tests]
    for test in pytest_tests:
        path = test.split("::", 1)[0]
        full = _inside(root, path)
        if full is None or not full.is_file():
            return f"named test {test} not found"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    limit = min(timeout, NAMED_TEST_TIMEOUT_SECONDS)
    try:
        commands = []
        if pytest_tests:
            commands.append([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *pytest_tests])
        if unittest_tests:
            commands.append([sys.executable, "-B", "-m", "unittest", "-q", *unittest_tests])
        results = [subprocess.run(command, cwd=working_dir, env=env, text=True,
                                  capture_output=True, timeout=limit) for command in commands]
    except subprocess.TimeoutExpired:
        return f"named tests timed out after {limit:g}s: {', '.join(tests)}"
    failed = next((done for done in results if done.returncode != 0), None)
    if failed is None:
        return None
    output = (failed.stdout + "\n" + failed.stderr).strip()
    return f"named test runner exited {failed.returncode}:\n{output[-FEEDBACK_ERROR_CHARS:]}"


def _generated_test_path(test_ids: list[str], working_dir: str) -> Path | None:
    """Recover the generated module path from ``module.Class.method`` IDs."""
    if not test_ids:
        return None
    module = test_ids[0].rsplit(".", 2)[0]
    if not module or any(test.rsplit(".", 2)[0] != module for test in test_ids):
        return None
    root = Path(working_dir).resolve()
    return _inside(root, module.replace(".", "/") + ".py")


def _install_generated_test(target: Path, content: str) -> bool:
    """Atomically install a generated test without replacing an existing file.

    The temporary file lives beside *target*, so linking it into place is an
    atomic same-filesystem operation.  ``False`` means another file already
    occupied the target; in all cases the private temporary file is removed.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
    )
    temporary = Path(temporary_name)
    stream = None
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1  # ownership passed to the stream
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        stream = None
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            if stream is not None:
                stream.close()
        finally:
            try:
                if fd >= 0:
                    os.close(fd)
            finally:
                temporary.unlink(missing_ok=True)


def apply_and_check(patch: str, working_dir: str, timeout: float, *,
                    allowed_paths: set[str] | None = None,
                    tests: list[str] | tuple[str, ...] = ()) -> None:
    """Apply *patch* and compile its ``.py`` files, or roll back and raise.

    For a subtask (#598) the patch may only touch *allowed_paths*, and the named
    *tests* must pass after it is applied.

    Raises :class:`_PatchRejected` after a successful rollback, and
    :class:`RollbackError` when the tree could not be restored.
    """
    if not patch:
        raise _PatchRejected("no_diff", "local model did not return a unified diff")
    if allowed_paths is not None:
        outside = sorted(patch_paths(patch) - allowed_paths)
        if outside:
            raise _PatchRejected(
                "outside_subtask",
                f"local model patch touches files outside this subtask: {', '.join(outside)}; "
                f"only change {', '.join(sorted(allowed_paths))}",
            )
    reason = _git_apply(["--check"], working_dir, patch, timeout)
    if reason is not None:
        raise _PatchRejected("does_not_apply", f"local model patch does not apply: {reason}")
    paths = patch_paths(patch)
    snapshot = take_snapshot(working_dir, paths, timeout)
    try:
        reason = _git_apply([], working_dir, patch, timeout)
        if reason is not None:
            raise _PatchRejected("apply_failed", f"local model patch failed to apply: {reason}")
        root = snapshot.root
        changed_py = sorted(rel for rel in snapshot.files
                            if rel.endswith(".py") and (root / rel).is_file() and not (root / rel).is_symlink()
                            and _read_state(root / rel) != snapshot.files[rel])
        reason = _compile_check(working_dir, changed_py, timeout)
        if reason is not None:
            raise _PatchRejected("compile_failed", f"local model patch breaks py_compile: {reason}")
        reason = run_named_tests(working_dir, tests, timeout)
        if reason is not None:
            raise _PatchRejected("tests_failed", f"named tests fail after the patch: {reason}")
    except BaseException:
        restore_snapshot(snapshot, timeout)
        raise


class LocalCodingWorker:
    provider = PROVIDER

    def __init__(self, *, client: LlamaCppClient | None = None, max_tokens: int = 4096,
                 split_tasks: bool = False, planner: Callable[[str], Any] | None = None,
                 test_first: bool = False, rates_path: str | Path | None = None,
                 **_: Any) -> None:
        self.client = client or LlamaCppClient()
        self.max_tokens = max_tokens
        # #598: split the task into small subtasks first. Off by default here; the
        # production factory turns it on via ``LOCAL_TASK_SPLIT``.
        self.split_tasks = split_tasks
        # The one free-tier fallback call when the local split is unusable.
        self.planner = planner or free_tier_plan
        self.test_first = test_first
        # Measured per model, so a smaller or quantized model starts fresh (#642).
        self.rates = LocalModelRates(rates_path, model=getattr(self.client, "model", None))
        self.last_plan: PromptPlan | None = None
        self.last_split: SplitOutcome | None = None
        self.generated_tests: tuple[str, ...] = ()
        self.local_attempt_passed_generated_tests: bool | None = None

    def run(self, *, task: str, working_dir: str, sandbox: str,
            timeout_seconds: float = 300, experiment_logger: Any | None = None,
            phase: str = "implementation", **_: Any) -> WorkerResult:
        started = time.monotonic()
        self.last_plan = None
        self.generated_tests = ()
        self.local_attempt_passed_generated_tests = None
        # Replaced by _plan_subtasks when splitting is on and the task reaches planning.
        self.last_split = SplitOutcome(
            reason="not planned" if self.split_tasks else f"splitting is off ({ENV_TASK_SPLIT})",
        )
        result: WorkerResult | None = None
        try:
            result = self._run(task, working_dir, sandbox, timeout_seconds, experiment_logger, phase, started)
            return result
        finally:
            if self.test_first:
                self._log(
                    experiment_logger, phase, "local_test_first_result",
                    ("Local attempt passed generated tests"
                     if self.local_attempt_passed_generated_tests is True
                     else "Local attempt did not pass generated tests"
                     if self.local_attempt_passed_generated_tests is False
                     else "No generated acceptance tests were retained"),
                    warning=self.local_attempt_passed_generated_tests is False,
                    tests_generated=len(self.generated_tests),
                    tests_failing_on_base=len(self.generated_tests),
                    local_attempt_passed_generated_tests=self.local_attempt_passed_generated_tests,
                    paid_fallback_used=False,
                )
            # One record per run, split or not, so split vs. one-shot success can be compared.
            split = self.last_split
            success = bool(result and result.success)
            self._log(
                experiment_logger, phase, "local_task_split_result",
                (f"Local subtasks: {split.succeeded}/{len(split.subtasks)} succeeded"
                 + (f", subtask {split.failed_subtask} failed" if split.failed_subtask else "")
                 if split.split else f"Local task ran one-shot ({split.reason})"),
                warning=not success, success=success, **split.log_fields(),
            )

    def _run(self, task: str, working_dir: str, sandbox: str, timeout_seconds: float,
             experiment_logger: Any | None, phase: str, started: float) -> WorkerResult:
        try:
            validate_worker_config(task, working_dir, sandbox)
            if self.test_first:
                ids, content = generate_failing_tests(task, working_dir, self.client)
                target = _generated_test_path(ids, working_dir)
                if ids and content and target is not None and not target.exists():
                    if _install_generated_test(target, content):
                        self.generated_tests = tuple(ids)
                self._log(
                    experiment_logger, phase, "local_test_first_generated",
                    f"Generated {len(self.generated_tests)} failing acceptance test(s)",
                    tests_generated=len(ids), tests_failing_on_base=len(self.generated_tests),
                    local_attempt_passed_generated_tests=None, paid_fallback_used=False,
                    tests=list(self.generated_tests),
                )
            try:
                # n_ctx is read once here and holds for this whole run.
                plan = asyncio.run(build_prompt(task, working_dir, self.client,
                                                max_output_tokens=self.max_tokens,
                                                rates=self.rates.medians()))
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
            if self.split_tasks:
                if plan.time_budget_limited:
                    # #642: the prompt is over the time budget, so the one call
                    # we can afford goes to the patch, not to planning.
                    self.last_split.reason = "planning skipped: prompt is over the local time budget"
                    self._log(
                        experiment_logger, phase, "local_task_split",
                        "Task planning skipped to reserve the affordable model call for the patch",
                        warning=True, **self.last_split.log_fields(),
                        time_budget_tokens=plan.time_budget_tokens,
                        time_budget_exceeded=plan.time_budget_exceeded, skipped=True,
                    )
                    split = self.last_split
                else:
                    split = self._plan_subtasks(plan, working_dir, experiment_logger, phase)
                if split.split:
                    split.subtasks = [Subtask(s.goal, s.files,
                                              tuple(dict.fromkeys((*s.tests, *self.generated_tests))))
                                      for s in split.subtasks]
                    return self._run_subtasks(split, plan, task, working_dir, timeout_seconds,
                                              experiment_logger, phase, started)
            return self._correction_loop(plan, task, working_dir, timeout_seconds,
                                         experiment_logger, phase, started,
                                         tests=self.generated_tests)
        except subprocess.TimeoutExpired:
            return self._result(task, working_dir, "", "local model patch application timed out", started,
                                success=False, exit_code=None, timed_out=True)
        except Exception as exc:  # worker failures are represented, never leaked into the pipeline
            # An httpx timeout stringifies to "" - never record an empty error (#642).
            error = str(exc).strip() or f"{type(exc).__name__} (no message)"
            return self._result(task, working_dir, "", error, started, success=False, exit_code=1)

    # Splitting (#598)

    def _plan_subtasks(self, plan: PromptPlan, working_dir: str, experiment_logger: Any | None,
                       phase: str) -> SplitOutcome:
        """Local model first; if its plan is unusable, exactly one free-tier call."""
        outcome = SplitOutcome()
        self.last_split = outcome
        local_reason = ""
        subtasks: list[Subtask] = []
        unreachable = False
        try:
            prompt = asyncio.run(self._local_plan_prompt(plan))
            answer = self._model_call(
                prompt, _PLAN_SYSTEM_PROMPT, min(plan.output_tokens or self.max_tokens, PLAN_MAX_TOKENS),
                experiment_logger, phase, call_type="plan",
            )
            subtasks = parse_plan(answer, working_dir)
            outcome.planner, outcome.planner_model = "local", "local"
            if len(subtasks) == 1:
                # One subtask is no split: as unusable as a broken plan.
                raise PlanUnusable("a single subtask is not a split")
        except PlanUnusable as exc:  # the free-tier planner is next
            subtasks = []
            outcome.planner = outcome.planner_model = ""
            local_reason = f"local plan unusable: {exc}"
        except Exception as exc:
            # The local model is down: the one-shot fails the same way, so a
            # free-tier call would be wasted.
            unreachable = True
            outcome.reason = f"local planner call failed: {type(exc).__name__}: {exc}"
        if not subtasks and not unreachable:
            subtasks, fallback_reason = self._free_tier_subtasks(plan, working_dir, outcome)
            if not subtasks:
                outcome.reason = f"{local_reason}; {fallback_reason}"
        if len(subtasks) == 1:
            outcome.reason = f"{outcome.planner} planner returned a single subtask; running one-shot"
            subtasks = []
        if subtasks:
            outcome.split = True
            outcome.subtasks = subtasks
            outcome.reason = local_reason
        else:
            outcome.planner = outcome.planner_model = ""
        self._log(
            experiment_logger, phase, "local_task_split",
            (f"Task split into {len(subtasks)} subtasks by the {outcome.planner} planner"
             if outcome.split else f"Task not split: {outcome.reason}"),
            warning=not outcome.split, **outcome.log_fields(),
            subtask_goals=[s.goal[:200] for s in subtasks],
        )
        return outcome

    async def _local_plan_prompt(self, plan: PromptPlan) -> str:
        count = plan.counter or TokenCounter(self.client)
        budget = plan.n_ctx - min(self.max_tokens, PLAN_MAX_TOKENS) - CONTEXT_MARGIN_TOKENS

        async def total(handover: str) -> int:
            return await count(_PLAN_SYSTEM_PROMPT) + await count(
                _render_plan_prompt(plan.essential, plan.named_files, handover))

        if await total("") > budget:
            raise PlanUnusable("the planning prompt does not fit the local context")
        kept = await _fit_part(plan.handover, budget, total, count)
        return _render_plan_prompt(plan.essential, plan.named_files, kept)

    def _free_tier_subtasks(self, plan: PromptPlan, working_dir: str,
                            outcome: SplitOutcome) -> tuple[list[Subtask], str]:
        handover = plan.handover
        if len(handover) > _FREE_TIER_HANDOVER_CHARS:
            handover = handover[:_FREE_TIER_HANDOVER_CHARS].rstrip() + _CUT_MARKER
        prompt = _render_plan_prompt(plan.essential[:_FREE_TIER_ESSENTIAL_CHARS], plan.named_files, handover)
        outcome.free_tier_calls += 1
        try:
            answer = self.planner(prompt)
        except Exception as exc:
            return [], f"free-tier planner failed: {type(exc).__name__}: {exc}"
        if answer is None:
            outcome.free_tier_calls -= 1  # no free model configured: nothing was called
            return [], "no free-tier planner configured"
        if getattr(answer, "cost", "free") not in ("free", "local"):
            return [], f"planner answered from a {answer.cost} model; refused"
        if not getattr(answer, "success", True):
            return [], f"free-tier planner failed: {getattr(answer, 'error', 'unknown')}"
        content = answer if isinstance(answer, str) else getattr(answer, "content", "")
        try:
            subtasks = parse_plan(content, working_dir)
        except PlanUnusable as exc:
            return [], f"free-tier plan unusable: {exc}"
        outcome.planner = "free_tier"
        outcome.planner_model = str(getattr(answer, "model_id", "") or "free_tier")
        return subtasks, ""

    def _run_subtasks(self, split: SplitOutcome, plan: PromptPlan, task: str, working_dir: str,
                      timeout: float, experiment_logger: Any | None, phase: str,
                      started: float) -> WorkerResult:
        """Each subtask on the same tree, seeing the earlier diffs; stop at the first failure."""
        total = len(split.subtasks)
        applied: list[str] = []
        outputs: list[str] = []
        for number, subtask in enumerate(split.subtasks, start=1):
            label = f"subtask {number}/{total}"
            self._log(experiment_logger, phase, "local_subtask_started",
                      f"Local {label}: {subtask.goal[:200]}", subtask=number, subtasks=total,
                      files=list(subtask.files), tests=list(subtask.tests))
            try:
                sub_plan = asyncio.run(self._subtask_plan(plan, subtask, number, total, applied, working_dir))
            except ContextTooSmallError as exc:
                ok, output, reason = False, "", f"subtask prompt does not fit: {exc}"
            else:
                ok, output, reason = self._patch_rounds(
                    sub_plan, working_dir, timeout, experiment_logger, phase,
                    # Only the declared files: named tests are checks, editable only
                    # when the subtask lists them in ``files`` too.
                    allowed_paths=set(subtask.files),
                    tests=subtask.tests, subtask=number,
                )
            self._log(experiment_logger, phase, "local_subtask_finished",
                      f"Local {label}: {'applied' if ok else 'failed'}" + (f" ({reason[:300]})" if reason else ""),
                      warning=not ok, subtask=number, subtasks=total, success=ok, reason=reason)
            if not ok:
                split.failed_subtask = number
                kept = (f"; the changes of subtasks 1-{number - 1} stay in the working tree "
                        "but the task is not done") if number > 1 else ""
                return self._result(
                    task, working_dir, "",
                    f"local {label} failed ({subtask.goal[:200]}): {reason}{kept}",
                    started, success=False, exit_code=1,
                )
            split.succeeded += 1
            applied.append(_extract_diff(output))
            outputs.append(output)
        return self._result(task, working_dir, "\n".join(outputs), None, started, success=True, exit_code=0)

    async def _subtask_plan(self, plan: PromptPlan, subtask: Subtask, number: int, total: int,
                            applied: list[str], working_dir: str) -> PromptPlan:
        tests = ", ".join(subtask.tests) if subtask.tests else "none named"
        essential = (
            f"Subtask {number} of {total} of a larger task. Make only this change now; "
            "the other subtasks do the rest.\n"
            f"Subtask: {subtask.goal}\n"
            f"Files to change (only these): {', '.join(subtask.files)}\n"
            f"Tests that must pass afterwards (run only; do not edit them unless listed "
            f"above): {tests}"
        )
        earlier = "".join(applied).strip() or "(none yet)"
        context = (
            f"Changes already made by the earlier subtasks (git diff, already applied):\n{earlier}\n\n"
            f"Overall task (context only; do not do the other subtasks):\n{plan.essential}"
        )
        return await build_prompt_parts(
            essential, context, working_dir, self.client, max_output_tokens=self.max_tokens,
            n_ctx=plan.n_ctx, n_ctx_source=plan.n_ctx_source, counter=plan.counter,
            rates=self.rates.medians(),
        )

    # The correction loop (#597)

    def _correction_loop(self, plan: PromptPlan, task: str, working_dir: str, timeout: float,
                         experiment_logger: Any | None, phase: str, started: float,
                         tests: tuple[str, ...] = ()) -> WorkerResult:
        ok, output, reason = self._patch_rounds(plan, working_dir, timeout, experiment_logger, phase,
                                                tests=tests)
        if ok:
            return self._result(task, working_dir, output, None, started, success=True, exit_code=0)
        return self._result(task, working_dir, "", reason, started, success=False, exit_code=1)

    def _patch_rounds(self, plan: PromptPlan, working_dir: str, timeout: float,
                      experiment_logger: Any | None, phase: str, *,
                      allowed_paths: set[str] | None = None, tests: tuple[str, ...] = (),
                      subtask: int | None = None) -> tuple[bool, str, str]:
        """``(applied, model output, failure reason)`` after at most ``MAX_PATCH_ROUNDS``."""
        reason = ""
        previous_diff = ""
        tag: dict[str, Any] = {"subtask": subtask} if subtask is not None else {}
        for round_no in range(1, MAX_PATCH_ROUNDS + 1):
            prompt, extra = plan.prompt, {"prompt_tokens": plan.prompt_tokens, **tag}
            if round_no > 1:
                fb = asyncio.run(build_feedback_prompt(
                    plan, self.client, round_no=round_no, error=reason, previous_diff=previous_diff,
                ))
                prompt = fb.prompt
                extra = {"prompt_tokens": fb.prompt_tokens, "feedback_error_chars": fb.error_chars,
                         "feedback_diff_cut": fb.diff_cut, "handover_tokens_kept": fb.handover_tokens_kept,
                         **tag}
            try:
                output = self._model_call(
                    prompt, _SYSTEM_PROMPT, plan.output_tokens or self.max_tokens, experiment_logger, phase,
                    call_type="patch", round=round_no, **tag,
                )
            except Exception as exc:
                if self._is_model_timeout(exc):
                    limit = getattr(self.client, "timeout", timeout)
                    reason = str(exc) or f"local model call timed out after {limit}s"
                    self._log_round(experiment_logger, phase, round_no, "model_timeout", reason, **extra)
                    return False, "", reason
                raise
            patch = _extract_diff(output)
            try:
                apply_and_check(patch, working_dir, timeout, allowed_paths=allowed_paths, tests=tests)
            except _PatchRejected as exc:
                reason = exc.reason
                previous_diff = patch or output.strip()
                self._log_round(experiment_logger, phase, round_no, exc.outcome, reason, **extra)
                continue
            except RollbackError as exc:
                reason = f"local model patch rollback failed in round {round_no}: {exc}"
                self._log_round(experiment_logger, phase, round_no, "rollback_failed", reason, **extra)
                return False, "", reason
            except subprocess.TimeoutExpired:
                self._log_round(experiment_logger, phase, round_no, "timed_out",
                                "local model patch application timed out", **extra)
                raise
            self._log_round(experiment_logger, phase, round_no, "applied", "", **extra)
            if self.generated_tests and set(self.generated_tests).issubset(tests):
                self.local_attempt_passed_generated_tests = True
            return True, output, ""
        if self.generated_tests and set(self.generated_tests).issubset(tests):
            # A prior subtask may already have satisfied the generated tests even
            # though this subtask exhausted its rounds for an unrelated reason
            # (for example, an empty or out-of-scope patch).  Judge fallback from
            # the current tree, not from the outcome of the last patch round.
            # Clear a result from an earlier subtask first so an unexpected runner
            # failure cannot leave a stale ``True`` that suppresses normal failure
            # handling.
            self.local_attempt_passed_generated_tests = None
            generated_test_failure = run_named_tests(working_dir, self.generated_tests, timeout)
            self.local_attempt_passed_generated_tests = generated_test_failure is None
        return False, "", f"local model patch failed after {MAX_PATCH_ROUNDS} rounds; last reason: {reason}"

    @staticmethod
    def _is_model_timeout(exc: Exception) -> bool:
        return (isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                or "timed out" in str(exc).lower()
                or "timeout" in type(exc).__name__.lower())

    def _model_call(self, prompt: str, system: str, max_tokens: int,
                    experiment_logger: Any | None, phase: str, *, call_type: str,
                    **fields: Any) -> str:
        """Call the local model and always leave a latency/outcome event."""
        started = time.monotonic()
        if hasattr(self.client, "last_timings"):
            self.client.last_timings = None
        outcome = "ok"
        try:
            answer = asyncio.run(self.client.complete(
                prompt, system=system, temperature=0.0, max_tokens=max_tokens,
            ))
            return answer
        except Exception as exc:
            outcome = "timeout" if self._is_model_timeout(exc) else "error"
            raise
        finally:
            wall_seconds = time.monotonic() - started
            timings = getattr(self.client, "last_timings", None)
            if outcome == "ok":
                self.rates.record(timings)  # never raises
            prompt_rate, generation_rate = self.rates.medians()
            self._log(
                experiment_logger, phase, "local_model_call",
                f"Local {call_type} model call: {outcome} in {wall_seconds:.1f}s",
                warning=outcome != "ok", call_type=call_type, outcome=outcome,
                wall_seconds=wall_seconds, timings=timings,
                prompt_tokens_per_second=prompt_rate,
                generation_tokens_per_second=generation_rate,
                max_tokens=max_tokens, **fields,
            )

    def _log_round(self, experiment_logger: Any | None, phase: str, round_no: int, outcome: str,
                   reason: str, **fields: Any) -> None:
        self._log(
            experiment_logger, phase, "local_patch_round",
            f"Local patch round {round_no}/{MAX_PATCH_ROUNDS}: {outcome}" + (f" ({reason[:300]})" if reason else ""),
            warning=outcome != "applied", round=round_no, outcome=outcome, reason=reason, **fields,
        )

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
