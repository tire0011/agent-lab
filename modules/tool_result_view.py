"""Semantic summaries of oversized tool results, and paging back into them (#232).

Between "the whole result" and "a bare stub" sits the rung this module adds:
when a result does not fit the controller prompt, it is reduced to the
fields the next decision needs - for an issue its title, state, goal and
acceptance criteria; for a log its head, its tail and the lines that report
failures. The stub stays the last resort, for when not even that fits.

The full result stays in ``nexus_task_events``. ``read_tool_result`` slices
it back by event id, by named section or by line range, so a controller that
needs more asks for that part only.

Both views are built from the same document model (``result_document``), so
the section names a summary advertises are exactly the ones the reader
accepts. Everything here is a pure function of the stored observation:
deterministic and read-only.

Kept dependency-free (bar the chunk reassembly it shares with the controller
view) so it stays unit testable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from modules.controller_observations import _reassembled

TOOL_READ_TOOL_RESULT = "read_tool_result"

DEFAULT_SLICE_LINES = 40
MAX_SLICE_LINES = 200
# One slice must fit the observation room of a small local context window
# alongside the rest of the prompt; a page is a page, not the payload again.
MAX_SLICE_CHARS = 4000
# Fewer lines than this have no structure worth summarising: the shrink and
# stub rungs handle them as before.
_MIN_SUMMARY_LINES = 6

# The bulk a summary replaces; everything else on an observation is identity.
_BULK_FIELDS = frozenset(
    {"output_lines", "result", "output", "content", "stdout", "stderr", "detail", "results"}
)
_TEXT_FIELDS = ("result", "output", "stdout", "stderr", "content", "detail")
_IDENTITY_VALUE_CHARS = 300

_FIELD_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s(.*))?$", re.DOTALL)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_SIGNAL = re.compile(
    r"\b(error|errors|fail|failed|failure|failures|exception|traceback|assert\w*|"
    r"warning|fatal|panic|denied|refused|timeout|timed out)\b",
    re.IGNORECASE,
)
_GOAL_HEADINGS = re.compile(
    r"^(goal|goals|ziel|ziele|summary|zusammenfassung|problem|motivation|purpose|"
    r"context|background|description|beschreibung|why|task|aufgabe)\b",
    re.IGNORECASE,
)
_ACCEPTANCE_HEADINGS = re.compile(
    r"^(acceptance(\s+criteria)?|akzeptanzkriterien|definition of done|done when|"
    r"success criteria|expected( behaviou?r)?|requirements|anforderungen)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    start: int  # first line of the section (its marker or heading)
    end: int  # one past its last line
    level: int  # 0 for a result field, 1-6 for a markdown heading


@dataclass(frozen=True, slots=True)
class ResultDocument:
    lines: tuple[str, ...]
    sections: tuple[Section, ...]


# --------------------------------------------------------------------------
# Document model
# --------------------------------------------------------------------------


def _result_lines(obs: dict[str, Any]) -> list[Any] | None:
    lines = obs.get("output_lines")
    if isinstance(lines, list):
        return lines
    result = obs.get("result")
    if isinstance(result, dict) and isinstance(result.get("output_lines"), list):
        return result["output_lines"]
    return None


def _field_entries(obs: dict[str, Any]) -> list[tuple[str | None, str]]:
    """The observation's payload as ``(field, text)`` pairs, in stored order."""
    output_lines = _result_lines(obs)
    if output_lines is not None:
        entries: list[tuple[str | None, str]] = []
        for line in _reassembled(output_lines):
            text = line if isinstance(line, str) else json.dumps(line, default=str)
            match = _FIELD_LINE.match(text)
            if match is None:
                entries.append((None, text))
            else:
                entries.append((match.group(1), match.group(2) or ""))
        return entries

    texts = [
        (field, obs[field])
        for field in _TEXT_FIELDS
        if isinstance(obs.get(field), str) and obs[field]
    ]
    if len(texts) == 1:
        # One text payload is the document itself; a field marker would only
        # shift every line number by one.
        return [(None, texts[0][1])]
    return list(texts)


def result_document(obs: Any) -> ResultDocument:
    """Lay one stored observation out as numbered lines with named sections.

    Result fields (``issue:``, ``body:``, ``result``) are level-0 sections;
    markdown headings inside them nest below. An observation with no text
    payload is rendered as indented JSON, so every result can be paged.
    """
    if isinstance(obs, str):
        return _document_from_entries([(None, obs)])
    if not isinstance(obs, dict):
        obs = {"value": obs}
    entries = _field_entries(obs)
    if not entries:
        rendered = json.dumps(obs, indent=1, sort_keys=True, default=str)
        return _document_from_entries([(None, rendered)])
    return _document_from_entries(entries)


