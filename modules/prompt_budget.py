"""Configurable prompt/token budget for the persistent Nexus controller.

The persistent local controller previously resent the complete accumulated
task/discussion history and every previous tool observation on every turn,
so prompt size grew roughly linearly (in practice compounding, since the
observations were *also* duplicated through the persisted history view).
Left unbounded this eventually produces prompts large enough to have
llama-server killed by the Linux OOM killer.

This module keeps the budgeting logic small, dependency-free and unit
testable: it computes a conservative effective character budget from the
selected model's context window, reserved output tokens and available host
memory, then exposes helpers to deterministically compact/truncate
observations so a compacted prompt always fits inside that budget.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MEMINFO_PATH = "/proc/meminfo"
# Conservative estimate for English/code text; used only for logging and
# coarse budgeting, never for exact tokenizer-accurate truncation.
CHARS_PER_TOKEN = 4


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def read_mem_available_kb(meminfo_path: str = DEFAULT_MEMINFO_PATH) -> int | None:
    """Best-effort read of ``MemAvailable`` (kB) from ``/proc/meminfo``.

    Returns ``None`` when the file is missing/unreadable so callers can fall
    back to context-window-only budgeting instead of failing.
    """
    path = Path(meminfo_path)
    try:
        if not path.is_file():
            return None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "MemAvailable":
                digits = "".join(ch for ch in value if ch.isdigit())
                return int(digits) if digits else None
    except OSError:
        return None
    return None


@dataclass(slots=True)
class PromptBudgetPlan:
    """One budget computation, used both for prompt sizing and for logging."""

    model_context_tokens: int
    reserved_output_tokens: int
    mem_available_kb: int | None
    mem_limit_kb: int
    context_budget_chars: int
    memory_budget_chars: int | None
    effective_budget_chars: int
    safe_local_budget_chars: int
    low_memory: bool
    instructions_chars: int
    tool_schema_chars: int
    current_input_chars: int
    history_chars: int
    observation_chars: int

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "model_context_tokens": self.model_context_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "mem_available_kb": self.mem_available_kb,
            "mem_limit_kb": self.mem_limit_kb,
            "context_budget_chars": self.context_budget_chars,
            "memory_budget_chars": self.memory_budget_chars,
            "effective_budget_chars": self.effective_budget_chars,
            "safe_local_budget_chars": self.safe_local_budget_chars,
            "low_memory": self.low_memory,
        }


def compute_budget(
    *,
    model_context_tokens: int | None = None,
    reserved_output_tokens: int = 700,
    mem_available_kb: int | None = None,
    mem_limit_mb: int | None = None,
    chars_per_token: int = CHARS_PER_TOKEN,
) -> PromptBudgetPlan:
    """Compute a conservative effective prompt budget.

    Model context capacity and host-memory safety are treated as two
    independent limits; the smaller one always wins, so the effective budget
    never claims more than either.
    """
    model_context_tokens = model_context_tokens if model_context_tokens is not None else _env_int(
        "NEXUS_LOCAL_MODEL_CONTEXT_TOKENS", 8192
    )
    if mem_limit_mb is None:
        mem_limit_mb = _env_int("NEXUS_LOCAL_MODEL_MEM_LIMIT_MB", 2048)
    mem_limit_kb = max(1, mem_limit_mb) * 1024

    context_tokens_available = max(256, model_context_tokens - max(0, reserved_output_tokens))
    context_budget_chars = context_tokens_available * chars_per_token

    low_memory = False
    memory_budget_chars: int | None = None
    if mem_available_kb is not None:
        # Very conservative: assume the configured memory ceiling is mostly
        # consumed by model weights/KV cache and headroom, so only a small
        # fraction of *actually free* host memory can safely be attributed to
        # additional input tokens.
        usable_kb = max(0, min(mem_available_kb, mem_limit_kb))
        memory_tokens_available = usable_kb // 2  # ~2kB headroom per input token
        memory_budget_chars = max(0, int(memory_tokens_available) * chars_per_token)
        if mem_available_kb < mem_limit_kb * 0.25:
            low_memory = True

    if memory_budget_chars is None:
        effective_budget_chars = context_budget_chars
    else:
        effective_budget_chars = min(context_budget_chars, memory_budget_chars)
    effective_budget_chars = max(500, effective_budget_chars)

    # The "safe local" budget is what may actually be sent to llama-server.
    # It is deliberately not inflated back up when memory is unknown/tight,
    # so a missing MemAvailable reading never silently permits an unsafe call.
    safe_local_budget_chars = effective_budget_chars
    if low_memory:
        safe_local_budget_chars = min(safe_local_budget_chars, memory_budget_chars or safe_local_budget_chars)

    instructions_chars = int(effective_budget_chars * 0.12)
    tool_schema_chars = int(effective_budget_chars * 0.18)
    current_input_chars = int(effective_budget_chars * 0.15)
    history_chars = int(effective_budget_chars * 0.25)
    observation_chars = max(
        0,
        effective_budget_chars - instructions_chars - tool_schema_chars - current_input_chars - history_chars,
    )

    return PromptBudgetPlan(
        model_context_tokens=model_context_tokens,
        reserved_output_tokens=reserved_output_tokens,
        mem_available_kb=mem_available_kb,
        mem_limit_kb=mem_limit_kb,
        context_budget_chars=context_budget_chars,
        memory_budget_chars=memory_budget_chars,
        effective_budget_chars=effective_budget_chars,
        safe_local_budget_chars=safe_local_budget_chars,
        low_memory=low_memory,
        instructions_chars=instructions_chars,
        tool_schema_chars=tool_schema_chars,
        current_input_chars=current_input_chars,
        history_chars=history_chars,
        observation_chars=observation_chars,
    )


def truncate_text(text: str, max_chars: int, *, note: str = "truncated") -> tuple[str, bool]:
    """Deterministically truncate ``text`` to fit ``max_chars``."""
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    marker = f"...[{note}]"
    keep = max(0, max_chars - len(marker))
    return f"{text[:keep]}{marker}", True


def compact_observations(
    observations: list[dict[str, Any]],
    *,
    budget_chars: int,
    keep_recent_full: int = 2,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Bound observations sent to the model to a total character budget.

    The most recent ``keep_recent_full`` observations are kept closest to
    full fidelity (still individually capped); older observations are
    replaced by a small stub referencing their stable event id so the raw
    result stays retrievable from the database without resending it inline.
    Returns ``(compacted_observations, omitted_indices)`` where
    ``omitted_indices`` are 0-based positions in the *input* list that were
    reduced to a stub.
    """
    n = len(observations)
    if n == 0 or budget_chars <= 0:
        return (list(observations), []) if budget_chars > 0 else (
            [_stub(obs) for obs in observations],
            list(range(n)),
        )

    newest_first = list(range(n - 1, -1, -1))
    remaining = budget_chars
    result: list[dict[str, Any] | None] = [None] * n
    omitted: list[int] = []

    for position, index in enumerate(newest_first):
        obs = observations[index]
        serialized = json.dumps(obs, separators=(",", ":"))
        is_recent = position < keep_recent_full
        fair_share = remaining if is_recent else min(remaining, max(500, budget_chars // n))
        if len(serialized) <= fair_share:
            result[index] = obs
            remaining = max(0, remaining - len(serialized))
            continue
        if is_recent and remaining > 500:
            # Still compress the observation's largest text fields rather
            # than dropping it outright, since recent observations are the
            # most likely to matter for the immediate next decision. This is
            # tracked separately from ``omitted``: the observation still
            # carries real (if truncated) content, it is not reduced to a
            # bare stub reference.
            compacted, truncated = _shrink(obs, remaining)
            if truncated:
                compacted["truncated"] = True
            serialized_compacted = json.dumps(compacted, separators=(",", ":"))
            result[index] = compacted
            remaining = max(0, remaining - len(serialized_compacted))
            continue
        stub = _stub(obs)
        serialized_stub = json.dumps(stub, separators=(",", ":"))
        result[index] = stub
        omitted.append(index)
        remaining = max(0, remaining - len(serialized_stub))

    return [item for item in result if item is not None], sorted(omitted)


def _stub(obs: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(obs, separators=(",", ":"))
    stub: dict[str, Any] = {
        "tool": obs.get("tool"),
        "truncated": True,
        "original_chars": len(serialized),
    }
    event_id = obs.get("event_id")
    if event_id is not None:
        stub["event_id"] = event_id
        stub["note"] = "full result stored durably; fetch by event_id if needed"
    return stub


_LARGE_TEXT_FIELDS = ("result", "content", "output_lines", "detail")


def _shrink(obs: dict[str, Any], max_chars: int) -> tuple[dict[str, Any], bool]:
    """Truncate the largest text-ish fields of one observation to fit."""
    shrunk = dict(obs)
    truncated = False
    overhead = len(json.dumps({k: v for k, v in shrunk.items() if k not in _LARGE_TEXT_FIELDS}, separators=(",", ":")))
    remaining = max(0, max_chars - overhead)
    for field in _LARGE_TEXT_FIELDS:
        if field not in shrunk or remaining <= 0:
            continue
        value = shrunk[field]
        if isinstance(value, str):
            shrunk[field], was_truncated = truncate_text(value, remaining)
            truncated = truncated or was_truncated
            remaining -= len(shrunk[field])
        elif isinstance(value, list):
            text = json.dumps(value, separators=(",", ":"))
            if len(text) > remaining:
                keep = max(1, remaining // 200)
                shrunk[field] = [str(item)[:200] for item in value[:keep]]
                truncated = True
            remaining -= len(json.dumps(shrunk[field], separators=(",", ":")))
    return shrunk, truncated


def dedupe_nested_results(
    observations: list[dict[str, Any]],
    *,
    already_seen: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Drop memory-search hits that duplicate content already in this prompt.

    Repeated ``memory_search`` calls can surface earlier task/history results
    (including previous tool observations) again, which nests duplicate
    context inside an already-duplicated section. This keeps only the first
    occurrence of any given result body across the whole observation list.
    """
    seen = set(already_seen or ())
    deduped: list[dict[str, Any]] = []
    for obs in observations:
        if obs.get("tool") != "memory_search" or not isinstance(obs.get("results"), list):
            deduped.append(obs)
            continue
        kept_results = []
        for hit in obs["results"]:
            content = str(hit.get("content", "")) if isinstance(hit, dict) else str(hit)
            fingerprint = content.strip()[:400]
            if fingerprint and fingerprint in seen:
                continue
            if fingerprint:
                seen.add(fingerprint)
            kept_results.append(hit)
        new_obs = dict(obs)
        new_obs["results"] = kept_results
        if len(kept_results) != len(obs["results"]):
            new_obs["deduped"] = len(obs["results"]) - len(kept_results)
        deduped.append(new_obs)
    return deduped
