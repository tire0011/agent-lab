"""Extraction of usage totals and MCP tool call traces from an agent run result."""

from typing import Any

from agents.items import ToolCallItem, ToolCallOutputItem

from modules.serialization import to_json_string


def extract_usage_totals(result: Any) -> tuple[int, int, int]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    for response in getattr(result, "raw_responses", []):
        usage = getattr(response, "usage", None)
        if usage is None:
            continue
        prompt_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        completion_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        total_tokens += int(getattr(usage, "total_tokens", 0) or 0)

    return prompt_tokens, completion_tokens, total_tokens


def extract_tool_trace(result: Any) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    by_call_id: dict[str, dict[str, Any]] = {}

    for item in getattr(result, "new_items", []):
        if isinstance(item, ToolCallItem):
            raw_item = getattr(item, "raw_item", None)
            call_id = item.call_id or f"call-{len(trace) + 1}"
            arguments = getattr(raw_item, "arguments", None)
            entry = {
                "tool_call_id": call_id,
                "tool_name": item.tool_name or getattr(raw_item, "name", "unknown"),
                "arguments": to_json_string(arguments if arguments is not None else {}),
            }
            trace.append(entry)
            by_call_id[call_id] = entry
            continue

        if isinstance(item, ToolCallOutputItem):
            call_id = item.call_id or ""
            output_text = to_json_string(getattr(item, "output", None))

            if call_id and call_id in by_call_id:
                by_call_id[call_id]["result"] = output_text
            else:
                trace.append(
                    {
                        "tool_call_id": call_id,
                        "tool_name": "unknown",
                        "arguments": "{}",
                        "result": output_text,
                    }
                )

    return trace
