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
import re
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


# --- controller-facing observation view (#223) -----------------------------
#
# An observation is written once and read twice. ``nexus_task_events`` keeps
# the full envelope because a diagnosis needs the grant, the schema and what
# the resolver measured. The next controller turn needs none of it - and with
# it, a 7B model could not find the issue text it already had underneath the
# record of how it was obtained (#223).
#
# A view, not an edit: the journal is untouched.

# Authorisation and telemetry: real audit fields, and never an input to
# "what should I do next".
_EXECUTION_METADATA_FIELDS = ("grant_event_id", "operation_schema", "metrics")

# A refusal, not a result. These carry the call's identity but nothing it
# returned, so they are read for the pointer they can be given.
_LOOP_GUARD_EVENTS = frozenset(
    {"duplicate_tool_call", "non_progressing_cycle", "retry_reason_required"}
)

# What a successful operation result actually says, in the order a reader
# wants it. `operation` and `success` are lifted into the outer view instead.
_RESULT_PAYLOAD_FIELDS = ("output_lines", "exit_code", "error_summary")


def _args_signature(operation: str, args: Any) -> str:
    """Identity of one operation call, tolerant of how a number was written.

    The registry coerces ``"196"`` to ``196``, so a rejected first attempt
    and its corrected retry must compare equal or the failure stays in the
    prompt after the call has succeeded.
    """
    if not isinstance(args, dict):
        return f"{operation}|{args!r}"
    normalized: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str):
            # Only a value int() actually accepts. Stripping every leading sign
            # and then converting turns "--196" into a ValueError raised while
            # building a prompt, which would end the turn over a typo.
            try:
                value = int(value.strip(), 10)
            except ValueError:
                pass
        normalized[str(key)] = value
    return f"{operation}|{json.dumps(normalized, sort_keys=True, separators=(',', ':'), default=str)}"


def _operation_view(obs: dict[str, Any]) -> dict[str, Any]:
    view = {key: value for key, value in obs.items() if key not in _EXECUTION_METADATA_FIELDS}
    result = obs.get("result")
    if not isinstance(result, dict):
        # A refused call: `error`, `reason` and `recovery` are already the
        # whole message, and there is no result to unwrap.
        return view
    view.pop("result", None)
    succeeded = bool(result.get("success"))
    view["status"] = "succeeded" if succeeded else "failed"
    for field in _RESULT_PAYLOAD_FIELDS:
        value = result.get(field)
        if value not in (None, [], ""):
            view[field] = value
    if isinstance(view.get("output_lines"), list):
        view["output_lines"] = _reassembled(view["output_lines"])
    if succeeded and not view.get("output_lines"):
        # "Succeeded, returned nothing" is an answer - a clean worktree -
        # but an empty list is stripped and then reads as a dropped one.
        view["output"] = "none"
    return view


# Operations emit a long text field as numbered slices for a line-oriented
# log: `body[1]:`, `body[2]:`, cut every 450 characters blind to words. The
# prompt wants the opposite - one field, one continuous text.
_CHUNKED_FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]:\s?(.*)$", re.DOTALL)


def _reassembled(output_lines: list[Any]) -> list[Any]:
    """Join `name[1]:`, `name[2]:` ... slices back into one `name:` line."""
    rebuilt: list[Any] = []
    pending_name: str | None = None
    pending_parts: list[str] = []

    def flush() -> None:
        nonlocal pending_name, pending_parts
        if pending_name is not None:
            rebuilt.append(f"{pending_name}: {''.join(pending_parts)}")
        pending_name, pending_parts = None, []

    for line in output_lines:
        match = _CHUNKED_FIELD.match(line) if isinstance(line, str) else None
        if match is None:
            flush()
            rebuilt.append(line)
            continue
        name, index, text = match.group(1), int(match.group(2)), match.group(3)
        if name != pending_name or index == 1:
            flush()
            pending_name = name
        pending_parts.append(text)
    flush()
    return rebuilt


