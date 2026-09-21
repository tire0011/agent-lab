"""Controller-facing views of persisted tool observations.

Raw events remain unchanged in the journal. This module prepares only the
semantic view the next controller decision needs; prompt sizing belongs to
prompt_budget.
"""

from __future__ import annotations

import json
import re
from typing import Any

_EXECUTION_METADATA_FIELDS = ("grant_event_id", "operation_schema", "metrics")
_LOOP_GUARD_EVENTS = frozenset(
    {"duplicate_tool_call", "non_progressing_cycle", "retry_reason_required"}
)
_RESULT_PAYLOAD_FIELDS = ("output_lines", "exit_code", "error_summary")
_CHUNKED_FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]:\s?(.*)$", re.DOTALL)


def _args_signature(operation: str, args: Any) -> str:
    """Return stable identity for an operation call."""
    if not isinstance(args, dict):
        return f"{operation}|{args!r}"

    normalized: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str):
            try:
                value = int(value.strip(), 10)
            except ValueError:
                pass
        normalized[str(key)] = value

    return (
        f"{operation}|"
        f"{json.dumps(normalized, sort_keys=True, separators=(',', ':'), default=str)}"
    )


def _operation_view(obs: dict[str, Any]) -> dict[str, Any]:
    view = {
        key: value
        for key, value in obs.items()
        if key not in _EXECUTION_METADATA_FIELDS
    }
    result = obs.get("result")
    if not isinstance(result, dict):
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
        view["output"] = "none"
    return view


def _reassembled(output_lines: list[Any]) -> list[Any]:
    """Join numbered line chunks back into their original semantic field."""
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
    view = {
        key: value
        for key, value in obs.items()
        if key not in _EXECUTION_METADATA_FIELDS
    }
    if obs.get("status") != "found":
        return view

    view.pop("operation", None)
    if isinstance(obs.get("resolved_arguments"), dict):
        view.pop("arguments_schema", None)
    return view


def controller_observation_view(
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prepare persisted observations for the next controller decision."""
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

        event = str(obs.get("event", ""))
        if event in _LOOP_GUARD_EVENTS:
            viewed.append(_duplicate_call_view(obs, succeeded_in))
            continue

        tool = obs.get("tool")
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

        viewed.append(obs)

    return viewed


def _superseded_later(
    observations: list[dict[str, Any]], index: int, signature: str
) -> bool:
    for obs in observations[index + 1 :]:
        if not isinstance(obs, dict) or obs.get("tool") != "operation":
            continue
        result = obs.get("result")
        if not (isinstance(result, dict) and result.get("success")):
            continue
        if _args_signature(str(obs.get("operation", "")), obs.get("args")) == signature:
            return True
    return False


def _duplicate_call_view(
    obs: dict[str, Any], succeeded_in: dict[str, Any]
) -> dict[str, Any]:
    view = dict(obs)
    operation = str(obs.get("operation", "")).strip()
    if not operation:
        return view

    event_id = succeeded_in.get(_args_signature(operation, obs.get("args")))
    if event_id is None:
        return view

    arguments = json.dumps(
        obs.get("args") or {},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    view["already_succeeded_in_event"] = event_id
    view["recovery"] = (
        f"{operation}{arguments} already succeeded in event {event_id}; "
        "use that result and choose the next action"
    )
    return view
