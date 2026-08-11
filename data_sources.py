"""Read-only adapters over existing data sources.

No new persistent storage is introduced here. Problems and todos are derived
from files that already exist in the repository/runtime state:

- Problems: error entries already logged by agent.py under state/runs/*.json
  (any run whose "response" starts with "ERROR:").
- Todos: open checklist items ("- [ ] ...") already tracked in TODO.md.

Both /problems and /todos as well as the chat endpoint call these same
functions, so structured endpoints and chat answers never diverge.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from models import Problem, Todo

REPO_ROOT = Path(__file__).resolve().parent

RUNS_DIR = Path(os.environ.get("AGENT_RUNS_DIR", str(REPO_ROOT / "state" / "runs")))
TODO_PATH = Path(os.environ.get("AGENT_TODO_PATH", str(REPO_ROOT / "TODO.md")))

_TODO_ITEM_RE = re.compile(r"^-\s\[\sx?\]\s+(?P<text>.+)$", re.IGNORECASE)
_SECTION_RE = re.compile(r"^(#{1,6})\s+(?P<title>.+)$")


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


def list_todos() -> list[Todo]:
    """Return open (unchecked) todo items parsed from TODO.md.

    Missing or unreadable file yields an empty list rather than an error,
    matching the read-only, best-effort contract of this adapter.
    """
    todos: list[Todo] = []

    try:
        text = TODO_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return todos

    current_section = ""
    index = 0
    for line in text.splitlines():
        section_match = _SECTION_RE.match(line.strip())
        if section_match:
            current_section = section_match.group("title").strip()
            continue

        stripped = line.strip()
        if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
            continue

        item_match = _TODO_ITEM_RE.match(stripped)
        if not item_match:
            continue

        index += 1
        todos.append(
            Todo(
                id=f"todo-{index}",
                section=current_section or "General",
                text=item_match.group("text").strip(),
            )
        )

    return todos
