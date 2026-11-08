"""Prompt budget for the persistent Nexus controller, bounded by the context window: the
controller resends its observations every turn, so an unbounded prompt would outgrow it. The
window is the one hard limit (llama-server allocates its KV cache for ``n_ctx`` up front; a
full-context prompt costs ~109 MB more RSS on the VM, #230), so host memory is only reported as
a signal (``low_memory``, ``safe_local_budget_chars``) and never shrinks the budget or gates a
local call. Non-compacted sections (rules, capability catalog) are passed in as their measured
length (``fixed_chars``); what remains goes to the current input, the observations and, last, the
persisted (re-readable) history. Dependency-free so it stays unit testable."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modules.observation_summary import summarize_observation
from modules.tool_result_reader import read_hint

logger = logging.getLogger(__name__)

DEFAULT_MEMINFO_PATH = "/proc/meminfo"
# Conservative estimate for English/code text; used only for logging and coarse budgeting,
# never for exact tokenizer-accurate truncation.
CHARS_PER_TOKEN = 4

# Shares of the *variable* room (budget minus the measured fixed sections): the current
# input is capped, history keeps a small reserved floor, observations get the rest - they
# carry the answer to the current task, while history is persisted and re-readable.
INPUT_SHARE = 0.20
HISTORY_FLOOR_SHARE = 0.10
# Below this much variable room a controller cannot see its own tool results and would loop;
# it fails loudly instead of running blind (#230).
MIN_VARIABLE_CHARS = 2000


class PromptBudgetExhaustedError(RuntimeError):
    """The fixed prompt sections leave no usable room for the task's content."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def read_mem_available_kb(meminfo_path: str = DEFAULT_MEMINFO_PATH) -> int | None:
    """Best-effort read of ``MemAvailable`` (kB) from /proc/meminfo; ``None`` when missing or
    unreadable, so callers fall back to context-window-only budgeting instead of failing."""
    return _read_meminfo_field(meminfo_path, "MemAvailable")


def read_swap_free_kb(meminfo_path: str = DEFAULT_MEMINFO_PATH) -> int | None:
    """Best-effort read of ``SwapFree`` (kB) from /proc/meminfo. Low/absent swap is an extra
    low-memory signal only when swap is actually configured (see ``read_swap_total_kb``) - a host
    without swap reports ``SwapFree: 0`` forever, which is not itself pressure."""
    return _read_meminfo_field(meminfo_path, "SwapFree")


def read_swap_total_kb(meminfo_path: str = DEFAULT_MEMINFO_PATH) -> int | None:
    """Best-effort read of ``SwapTotal`` (kB) from /proc/meminfo; callers must check
    ``SwapTotal > 0`` before treating a low ``SwapFree`` as pressure."""
    return _read_meminfo_field(meminfo_path, "SwapTotal")


def _read_meminfo_field(meminfo_path: str, field: str) -> int | None:
    path = Path(meminfo_path)
    try:
        if not path.is_file():
            return None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.strip() == field:
                digits = "".join(ch for ch in value if ch.isdigit())
                return int(digits) if digits else None
    except OSError:
        return None
    return None


@dataclass(slots=True)
class PromptBudgetPlan:
    """One budget computation, used for prompt sizing and logging: ``effective_budget_chars``
    (context window minus reserved output) splits into measured ``fixed_chars`` (never compacted)
    and ``variable_chars``, which splits into ``current_input_chars`` (a cap), ``observation_chars``
    and ``history_chars`` (a reserved floor; history also gets what observations leave unused). For
    a non-starved plan the four sum to ``effective_budget_chars``. The memory fields are advisory
    signals only - logged and reported, never applied (#230)."""

    model_context_tokens: int
    reserved_output_tokens: int
    mem_available_kb: int | None
    mem_limit_kb: int
    swap_free_kb: int | None
    swap_total_kb: int | None
    inference_headroom_kb: int
    context_budget_chars: int
    memory_budget_chars: int | None
    effective_budget_chars: int
    safe_local_budget_chars: int
    low_memory: bool
    fixed_chars: int
    variable_chars: int
    starved: bool
    current_input_chars: int
    history_chars: int
    observation_chars: int

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "model_context_tokens": self.model_context_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "mem_available_kb": self.mem_available_kb,
            "mem_limit_kb": self.mem_limit_kb,
            "swap_free_kb": self.swap_free_kb,
            "swap_total_kb": self.swap_total_kb,
            "inference_headroom_kb": self.inference_headroom_kb,
            "context_budget_chars": self.context_budget_chars,
            "memory_budget_chars": self.memory_budget_chars,
            "effective_budget_chars": self.effective_budget_chars,
            "safe_local_budget_chars": self.safe_local_budget_chars,
            "low_memory": self.low_memory,
            "fixed_chars": self.fixed_chars,
            "variable_chars": self.variable_chars,
            "starved": self.starved,
            "current_input_chars": self.current_input_chars,
            "observation_chars": self.observation_chars,
            "history_chars": self.history_chars,
        }

    def require_usable(self) -> None:
        """Raise instead of letting a blind controller loop on stubs (#230)."""
        if self.starved:
            raise PromptBudgetExhaustedError(
                f"prompt budget exhausted: the fixed prompt sections take {self.fixed_chars} "
                f"of {self.effective_budget_chars} characters, leaving {self.variable_chars} "
                f"for the task, its tool results and history (minimum {MIN_VARIABLE_CHARS}); "
                "the controller could not see its own tool results"
            )