def _find_tool_view(obs: dict[str, Any]) -> dict[str, Any]:
    view = {key: value for key, value in obs.items() if key not in _EXECUTION_METADATA_FIELDS}
    if obs.get("status") != "found":
        # not_found/unavailable carry the guidance the prompt rules tell the
        # controller to read - `related_operations`, `recommended_next_action`,
        # `retryable`, and what the resolver actually said. All of it stays.
        return view
    # `operation` is the full selected schema, which the router has already
    # bound this call against.
    view.pop("operation", None)
    if isinstance(obs.get("resolved_arguments"), dict):
        # The operation ran with those arguments; its result is the next
        # observation. Nothing here is left for the controller to fill in.
        view.pop("arguments_schema", None)
    return view


def controller_observation_view(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Narrow tool observations to what the next controller decision needs.

    Three reductions, in one pass so they see each other's outcome:

    * execution metadata is dropped and a successful result flattened to
      ``status`` plus its payload;
    * a failed attempt a later call completed with the same arguments is
      dropped - the next step follows from the result, not the retry count;
    * a duplicate-call refusal names the event that already answered it.

    Never mutates its input.
    """
    # Where each operation call first succeeded, by call identity.
    succeeded_in: dict[str, Any] = {}
    for obs in observations:
        if not isinstance(obs, dict) or obs.get("tool") != "operation":
            continue
        result = obs.get("result")
        if not (isinstance(result, dict) and result.get("success")):
            continue
        signature = _args_signature(str(obs.get("operation", "")), obs.get("args"))
        succeeded_in.setdefault(signature, obs.get("event_id"))

    viewed: list[dict[str, Any]] = []
    for index, obs in enumerate(observations):
        if not isinstance(obs, dict):
            viewed.append(obs)
            continue
        tool = obs.get("tool")

        # Checked before the tool branches: a refused operation call carries
        # `tool: "operation"` too, but it has no result to unwrap - what it
        # needs is a pointer to the one that does.
        event = str(obs.get("event", ""))
        if event in _LOOP_GUARD_EVENTS:
            viewed.append(_duplicate_call_view(obs, succeeded_in))
            continue

        if tool == "operation":
            signature = _args_signature(str(obs.get("operation", "")), obs.get("args"))
            result = obs.get("result")
            failed = obs.get("error") is not None or not (
                isinstance(result, dict) and result.get("success")
            )
            if failed and _superseded_later(observations, index, signature):
                continue
            viewed.append(_operation_view(obs))
            continue

        if tool == "find_tool":
            viewed.append(_find_tool_view(obs))
            continue

        # command, python_run, memory_search, delegate and anything added
        # later pass through unchanged. Compaction that guessed at the shape of
        # a tool it does not know would be a worse failure than a long prompt.
        viewed.append(obs)
    return viewed


def _superseded_later(observations: list[dict[str, Any]], index: int, signature: str) -> bool:
    """Did a later call complete this exact operation successfully?

    Only *later*: a failure after the success is new information (the
    capability stopped working), not a superseded attempt.
    """
    for obs in observations[index + 1:]:
        if not isinstance(obs, dict) or obs.get("tool") != "operation":
            continue
        result = obs.get("result")
        if not (isinstance(result, dict) and result.get("success")):
            continue
        if _args_signature(str(obs.get("operation", "")), obs.get("args")) == signature:
            return True
    return False


def _duplicate_call_view(obs: dict[str, Any], succeeded_in: dict[str, Any]) -> dict[str, Any]:
    """Point a refusal at the result that already answered the question.

    ``duplicate_tool_call`` on its own says only that a repeat was refused;
    ``recovery`` then tells the controller to pick something else without
    saying why it does not need this one. Naming the event turns the refusal
    into the answer.
    """
    view = dict(obs)
    operation = str(obs.get("operation", "")).strip()
    if not operation:
        return view
    event_id = succeeded_in.get(_args_signature(operation, obs.get("args")))
    if event_id is None:
        return view
    arguments = json.dumps(obs.get("args") or {}, sort_keys=True, separators=(",", ":"), default=str)
    view["already_succeeded_in_event"] = event_id
    view["recovery"] = (
        f"{operation}{arguments} already succeeded in event {event_id}; "
        "use that result and choose the next action"
    )
    return view