def _document_from_entries(entries: list[tuple[str | None, str]]) -> ResultDocument:
    lines: list[str] = []
    fields: list[tuple[str, int]] = []
    headings: list[tuple[str, int, int]] = []
    for field, text in entries:
        physical = text.splitlines() or [""]
        if field is not None:
            fields.append((field, len(lines)))
            if len(physical) == 1 and not _HEADING.match(physical[0]):
                lines.append(f"{field}: {physical[0]}")
                continue
            lines.append(f"{field}:")
        for line in physical:
            heading = _HEADING.match(line)
            if heading:
                headings.append((heading.group(2).strip(), len(lines), len(heading.group(1))))
            lines.append(line)

    total = len(lines)
    field_starts = [start for _, start in fields]
    sections: list[Section] = []
    for position, (name, start) in enumerate(fields):
        end = field_starts[position + 1] if position + 1 < len(fields) else total
        sections.append(Section(name, start, end, 0))
    for position, (name, start, level) in enumerate(headings):
        field_end = min((s for s in field_starts if s > start), default=total)
        end = field_end
        for _, other_start, other_level in headings[position + 1 :]:
            if other_start >= field_end:
                break
            if other_level <= level:
                end = other_start
                break
        sections.append(Section(name, start, end, level))
    sections.sort(key=lambda section: (section.start, section.level))
    return ResultDocument(tuple(lines), tuple(sections))


def _normalized(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().strip("#:").strip()).lower()


def find_section(document: ResultDocument, query: str) -> Section | None:
    """Exact (case-insensitive) section name first, then the first containing it."""
    wanted = _normalized(query)
    if not wanted:
        return None
    for section in document.sections:
        if _normalized(section.name) == wanted:
            return section
    for section in document.sections:
        if wanted in _normalized(section.name):
            return section
    return None


def _section_body(document: ResultDocument, section: Section) -> list[str]:
    """The section's lines without its marker or heading line."""
    first = document.lines[section.start]
    body = list(document.lines[section.start + 1 : section.end])
    if section.level == 0 and first != f"{section.name}:":
        # A one-line field carries its value on the marker line itself.
        body.insert(0, first[len(section.name) + 2 :])
    return body


# --------------------------------------------------------------------------
# Paging: read_tool_result
# --------------------------------------------------------------------------


