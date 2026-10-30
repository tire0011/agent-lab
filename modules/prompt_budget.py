"""Configurable prompt/token budget for the persistent Nexus controller.

The controller resends its observations every turn, so an unbounded prompt
grows until llama-server is killed by the OOM killer. This computes a
character budget from the model's context window, its reserved output and
host memory, then compacts observations deterministically to fit it.

Kept dependency-free so it stays unit testable.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
    return _read_meminfo_field(meminfo_path, "MemAvailable")


def read_swap_free_kb(meminfo_path: str = DEFAULT_MEMINFO_PATH) -> int | None:
    """Best-effort read of ``SwapFree`` (kB) from ``/proc/meminfo``.

    Low/absent swap is an additional low-memory signal: a host relying on
    swap to absorb an inference spike is already close to OOM pressure even
    if ``MemAvailable`` alone looks tolerable. Meaningful only when swap is
    actually configured; see ``read_swap_total_kb``.
    """
    return _read_meminfo_field(meminfo_path, "SwapFree")


def read_swap_total_kb(meminfo_path: str = DEFAULT_MEMINFO_PATH) -> int | None:
    """Best-effort read of ``SwapTotal`` (kB) from ``/proc/meminfo``.

    A host without swap configured always reports ``SwapFree: 0``, which is
    not itself a low-memory signal; callers must check ``SwapTotal > 0``
    before treating a low ``SwapFree`` reading as pressure.
    """
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
    """One budget computation, used both for prompt sizing and for logging."""

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
            "swap_free_kb": self.swap_free_kb,
            "swap_total_kb": self.swap_total_kb,
            "inference_headroom_kb": self.inference_headroom_kb,
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
    swap_free_kb: int | None = None,
    swap_total_kb: int | None = None,
    mem_limit_mb: int | None = None,
    inference_headroom_mb: int | None = None,
    kb_per_input_token: int | None = None,
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

    if inference_headroom_mb is None:
        inference_headroom_mb = _env_int("NEXUS_LOCAL_MODEL_INFERENCE_HEADROOM_MB", 512)
    inference_headroom_kb = max(0, inference_headroom_mb) * 1024

    if kb_per_input_token is None:
        kb_per_input_token = _env_int("NEXUS_LOCAL_MODEL_KB_PER_TOKEN", 8)
    kb_per_input_token = max(1, kb_per_input_token)

    context_tokens_available = max(256, model_context_tokens - max(0, reserved_output_tokens))
    context_budget_chars = context_tokens_available * chars_per_token

    low_memory = False
    memory_budget_chars: int | None = None
    if mem_available_kb is not None:
        # The model is already resident: its bytes are in `used`, not in
        # `MemAvailable`, and its KV cache is sized up front from the
        # configured context, not per request. Adding `mem_limit_kb` here
        # asked the host for room to load the model a second time, which
        # collapsed the budget to its floor on every host where it was
        # running (#223). A prompt costs its own tokens and nothing else.
        required_kb = inference_headroom_kb
        surplus_kb = mem_available_kb - required_kb
        if surplus_kb <= 0:
            low_memory = True
            memory_budget_chars = 0
        else:
            memory_tokens_available = surplus_kb // kb_per_input_token
            memory_budget_chars = int(memory_tokens_available) * chars_per_token
        # A host with no swap reports ``SwapFree: 0`` forever, so exhausted
        # swap is only pressure when swap exists and RAM is thin too.
        swap_configured = swap_total_kb is not None and swap_total_kb > 0
        ram_pressure = surplus_kb < inference_headroom_kb
        if swap_configured and swap_free_kb is not None and swap_free_kb < inference_headroom_kb and ram_pressure:
            low_memory = True
            memory_budget_chars = 0


    if memory_budget_chars is None:
        effective_budget_chars = context_budget_chars
    else:
        effective_budget_chars = min(context_budget_chars, memory_budget_chars)
    effective_budget_chars = max(500, effective_budget_chars)

    # Advisory only. A resident model can still serve a request under
    # pressure, so this must never become a hard routing gate.
    safe_local_budget_chars = 0 if low_memory else effective_budget_chars

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
        swap_free_kb=swap_free_kb,
        swap_total_kb=swap_total_kb,
        inference_headroom_kb=inference_headroom_kb,
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


def prompt_json(value: Any) -> str:
    """Serialise for a model prompt, without the parts that carry no meaning.

    ``"arguments":[]`` and ``"default":null`` say what their absence says,
    and at seven tokens a second each is paid for in wall-clock time.

    Prompts only. ``OperationSpec.to_dict`` also answers the catalog
    endpoint, where a stable shape is the contract.
    """
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
        summary = _semantic_summary(obs)
        serialized_summary = json.dumps(summary, separators=(",", ":"))
        if summary.get("semantic_summary") and len(serialized_summary) <= fair_share:
            result[index] = summary
            remaining = max(0, remaining - len(serialized_summary))
            continue
        if is_recent and remaining > 500:
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


_DECISION_FIELDS = (
    "goal", "title", "state", "status", "acceptance_criteria", "summary",
    "retryable", "recommended_next_action", "recommended_operation",
)


def _semantic_summary(obs: dict[str, Any]) -> dict[str, Any]:
    """Keep decision-relevant meaning while dropping bulky raw payloads."""
    summary = _stub(obs)
    summary.pop("note", None)
    meaningful = False
    for field in _DECISION_FIELDS:
        if field in obs and obs[field] not in (None, "", [], {}):
            summary[field] = obs[field]
            meaningful = True

    if obs.get("operation") == "github_issue":
        lines = obs.get("output_lines")
        if isinstance(lines, list):
            body = ""
            for line in lines:
                if not isinstance(line, str):
                    continue
                if line.startswith("issue: "):
                    try:
                        metadata = json.loads(line[7:])
                    except (json.JSONDecodeError, TypeError):
                        metadata = {}
                    for field in ("title", "state", "url", "number"):
                        if metadata.get(field) not in (None, ""):
                            summary[field] = metadata[field]
                            meaningful = True
                elif line.startswith("body: "):
                    body = line[6:]
            if body:
                intro = re.split(r"(?m)^\s*#{1,6}\s+", body, maxsplit=1)[0].strip()
                summary["goal"] = truncate_text(intro or body.strip(), 800, note="goal truncated")[0]
                meaningful = True
                match = re.search(
                    r"(?ims)^\s*#{1,6}\s+acceptance criteria\s*$\n(.*?)(?=^\s*#{1,6}\s+|\Z)",
                    body,
                )
                if match:
                    summary["acceptance_criteria"] = truncate_text(
                        match.group(1).strip(), 1600, note="criteria truncated"
                    )[0]
                    meaningful = True
    if meaningful:
        summary["semantic_summary"] = True
    return summary


# What a next decision depends on, kept even when the body is dropped. These
# cost tens of characters; the body is what is expensive. A stub without them
# reads "a github_issue happened here", which cannot stop the controller
# asking the identical question again (#218, #223). With them it reads
# "github_issue(196) succeeded, event 158" - the whole decision.
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
            # The values the call ran with, which is what makes one call
            # distinguishable from another. Kept whole or not at all: half an
            # argument list identifies nothing, and a stub is meant to be small.
            rendered = json.dumps(value, separators=(",", ":"), default=str)
            value = value if len(rendered) <= _STUB_FIELD_CHARS else None
        elif isinstance(value, dict):
            # `operation` is a full schema on a found result; its name is the
            # part a decision needs.
            value = str(value.get("operation", ""))[:_STUB_FIELD_CHARS] or None
        elif not isinstance(value, (bool, int, float, type(None))):
            continue
        if value is not None:
            stub[field] = value
    event_id = obs.get("event_id")
    if event_id is not None:
        stub["event_id"] = event_id
        # Says what is missing, never what to do about it: there is no
        # capability that fetches an event, so inviting one invites a retry.
        stub["note"] = (
            f"the result is not in this prompt; it is stored in event {event_id}"
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
