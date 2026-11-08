"""Semantic summaries of tool observations too large for the prompt (#561).

The middle step between a full observation and the bare stub of #227: a
small, deterministic set of the fields the next controller decision needs,
chosen per operation with a generic default. The raw result stays stored
unchanged in ``nexus_task_events``; this only shapes what the prompt shows.

``summarize_observation`` returns ``None`` when no summary fits ``max_chars``;
the caller then falls back to the stub.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from modules.runtime_execution import exit_code_of
from modules.tool_result_reader import read_hint

# Fields every summary keeps when present: what ran, with what, and how it ended.
_IDENTITY_FIELDS = (
    "tool",
    "operation",
    "args",
    "command",
    "target",
    "goal",
    "status",
    "event",
    "error",
    "error_summary",
    "resolved_tool",
    "retryable",
    "recommended_next_action",
    "recommended_operation",
    "already_resolved_operation",
    "event_id",
)
_IDENTITY_TEXT_CHARS = 300

# Caps for the summary content at scale 1. Each further attempt halves them,
# so a summary shrinks deterministically before the stub takes over.
_SCALES = (1.0, 0.5, 0.25, 0.125)
_TEXT_CHARS = 1500  # goal / acceptance criteria
_LINE_CHARS = 240
_TAIL_LINES = 15
_LIST_ITEMS = 10
_FAILING_TESTS = 20

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_GOAL_HEADINGS = ("goal", "ziel", "problem", "summary", "motivation", "context")
_ACCEPTANCE_HEADINGS = (
    "acceptance criteria",
    "acceptance",
    "akzeptanzkriterien",
    "done when",
    "definition of done",
    "test",
    "tests",
)
_FAILING_TEST = re.compile(
    r"^(?:FAIL|ERROR|FAILED)[:\s]+(\S.*?)\s*$|^(\S+::\S+)\s+(?:FAILED|ERROR)\b",
    re.MULTILINE,
)
_TEXT_RESULT_FIELDS = ("result", "output", "content", "detail")
_LIST_RESULT_FIELDS = ("results", "items", "matches", "output_lines")


def _cut(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    marker = "...[cut]"
    return text[: max(0, limit - len(marker))] + marker


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str))


def _identity(obs: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for field in _IDENTITY_FIELDS:
        if field not in obs:
            continue
        value = obs[field]
        if isinstance(value, str):
            value = _cut(value, _IDENTITY_TEXT_CHARS)
        elif field == "args" and isinstance(value, dict):
            if _size(value) > _IDENTITY_TEXT_CHARS:
                continue
        elif isinstance(value, dict):
            # `operation` is a full schema on a found find_tool result.
            value = _cut(str(value.get("operation") or value.get("name") or ""), _IDENTITY_TEXT_CHARS)
            if not value:
                continue
        elif not isinstance(value, (bool, int, float, type(None))):
            continue
        if value is not None:
            summary[field] = value
    return summary


def _lines_of(obs: dict[str, Any]) -> list[str]:
    """All text the observation carries, as lines, in order."""
    lines = obs.get("output_lines")
    if isinstance(lines, list):
        return [str(line) for line in lines]
    for field in _TEXT_RESULT_FIELDS:
        value = obs.get(field)
        if isinstance(value, str) and value:
            return value.splitlines()
        if isinstance(value, dict):
            return _lines_of(value)
    return []


# --- github_issue ---------------------------------------------------------


def _markdown_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        match = _HEADING.match(line)
        if match:
            current = match.group(1).strip().lower().rstrip(":")
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _section(sections: dict[str, str], wanted: tuple[str, ...]) -> str | None:
    for name in wanted:
        for heading, text in sections.items():
            if (heading == name or heading.startswith(name + " ")) and text:
                return text
    return None


def _issue_fields(lines: list[str]) -> tuple[dict[str, Any], str]:
    meta: dict[str, Any] = {}
    body_parts: list[str] = []
    for line in lines:
        if line.startswith("issue: "):
            try:
                parsed = json.loads(line[len("issue: "):])
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                meta.update(parsed)
            continue
        header = re.match(r"^Issue #(\d+):\s*(.*)$", line)
        if header:
            meta.setdefault("number", int(header.group(1)))
            meta.setdefault("title", header.group(2))
            continue
        state = re.match(r"^State:\s*(\S+)", line)
        if state:
            meta.setdefault("state", state.group(1))
            continue
        if line.startswith("body: "):
            body_parts.append(line[len("body: "):])
        elif line.startswith("Body: "):
            body_parts.append(line[len("Body: "):])
        elif body_parts:
            body_parts.append(line)
    return meta, "\n".join(body_parts)


def _github_issue_summary(obs: dict[str, Any], scale: float) -> dict[str, Any]:
    meta, body = _issue_fields(_lines_of(obs))
    text_chars = max(80, int(_TEXT_CHARS * scale))
    summary: dict[str, Any] = {}
    for field in ("number", "title", "state"):
        if meta.get(field) is not None:
            summary[field] = _cut(meta[field], _LINE_CHARS) if isinstance(meta[field], str) else meta[field]
    labels = meta.get("labels")
    if isinstance(labels, list) and labels:
        summary["labels"] = [
            _cut(label.get("name", label) if isinstance(label, dict) else label, 60)
            for label in labels[:_LIST_ITEMS]
        ]
    sections = _markdown_sections(body)
    goal = _section(sections, _GOAL_HEADINGS)
    if goal is None and body:
        # No headings: the opening of the body is the closest thing to a goal.
        goal = body.strip().split("\n\n", 1)[0]
    if goal:
        summary["goal"] = _cut(goal, text_chars)
    acceptance = _section(sections, _ACCEPTANCE_HEADINGS)
    if acceptance:
        summary["acceptance_criteria"] = _cut(acceptance, text_chars)
    return summary


# --- command / test output ------------------------------------------------


def _command_summary(obs: dict[str, Any], scale: float) -> dict[str, Any]:
    lines = _lines_of(obs)
    text = "\n".join(lines)
    summary: dict[str, Any] = {}
    exit_code = obs.get("exit_code")
    if exit_code is None:
        exit_code = exit_code_of(text)
    if exit_code is not None:
        summary["exit_code"] = exit_code
    failing: list[str] = []
    for match in _FAILING_TEST.finditer(text):
        name = (match.group(1) or match.group(2) or "").strip()
        if name and name not in failing:
            failing.append(name)
    if failing:
        keep = max(1, int(_FAILING_TESTS * scale))
        summary["failing_tests"] = [_cut(name, _LINE_CHARS) for name in failing[:keep]]
        if len(failing) > keep:
            summary["failing_tests_total"] = len(failing)
    meaningful = [line for line in lines if line.strip() and line.strip() != "```"]
    tail_count = max(1, int(_TAIL_LINES * scale))
    line_chars = max(60, int(_LINE_CHARS * max(scale, 0.25)))
    if meaningful:
        summary["last_lines"] = [_cut(line, line_chars) for line in meaningful[-tail_count:]]
        summary["total_lines"] = len(lines)
    return summary


# --- search / list results ------------------------------------------------


def _list_summary(obs: dict[str, Any], scale: float) -> dict[str, Any] | None:
    for field in _LIST_RESULT_FIELDS:
        items = obs.get(field)
        if isinstance(items, list):
            break
    else:
        return None
    keep = max(1, int(_LIST_ITEMS * scale))
    item_chars = max(60, int(_LINE_CHARS * max(scale, 0.25)))
    first: list[Any] = []
    for item in items[:keep]:
        if isinstance(item, dict):
            rendered = json.dumps(item, separators=(",", ":"), default=str, ensure_ascii=False)
            first.append(_cut(rendered, item_chars))
        else:
            first.append(_cut(item, item_chars))
    return {"count": len(items), f"first_{field}": first}


def _default_summary(obs: dict[str, Any], scale: float) -> dict[str, Any]:
    listed = _list_summary(obs, scale)
    name = str(obs.get("operation") or "")
    list_like = obs.get("tool") != "operation" or "list" in name or "search" in name
    if listed is not None and list_like:
        return listed
    # Generic default: an exit code if any, and the tail of the output.
    return _command_summary(obs, scale)


_Summarizer = Callable[[dict[str, Any], float], "dict[str, Any] | None"]

# Per-operation field lists; anything else gets the generic default.
SUMMARIZERS: dict[str, _Summarizer] = {
    "github_issue": _github_issue_summary,
    "command": _command_summary,
    "python_run": _command_summary,
    "run_tests": _command_summary,
    "service_logs": _command_summary,
    "memory_search": lambda obs, scale: _list_summary(obs, scale),
}


def _summarizer_for(obs: dict[str, Any]) -> _Summarizer:
    key = obs.get("operation") if obs.get("tool") == "operation" else obs.get("tool")
    return SUMMARIZERS.get(str(key or ""), _default_summary)


def summarize_observation(obs: dict[str, Any], max_chars: int) -> dict[str, Any] | None:
    """Return a semantic summary of ``obs`` within ``max_chars``, or ``None``.

    ``None`` means even the tightest summary does not fit, or there was no
    content to summarise; the caller uses the stub instead.
    """
    if not isinstance(obs, dict) or max_chars <= 0:
        return None
    original_chars = _size(obs)
    summarizer = _summarizer_for(obs)
    identity = _identity(obs)
    for scale in _SCALES:
        content = summarizer(obs, scale)
        if not content:
            return None
        summary = {**identity, "summary": content, "summarized": True, "original_chars": original_chars}
        event_id = obs.get("event_id")
        if event_id is not None:
            summary["note"] = (
                f"summary of a larger result; the full result is stored in event {event_id}"
                + read_hint(obs, event_id)
            )
        if _size(summary) <= max_chars:
            return summary
    return None