def _int_arg(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(str(value).strip(), 10) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default


def normalize_read_arguments(decision: dict[str, Any]) -> dict[str, Any]:
    """The identity of one page: same event, section and range read the same lines.

    Used both for execution and for the loop guard's signature, so two calls
    that would return the same slice are recognised as the same call and two
    that would not are never confused.
    """
    event_id = _int_arg(decision.get("event_id"), 0)
    offset = max(0, _int_arg(decision.get("offset"), 0))
    limit = _int_arg(decision.get("limit"), DEFAULT_SLICE_LINES)
    limit = max(1, min(MAX_SLICE_LINES, limit))
    section = str(decision.get("section") or "").strip()[:200]
    return {"event_id": event_id, "section": section, "offset": offset, "limit": limit}


def page_identity(arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments of a page as carried on its observation.

    The event being read is ``result_event_id``: an observation's own
    ``event_id`` is the journal row it is stored in, set by the orchestrator,
    so the two cannot share a key.
    """
    identity = {key: value for key, value in arguments.items() if key != "event_id"}
    return {"result_event_id": arguments.get("event_id"), **identity}


def parse_stored_content(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        # A stored event is bounded, so its JSON can be cut; its text still pages.
        return content


def read_tool_result_slice(
    stored: Any,
    *,
    event_id: int,
    section: str = "",
    offset: int = 0,
    limit: int = DEFAULT_SLICE_LINES,
    max_chars: int = MAX_SLICE_CHARS,
) -> dict[str, Any]:
    """Return one deterministic page of a stored result.

    ``offset`` and ``limit`` count lines, inside ``section`` when one is
    named, else over the whole result. A page also stops at ``max_chars``;
    ``next_offset`` then says where the following page starts, or is
    ``None`` when this page reached the end.
    """
    document = result_document(stored)
    offset = max(0, int(offset))
    limit = max(1, min(MAX_SLICE_LINES, int(limit)))
    available = [s.name for s in document.sections][:40]
    view: dict[str, Any] = {
        "tool": TOOL_READ_TOOL_RESULT,
        **page_identity(
            {"event_id": event_id, "section": section, "offset": offset, "limit": limit}
        ),
    }
    if isinstance(stored, dict):
        for field in ("tool", "operation"):
            if isinstance(stored.get(field), str):
                view["source_" + field] = stored[field][:200]
        if isinstance(stored.get("args"), dict):
            view["source_args"] = stored["args"]

    if section:
        found = find_section(document, section)
        if found is None:
            view["error"] = "section_not_found"
            view["sections"] = available
            view["recovery"] = (
                "name one of the listed sections, or omit section and page by offset"
            )
            return view
        view["section"] = found.name
        scope = list(document.lines[found.start : found.end])
    else:
        scope = list(document.lines)

    total = len(scope)
    returned: list[str] = []
    used = 0
    for line in scope[offset : offset + limit]:
        room = max_chars - used
        if returned and len(line) > room:
            break
        if len(line) > room:
            marker = "...[line truncated]"
            line = line[: max(0, room - len(marker))] + marker
        returned.append(line)
        used += len(line) + 1
    end = offset + len(returned)
    view["total_lines"] = total
    view["returned_lines"] = len(returned)
    view["lines"] = returned
    view["next_offset"] = end if end < total else None
    if offset >= total:
        view["note"] = f"offset {offset} is past the end ({total} lines)"
    if not section:
        view["sections"] = available
    return view


def repeated_page_recovery(arguments: dict[str, Any]) -> str:
    """Recovery text for a page read twice: point at the next range, not elsewhere.

    The guard is right to refuse the identical page - it is already in the
    observations and a stored result does not change - but "choose another
    tool" is the wrong advice for a controller that is paging.
    """
    event_id = arguments.get("event_id")
    offset = int(arguments.get("offset") or 0)
    limit = int(arguments.get("limit") or DEFAULT_SLICE_LINES)
    section = str(arguments.get("section") or "")
    where = f" section {section!r}" if section else ""
    return (
        f"lines {offset}-{offset + limit - 1}{where} of event {event_id} were already read "
        "and are in the observations; a stored result does not change. To read further, "
        f"call read_tool_result with event_id {event_id}{' and the same section' if section else ''} "
        f"and offset {offset + limit} (or the next_offset that page reported), name another "
        "section, or continue with the next action"
    )


# --------------------------------------------------------------------------
# Rung 3: semantic summary
# --------------------------------------------------------------------------

# (characters per text field, head lines, tail lines, signal lines, chars per line)
_DETAIL_LEVELS = (
    (1500, 12, 12, 12, 240),
    (700, 6, 6, 6, 180),
    (300, 3, 3, 3, 120),
    (140, 1, 2, 2, 100),
)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def _identity(obs: dict[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key, value in obs.items():
        if key in _BULK_FIELDS or key in {"note", "truncated", "original_chars"}:
            continue
        if isinstance(value, str):
            identity[key] = value[:_IDENTITY_VALUE_CHARS]
        elif isinstance(value, (bool, int, float)) or value is None:
            identity[key] = value
        elif len(json.dumps(value, separators=(",", ":"), default=str)) <= _IDENTITY_VALUE_CHARS:
            identity[key] = value
    result = obs.get("result")
    if isinstance(result, dict):
        if "status" not in identity and "success" in result:
            identity["status"] = "succeeded" if result.get("success") else "failed"
        for field in ("exit_code", "error_summary"):
            if field not in identity and result.get(field) not in (None, ""):
                identity[field] = (
                    str(result[field])[:_IDENTITY_VALUE_CHARS]
                    if isinstance(result[field], str)
                    else result[field]
                )
    return identity


def _issue_metadata(document: ResultDocument) -> dict[str, Any] | None:
    section = next((s for s in document.sections if s.level == 0 and s.name == "issue"), None)
    if section is None:
        return None
    raw = " ".join(_section_body(document, section)).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    metadata: dict[str, Any] = {}
    if isinstance(data, dict):
        for key in ("number", "title", "state", "labels", "url"):
            if data.get(key) not in (None, "", []):
                metadata[key] = data[key]
    else:
        # Redaction can cut the line short of valid JSON (#292): read field by field.
        for key in ("title", "state"):
            match = re.search(rf'"{key}":\s*"((?:[^"\\]|\\.)*)"', raw)
            if match:
                metadata[key] = json.loads(f'"{match.group(1)}"')
        number = re.search(r'"number":\s*(\d+)', raw)
        if number:
            metadata["number"] = int(number.group(1))
    return metadata or None


def _first_paragraph(lines: list[str]) -> str:
    paragraph: list[str] = []
    for line in lines:
        if _HEADING.match(line):
            if paragraph:
                break
            continue
        if not line.strip():
            if paragraph:
                break
            continue
        paragraph.append(line.strip())
    return " ".join(paragraph)


def _heading_text(document: ResultDocument, pattern: re.Pattern[str]) -> tuple[str, str] | None:
    for section in document.sections:
        if section.level and pattern.match(section.name.strip()):
            text = "\n".join(line for line in _section_body(document, section) if line.strip())
            return section.name, text
    return None


def _issue_summary(document: ResultDocument, field_chars: int) -> dict[str, Any] | None:
    metadata = _issue_metadata(document)
    if metadata is None:
        return None
    summary: dict[str, Any] = dict(metadata)
    if isinstance(summary.get("title"), str):
        summary["title"] = _clip(summary["title"], 300)
    if isinstance(summary.get("labels"), list):
        summary["labels"] = [str(label)[:60] for label in summary["labels"][:10]]
    body_section = next((s for s in document.sections if s.level == 0 and s.name == "body"), None)
    body = _section_body(document, body_section) if body_section is not None else []

    goal = _heading_text(document, _GOAL_HEADINGS)
    goal_text = goal[1] if goal is not None else _first_paragraph(body)
    if goal_text:
        summary["goal"] = _clip(goal_text, field_chars)
    acceptance = _heading_text(document, _ACCEPTANCE_HEADINGS)
    if acceptance is not None and acceptance[1]:
        summary["acceptance_criteria"] = _clip(acceptance[1], field_chars)
    summary["body_lines"] = len(body)
    return summary


def _text_summary(
    document: ResultDocument, head: int, tail: int, signals: int, line_chars: int
) -> dict[str, Any]:
    lines = document.lines
    total = len(lines)
    head_lines = [_clip(line, line_chars) for line in lines[:head]]
    tail_start = max(head, total - tail)
    tail_lines = [_clip(line, line_chars) for line in lines[tail_start:]]
    signal_lines = [
        f"{number}: {_clip(line, line_chars)}"
        for number, line in enumerate(lines)
        if head <= number < tail_start and _SIGNAL.search(line)
    ][:signals]
    summary: dict[str, Any] = {"total_lines": total, "head": head_lines}
    if signal_lines:
        summary["signal_lines"] = signal_lines
    if tail_lines:
        summary["tail"] = tail_lines
        summary["tail_starts_at_line"] = tail_start
    return summary


def semantic_summary(obs: dict[str, Any], max_chars: int) -> dict[str, Any] | None:
    """Reduce one oversized observation to what the next decision needs.

    Returns ``None`` when the observation has no structure to summarise
    (a single blob of text, no payload) or when even the tersest summary
    exceeds ``max_chars`` - the caller then falls back to the stub.
    """
    if not isinstance(obs, dict) or max_chars <= 0:
        return None
    if not _field_entries(obs):
        return None
    document = result_document(obs)
    is_issue = obs.get("operation") == "github_issue" or any(
        s.level == 0 and s.name == "issue" for s in document.sections
    )
    if not is_issue and len(document.lines) < _MIN_SUMMARY_LINES:
        return None

    original_chars = len(json.dumps(obs, separators=(",", ":"), default=str))
    identity = _identity(obs)
    event_id = obs.get("event_id")
    if event_id is not None:
        note = (
            "a summary; the full result is not in this prompt. It is stored in event "
            f"{event_id}: read_tool_result with event_id {event_id} and a section, or "
            "an offset and limit in lines, returns that part"
        )
    else:
        note = "a summary; the full result is not in this prompt"
    section_names = [s.name for s in document.sections]

    for field_chars, head, tail, signals, line_chars in _DETAIL_LEVELS:
        summary = _issue_summary(document, field_chars) if is_issue else None
        if summary is None:
            summary = _text_summary(document, head, tail, signals, line_chars)
        for section_limit in (20, 8, 0):
            view: dict[str, Any] = {
                **identity,
                "summarized": True,
                "original_chars": original_chars,
                "summary": summary,
            }
            if section_limit and section_names:
                view["sections"] = [_clip(name, 60) for name in section_names[:section_limit]]
            view["note"] = note
            if len(json.dumps(view, separators=(",", ":"), default=str)) <= max_chars:
                return view
    return None
