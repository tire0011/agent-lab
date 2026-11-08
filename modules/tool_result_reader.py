"""Paging through stored tool results: ``read_tool_result`` (#562, part 2 of #232).

Part 1 (#561) shows a large tool result as a semantic summary and names the
event that stores it in full. This read-only controller tool returns one
deterministic slice of that stored result, so the controller pulls back only
the part it needs instead of the whole result.

Access rule (exact): an event is readable only when it

- belongs to the **same session** as the caller,
- has ``role == "tool"``, and
- has one of the tool-result kinds the orchestrator writes for a tool call
  (``TOOL_RESULT_KINDS``).

Anything else - a user, assistant or system event, another session's event, a
non-result tool event such as ``goal_truncated``, or an unknown id - gets the
same refusal without content, so a refusal does not reveal whether the id
exists. Only the event's ``content`` column is read; never its metadata and
never another table.
"""

from __future__ import annotations

from typing import Any, Protocol

TOOL_READ_TOOL_RESULT = "read_tool_result"

# The ``kind`` values the orchestrator writes for the stored result of a tool
# call (``kind = observation["tool"]``, or the action itself for experiments).
# ``read_tool_result`` is deliberately absent: its own results are copies of a
# slice, and paging through them would only re-read what is already readable.
TOOL_RESULT_KINDS: frozenset[str] = frozenset(
    {
        "find_tool",
        "operation",
        "memory_search",
        "knowledge_collector",
        "delegate",
        "command",
        "python_run",
        "new_experiment",
    }
)

# Smallest cap, so a starved budget still pages forward instead of stalling.
MIN_LIMIT_CHARS = 200
# Room left in the observation budget for the envelope around the slice.
_ENVELOPE_CHARS = 400

REFUSAL_NOT_READABLE = "tool_result_not_readable"
REFUSAL_INVALID_ARGUMENTS = "invalid_read_tool_result_arguments"


class _ToolResultSource(Protocol):
    def tool_result_content(
        self, session_id: str, event_id: int, kinds: frozenset[str]
    ) -> str | None: ...


def limit_cap(observation_chars: int | None) -> int:
    """The largest slice one read may return: the observation budget, less the envelope."""
    if observation_chars is None:
        return 4000
    return max(MIN_LIMIT_CHARS, int(observation_chars) - _ENVELOPE_CHARS)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def normalize_arguments(
    decision: dict[str, Any], cap: int
) -> tuple[dict[str, int] | None, str | None]:
    """``(arguments, error)``: the effective ``event_id``/``offset``/``limit``.

    ``limit`` defaults to and is reduced to ``cap``, so a read above the cap and
    a read at the cap are the same range - for the reader and for the loop guard.
    """
    event_id = _as_int(decision.get("event_id"))
    if event_id is None or event_id <= 0:
        return None, "event_id must be a positive integer id of a stored tool result"
    raw_offset = decision.get("offset", 0)
    offset = 0 if raw_offset in (None, "") else _as_int(raw_offset)
    if offset is None or offset < 0:
        return None, "offset must be a non-negative integer"
    raw_limit = decision.get("limit")
    limit = cap if raw_limit in (None, "") else _as_int(raw_limit)
    if limit is None or limit <= 0:
        return None, "limit must be a positive integer"
    return {"event_id": event_id, "offset": offset, "limit": min(limit, cap)}, None


def _refusal(event_id: Any, error: str, reason: str) -> dict[str, Any]:
    return {
        "tool": TOOL_READ_TOOL_RESULT,
        "event_id": event_id,
        "error": error,
        "reason": reason,
        "retryable": False,
        "recovery": (
            "read_tool_result only reads tool results stored in this session; use an "
            "event_id shown on a tool observation of this run"
        ),
    }


def read_tool_result(
    source: _ToolResultSource,
    session_id: str,
    decision: dict[str, Any],
    *,
    cap: int,
) -> dict[str, Any]:
    """Return one slice of a stored tool result of ``session_id``, or a refusal."""
    arguments, error = normalize_arguments(decision, cap)
    if arguments is None:
        refusal = _refusal(decision.get("event_id"), REFUSAL_INVALID_ARGUMENTS, str(error))
        refusal["recovery"] = "call read_tool_result with event_id, offset and limit as integers"
        return refusal
    event_id = arguments["event_id"]
    content = (
        source.tool_result_content(str(session_id or ""), event_id, TOOL_RESULT_KINDS)
        if session_id
        else None
    )
    if content is None:
        return _refusal(
            event_id,
            REFUSAL_NOT_READABLE,
            "no stored tool result with this event_id exists in this session",
        )
    offset, limit = arguments["offset"], arguments["limit"]
    total = len(content)
    chunk = content[offset : offset + limit]
    end = offset + len(chunk)
    return {
        "tool": TOOL_READ_TOOL_RESULT,
        "event_id": event_id,
        "offset": offset,
        "limit": limit,
        "total_chars": total,
        "content": chunk,
        "complete": end >= total,
        "next_offset": end if end < total else None,
    }


def read_hint(observation: dict[str, Any], event_id: Any) -> str:
    """The note suffix pointing at ``read_tool_result``, only for a readable result."""
    if str(observation.get("tool") or "") not in TOOL_RESULT_KINDS:
        return ""
    return f"; read ranges of it with {TOOL_READ_TOOL_RESULT}(event_id={event_id}, offset=0)"


def duplicate_read_guidance(arguments: dict[str, Any]) -> dict[str, Any]:
    """What a refused re-read of the same range says to do instead.

    Reading another range of the same event is progress, so the recovery names
    the next range rather than sending the controller to another tool.
    """
    event_id = arguments.get("event_id")
    offset = max(0, _as_int(arguments.get("offset")) or 0)
    limit = max(0, _as_int(arguments.get("limit")) or 0)
    next_offset = offset + limit
    return {
        "event_id": event_id,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
        "recovery": (
            f"this range of event {event_id} was already read; read the next range "
            f"with read_tool_result(event_id={event_id}, offset={next_offset}, "
            f"limit={limit}) instead of reading the same range again"
        ),
    }
