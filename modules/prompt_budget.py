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
        # ``mem_limit_kb`` is what the configured local model already needs
        # just to run (weights + baseline KV cache). Anything below that
        # requirement plus an additional safety headroom means the host
        # cannot safely serve one more local inference call at all, no
        # matter how small the resulting prompt is.
        required_kb = mem_limit_kb + inference_headroom_kb
        surplus_kb = mem_available_kb - required_kb
        if surplus_kb <= 0:
            low_memory = True
            memory_budget_chars = 0
        else:
            memory_tokens_available = surplus_kb // kb_per_input_token
            memory_budget_chars = int(memory_tokens_available) * chars_per_token
        # A host with no swap configured at all always reports
        # ``SwapFree: 0``, which is not a low-memory signal by itself.
        # Only treat exhausted swap as pressure when swap actually exists
        # (``swap_total_kb > 0``) *and* RAM headroom is already thin, since
        # a host with abundant MemAvailable doesn't need swap as a cushion.
        swap_configured = swap_total_kb is not None and swap_total_kb > 0
        ram_pressure = surplus_kb < inference_headroom_kb
        if swap_configured and swap_free_kb is not None and swap_free_kb < inference_headroom_kb and ram_pressure:
            # Near-exhausted swap alongside thin RAM headroom means an
            # inference spike would have no cushion left to be absorbed by
            # the kernel.
            low_memory = True
            memory_budget_chars = 0


    if memory_budget_chars is None:
        effective_budget_chars = context_budget_chars
    else:
        effective_budget_chars = min(context_budget_chars, memory_budget_chars)
    effective_budget_chars = max(500, effective_budget_chars)

    # The "safe local" budget remains an advisory signal for callers to use
    # when compacting prompts and reporting memory pressure. The local
    # inference service may still be able to serve a request (for example
    # when the model is already mmap-loaded), so this must not itself become
    # a hard routing gate.
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

    ``"arguments":[]``, ``"default":null`` and ``"maximum":null`` say exactly
    what their absence says, and on a host processing about seven tokens per
    second every one of them is paid for in wall-clock time.

    Applied only when rendering a prompt, never to the observation or the
    catalog itself: ``OperationSpec.to_dict`` also answers
    ``GET /operations/catalog``, where a stable shape is the contract, and an
    observation that loses a key between being recorded and being read is a
    different kind of bug entirely.
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


# What a next decision depends on, kept even when the body is dropped.
#
# A stub saying only "a find_tool happened here" cannot stop the controller
# asking the identical question again: the rule it has to follow is about
# *that exact goal*, and the goal was the first thing compacted away. On a
# small box the observation budget collapses to a few hundred characters, so
# every observation becomes a stub and the controller decides each turn with
# no memory of what it just did - which is how a session resolved git_status,
# ran it, and then asked for it twice more (#218).
#
# These fields are tens of characters. The body is what is expensive.
#
# `args` is here for the same reason. On a memory-pressured VM the observation
# budget collapses to about 150 characters, so every observation becomes a
# stub - and a stub naming `github_issue` without saying *which issue* cannot
# stop a controller asking for #196 again when it already has the answer
# (#223). With `args` and `status`, the stub reads as "github_issue(196)
# succeeded, event 158", which is the whole decision.
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


# --- controller-facing observation view (#223) -----------------------------
#
# A tool observation is written once and read twice, for two different
# purposes. ``nexus_task_events`` keeps the full envelope because a diagnosis
# weeks later needs to know which grant authorised a call, which schema it was
# bound against and what the resolver measured. The next controller turn needs
# none of that: it needs to know what the call answered, so it does not ask
# again.
#
# Session 3e2e565e49134703934e59bd57ad9118 is what the difference costs. The
# controller fetched GitHub issue #196 successfully and then asked for the
# identical ``github_issue(196)`` again - the issue text was in the prompt, but
# wrapped in a tool wrapper, a grant id, a full operation schema and a nested
# result object. For a 7B local model the answer it already had was buried in
# the record of how it had been obtained.
#
# This is a view, not an edit. The persisted journal is untouched; only the
# copy rendered into the prompt is narrowed.

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

    ``{"issue_number": 196}`` and ``{"issue_number": "196"}`` are one call as
    far as the next decision is concerned: the registry coerces the string, and
    a rejected first attempt followed by a corrected one is the "previous
    transient validation attempt" that should not still be in the prompt after
    the call succeeded.
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
    view["status"] = "succeeded" if result.get("success") else "failed"
    for field in _RESULT_PAYLOAD_FIELDS:
        value = result.get(field)
        if value not in (None, [], ""):
            view[field] = value
    return view


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

    * execution metadata (grant id, already-bound operation schema, resolver
      telemetry) is dropped, and a successful operation's nested result is
      flattened to ``status`` plus its payload;
    * a failed attempt at an operation that a later call then completed with
      the same arguments is dropped, since the controller's next step is
      decided by the result, not by how many tries it took;
    * a duplicate-call refusal names the event that already answered it, so
      "you asked this before" becomes "that answer is in event 158".

    Never mutates its input: the persisted journal and the in-memory
    observation list keep the full envelope.
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
