"""Read-only adapters over existing data sources.

No new persistent storage is introduced here. Problems and ideas are derived
from files that already exist in the repository/runtime state:

- Problems: error entries already logged by agent.py under state/runs/*.json
  (any run whose "response" starts with "ERROR:").
- Ideas: entries under "## Ideas" in IDEAS.md, each an open-ended thought
  rather than a task; an optional trailing `` `state` `` marker (one of
  new/exploring/selected/tested/adopted/rejected) is parsed if present and
  defaults to "new" otherwise. Ideas are never a fixed roadmap or priority
  list.

``add_idea`` is the one write path in this module: it appends a new,
sanitized, state-``new`` idea under a fixed "### Inbox" subsection at the
end of "## Ideas", guarded by an exclusive file lock so concurrent
appends cannot interleave and corrupt IDEAS.md.

Both /problems and /ideas as well as the chat endpoint call these same
functions, so structured endpoints and chat answers never diverge.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path

from models import Idea, Problem

REPO_ROOT = Path(__file__).resolve().parent

RUNS_DIR = Path(os.environ.get("AGENT_RUNS_DIR", str(REPO_ROOT / "state" / "runs")))
IDEAS_PATH = Path(os.environ.get("AGENT_IDEAS_PATH", str(REPO_ROOT / "IDEAS.md")))

_IDEA_ITEM_RE = re.compile(
    r"^-\s+(?P<text>.+?)(?:\s+`(?P<state>new|exploring|selected|tested|adopted|rejected)`)?$",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(r"^(#{1,6})\s+(?P<title>.+)$")
_IDEAS_HEADING_RE = re.compile(r"^##\s+Ideas\s*$", re.IGNORECASE)
_KNOWN_STATES = {"new", "exploring", "selected", "tested", "adopted", "rejected"}

# Subsection new ideas submitted via the API are appended under. Kept as a
# single fixed location so appends are simple, predictable, and easy to
# find, rather than guessing which existing subsection an idea belongs in.
_INBOX_SECTION_TITLE = "Inbox"
_INBOX_HEADING_RE = re.compile(r"^###\s+" + re.escape(_INBOX_SECTION_TITLE) + r"\s*$", re.IGNORECASE)

MAX_IDEA_TEXT_LENGTH = 500


class InvalidIdeaTextError(ValueError):
    """Raised when submitted idea text fails validation/sanitization."""


def list_problems() -> list[Problem]:
    """Return currently known problems derived from failed run logs.

    Read errors (missing directory, unreadable/corrupt files) are treated as
    "no problems found" rather than raised, since this is a best-effort,
    read-only view over operational logs.
    """
    problems: list[Problem] = []

    if not RUNS_DIR.exists() or not RUNS_DIR.is_dir():
        return problems

    try:
        run_files = sorted(RUNS_DIR.glob("*.json"))
    except OSError:
        return problems

    for run_file in run_files:
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue

        if not isinstance(data, dict):
            continue

        response = data.get("response")
        if not isinstance(response, str) or not response.startswith("ERROR:"):
            continue

        detail = response.removeprefix("ERROR:").strip()
        problems.append(
            Problem(
                id=run_file.stem,
                source="run_log",
                summary=detail.splitlines()[0][:200] if detail else "Unknown error",
                detail=detail,
                timestamp=str(data.get("timestamp", "unknown")),
            )
        )

    return problems


def list_ideas() -> list[Idea]:
    """Return ideas parsed from the "## Ideas" section of IDEAS.md.

    Only list items inside the "## Ideas" section are returned (the
    surrounding intro/states/unknowns sections are prose, not ideas).
    "###" headings within that section become the ``section`` field. Each
    item may end with a trailing `` `state` `` marker (one of new,
    exploring, selected, tested, adopted, rejected); items without one
    default to "new". Missing or unreadable file yields an empty list
    rather than an error, matching the read-only, best-effort contract of
    this adapter.
    """
    try:
        text = IDEAS_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    return _parse_ideas(text)


def _parse_ideas(text: str) -> list[Idea]:
    """Parse ideas out of raw IDEAS.md *text* (see ``list_ideas`` for the
    exact rules). Split out so ``add_idea`` can parse its own just-written
    content directly, without a second disk read that could race with
    another concurrent writer."""
    ideas: list[Idea] = []

    current_section = ""
    in_ideas_section = False
    index = 0
    for line in text.splitlines():
        stripped = line.strip()

        if _IDEAS_HEADING_RE.match(stripped):
            in_ideas_section = True
            current_section = ""
            continue

        section_match = _SECTION_RE.match(stripped)
        if section_match:
            level = len(section_match.group(1))
            if level <= 2:
                # Any other top-level ("##") heading ends the ideas section.
                in_ideas_section = False
                continue
            if in_ideas_section:
                current_section = section_match.group("title").strip()
            continue

        if not in_ideas_section:
            continue

        item_match = _IDEA_ITEM_RE.match(stripped)
        if not item_match:
            continue

        index += 1
        state = (item_match.group("state") or "new").lower()
        if state not in _KNOWN_STATES:
            state = "new"
        ideas.append(
            Idea(
                id=f"idea-{index}",
                section=current_section or "General",
                text=item_match.group("text").strip(),
                state=state,
            )
        )

    return ideas


def _sanitize_idea_text(raw: str) -> str:
    """Validate and normalize idea text before it is written to IDEAS.md.

    Collapses internal whitespace/newlines to single spaces (so an idea is
    always exactly one Markdown list item, never a stray heading or a
    multi-line block) and strips backticks (so a submitted idea can never
    forge a trailing `` `state` `` marker or break the surrounding
    Markdown code span). Raises ``InvalidIdeaTextError`` for empty,
    whitespace-only, or too-long input.
    """
    normalized = " ".join(raw.split())
    normalized = normalized.replace("`", "'")
    if not normalized:
        raise InvalidIdeaTextError("idea text must not be empty")
    if len(normalized) > MAX_IDEA_TEXT_LENGTH:
        raise InvalidIdeaTextError(
            f"idea text must be at most {MAX_IDEA_TEXT_LENGTH} characters"
        )
    # A leading "#"/"-"/">" would be interpreted as Markdown structure
    # (heading, list marker, blockquote) rather than plain text once
    # written as "- <text>", so strip any such leading markers.
    normalized = re.sub(r"^[#\->\s]+", "", normalized).strip()
    if not normalized:
        raise InvalidIdeaTextError("idea text must not be empty")
    return normalized


def add_idea(raw_text: str) -> Idea:
    """Append a new idea (state ``new``) to the "## Ideas" / "### Inbox"
    section of IDEAS.md and return it.

    Uses an exclusive advisory file lock (``fcntl.flock``) around the
    read-modify-write so concurrent requests append one at a time instead
    of racing and corrupting the file. Text is sanitized first so a
    submitted idea can never inject a fake heading, state marker, or
    multi-line content into the document. The returned ``Idea`` is parsed
    from the content this call just wrote (still under the lock), never
    from a fresh disk read, so a concurrent writer appending immediately
    afterwards cannot make this call report the wrong idea.
    """
    text = _sanitize_idea_text(raw_text)

    IDEAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # "a+" creates the file if missing without truncating an existing one;
    # the lock is acquired on this same handle before reading, so no other
    # writer can observe or produce an interleaved partial write.
    with open(IDEAS_PATH, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0)
            original = handle.read()
            updated = _insert_idea_into_inbox(original, text)
            handle.seek(0)
            handle.truncate()
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
            ideas = _parse_ideas(updated)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

    if not ideas or ideas[-1].text != text:
        # Should be unreachable given the insert logic below always adds
        # the new idea at the end of the "## Ideas" section (i.e. last in
        # document order); guard defensively rather than silently
        # returning a mismatched idea.
        raise RuntimeError("failed to append idea to IDEAS.md")
    return ideas[-1]


def _insert_idea_into_inbox(original: str, text: str) -> str:
    """Return *original* with a new "- <text> `new`" bullet appended under
    an "### Inbox" subsection at the end of the "## Ideas" section,
    creating that subsection if it does not already exist."""
    lines = original.splitlines()
    bullet = f"- {text} `new`"

    ideas_start = None
    for i, line in enumerate(lines):
        if _IDEAS_HEADING_RE.match(line.strip()):
            ideas_start = i
            break

    if ideas_start is None:
        # No "## Ideas" section yet: create one at the end of the file.
        prefix = original if original.endswith("\n") or not original else original + "\n"
        if prefix and not prefix.endswith("\n\n") and prefix.strip():
            prefix += "\n"
        return prefix + f"## Ideas\n\n### {_INBOX_SECTION_TITLE}\n\n{bullet}\n"

    # Find the end of the "## Ideas" section: the next line at heading
    # level <= 2, or end of file.
    ideas_end = len(lines)
    for i in range(ideas_start + 1, len(lines)):
        match = _SECTION_RE.match(lines[i].strip())
        if match and len(match.group(1)) <= 2:
            ideas_end = i
            break

    inbox_start = None
    for i in range(ideas_start + 1, ideas_end):
        if _INBOX_HEADING_RE.match(lines[i].strip()):
            inbox_start = i
            break

    if inbox_start is not None:
        # Existing "### Inbox" subsection: append after its last non-blank
        # line, before the next subsection (or end of the Ideas section).
        inbox_end = ideas_end
        for i in range(inbox_start + 1, ideas_end):
            match = _SECTION_RE.match(lines[i].strip())
            if match:
                inbox_end = i
                break
        insert_at = inbox_end
        while insert_at > inbox_start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        new_lines = lines[:insert_at] + [bullet] + lines[insert_at:]
        return "\n".join(new_lines) + "\n"

    # No "### Inbox" subsection yet: create it at the end of the Ideas
    # section, right before the next "##" heading (or end of file).
    insert_at = ideas_end
    while insert_at > ideas_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    block = ["", f"### {_INBOX_SECTION_TITLE}", "", bullet]
    new_lines = lines[:insert_at] + block + lines[insert_at:]
    return "\n".join(new_lines) + "\n"