def compute_budget(
    *,
    model_context_tokens: int | None = None,
    reserved_output_tokens: int = 700,
    fixed_chars: int = 0,
    mem_available_kb: int | None = None,
    swap_free_kb: int | None = None,
    swap_total_kb: int | None = None,
    mem_limit_mb: int | None = None,
    inference_headroom_mb: int | None = None,
    kb_per_input_token: int | None = None,
    chars_per_token: int = CHARS_PER_TOKEN,
) -> PromptBudgetPlan:
    """Compute the prompt budget from the context window: ``fixed_chars`` is the measured length of
    every section rendered in full (rules, capability catalog, framing) and is subtracted as-is; only
    the remainder is shared between input, observations and history. Host memory is read only to
    report pressure - it changes no section: a resident model KV cache is already sized for the
    whole window, so a larger prompt costs a bounded ~100 MB and an attempt can succeed where a
    prediction said it would not."""
    model_context_tokens = model_context_tokens if model_context_tokens is not None else _env_int(
        "NEXUS_LOCAL_MODEL_CONTEXT_TOKENS", 8192
    )
    if mem_limit_mb is None:
        mem_limit_mb = _env_int("NEXUS_LOCAL_MODEL_MEM_LIMIT_MB", 2048)
    mem_limit_kb = max(1, mem_limit_mb) * 1024

    if inference_headroom_mb is None:
        inference_headroom_mb = _env_int("NEXUS_LOCAL_MODEL_INFERENCE_HEADROOM_MB", 512)
    inference_headroom_kb = max(0, inference_headroom_mb) * 1024

    if kb_per_input_token is None:
        kb_per_input_token = _env_int("NEXUS_LOCAL_MODEL_KB_PER_TOKEN", 8)
    kb_per_input_token = max(1, kb_per_input_token)

    context_tokens_available = max(256, model_context_tokens - max(0, reserved_output_tokens))
    context_budget_chars = context_tokens_available * chars_per_token

    # --- Advisory memory signal: reported, never applied. ---
    low_memory = False
    memory_budget_chars: int | None = None
    if mem_available_kb is not None:
        surplus_kb = mem_available_kb - inference_headroom_kb
        if surplus_kb <= 0:
            low_memory = True
            memory_budget_chars = 0
        else:
            memory_budget_chars = int(surplus_kb // kb_per_input_token) * chars_per_token
        # A host with no swap reports ``SwapFree: 0`` forever, so exhausted swap is only pressure
        # when swap exists and RAM is thin too.
        swap_configured = swap_total_kb is not None and swap_total_kb > 0
        ram_pressure = surplus_kb < inference_headroom_kb
        if swap_configured and swap_free_kb is not None and swap_free_kb < inference_headroom_kb and ram_pressure:
            low_memory = True
            memory_budget_chars = 0

    # The context window is the bound; there is no floor that would let the budget claim room
    # the window does not have.
    effective_budget_chars = context_budget_chars
    # Advisory only: a resident model can still serve a request under pressure, so this must
    # never become a routing gate.
    safe_local_budget_chars = 0 if low_memory else effective_budget_chars

    fixed_chars = max(0, int(fixed_chars))
    variable_chars = max(0, effective_budget_chars - fixed_chars)
    starved = variable_chars < MIN_VARIABLE_CHARS
    current_input_chars = int(variable_chars * INPUT_SHARE)
    history_chars = int(variable_chars * HISTORY_FLOOR_SHARE)
    observation_chars = max(0, variable_chars - current_input_chars - history_chars)

    return PromptBudgetPlan(
        model_context_tokens=model_context_tokens,
        reserved_output_tokens=reserved_output_tokens,
        mem_available_kb=mem_available_kb,
        mem_limit_kb=mem_limit_kb,
        swap_free_kb=swap_free_kb,
        swap_total_kb=swap_total_kb,
        inference_headroom_kb=inference_headroom_kb,
        context_budget_chars=context_budget_chars,
        memory_budget_chars=memory_budget_chars,
        effective_budget_chars=effective_budget_chars,
        safe_local_budget_chars=safe_local_budget_chars,
        low_memory=low_memory,
        fixed_chars=fixed_chars,
        variable_chars=variable_chars,
        starved=starved,
        current_input_chars=current_input_chars,
        history_chars=history_chars,
        observation_chars=observation_chars,
    )


def fit_history(history: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """Keep the newest history events whose serialised list fits ``max_chars``; history is
    persisted and re-readable, so it is the section that gives way when observations need the room.
    Order is preserved."""
    kept: list[dict[str, Any]] = []
    for event in reversed(history):
        candidate = [event, *kept]
        if len(json.dumps(candidate, separators=(",", ":"))) > max_chars:
            break
        kept = candidate
    return kept


def truncate_text(text: str, max_chars: int, *, note: str = "truncated") -> tuple[str, bool]:
    """Deterministically truncate ``text`` to fit ``max_chars``."""
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    marker = f"...[{note}]"
    keep = max(0, max_chars - len(marker))
    return f"{text[:keep]}{marker}", True


def prompt_json(value: Any) -> str:
    """Serialise for a model prompt, dropping no-meaning defaults (``"arguments":[]``,
    ``"default":null`` - paid for in wall-clock time at seven tokens each). Prompts only;
    ``OperationSpec.to_dict`` also answers the catalog endpoint, where a stable shape is the contract."""
    return json.dumps(_without_empties(value), separators=(",", ":"))


def _without_empties(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_empties(item)
            for key, item in value.items()
            if item is not None and item != [] and item != {}
        }
    if isinstance(value, list):
        return [_without_empties(item) for item in value]
    return value


def compact_observations(
    observations: list[dict[str, Any]],
    *,
    budget_chars: int,
    keep_recent_full: int = 2,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Bound observations to a total character budget: one that fits its share passes through
    unchanged; otherwise a semantic summary (``observation_summary``) when it fits; the most recent
    ``keep_recent_full`` may still be truncated in place; everything else becomes a small stub
    referencing its stable event id, so the raw result stays retrievable from the database. Returns
    ``(compacted_observations, omitted_indices)`` with 0-based input positions reduced to a stub."""
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
        # Middle step (#561): the fields the next decision needs, between a blind cut and the bare
        # stub. Not counted as omitted.
        summary = summarize_observation(obs, fair_share)
        if summary is not None:
            result[index] = summary
            remaining = max(0, remaining - len(json.dumps(summary, separators=(",", ":"))))
            continue
        if is_recent and remaining > 500:
            # Not counted as omitted: this still carries real content.
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


# What a next decision depends on, kept even when the body is dropped - tens of characters vs
# an expensive body; a stub without them reads "a github_issue happened here" and cannot stop
# the controller asking the identical question again (#218, #223).
_STUB_IDENTITY_FIELDS = (
    "goal",
    "status",
    "retryable",
    "resolved_tool",
    "operation",
    "args",
    "recommended_next_action",
    "recommended_operation",
    "already_resolved_operation",
    "event",
)
_STUB_FIELD_CHARS = 200


def _stub(obs: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(obs, separators=(",", ":"))
    stub: dict[str, Any] = {
        "tool": obs.get("tool"),
        "truncated": True,
        "original_chars": len(serialized),
    }
    for field in _STUB_IDENTITY_FIELDS:
        if field not in obs:
            continue
        value = obs[field]
        if isinstance(value, str):
            value = value[:_STUB_FIELD_CHARS]
        elif field == "args" and isinstance(value, dict):
            # The values the call ran with distinguish one call from another; kept whole or not at all -
            # half an argument list identifies nothing, and a stub is meant to be small.
            rendered = json.dumps(value, separators=(",", ":"), default=str)
            value = value if len(rendered) <= _STUB_FIELD_CHARS else None
        elif isinstance(value, dict):
            # `operation` is a full schema on a found result; its name is the part a decision needs.
            value = str(value.get("operation", ""))[:_STUB_FIELD_CHARS] or None
        elif not isinstance(value, (bool, int, float, type(None))):
            continue
        if value is not None:
            stub[field] = value
    event_id = obs.get("event_id")
    if event_id is not None:
        stub["event_id"] = event_id
        # Says what is missing and names a way back only where one exists: read_tool_result (#562)
        # pages through stored tool-call results only.
        stub["note"] = (
            f"the result is not in this prompt; it is stored in event {event_id}"
            + read_hint(obs, event_id)
        )
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
        elif isinstance(value, dict):
            # The production shape of an operation result: `result` is a dict
            # whose `output_lines` carry the bulk. Without this branch a dict
            # matched neither case below and the observation was returned at
            # full size - the budget was computed, reported, and then not
            # applied, on the controller prompt as much as the preflight one.
            shrunk[field], was_truncated = _shrink(value, remaining)
            truncated = truncated or was_truncated
            remaining -= len(json.dumps(shrunk[field], separators=(",", ":"), default=str))
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
