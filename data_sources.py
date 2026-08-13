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

Both /problems and /ideas as well as the chat endpoint call these same
functions, so structured endpoints and chat answers never diverge.
"""

from __future__ import annotations

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
    ideas: list[Idea] = []

    try:
        text = IDEAS_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ideas

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
