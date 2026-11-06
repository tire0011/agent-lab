"""Versioned prompt template library.

Prompts live in ``prompts/`` as Markdown files with YAML front-matter.  This
module loads them, validates the front-matter, and performs simple
``{{variable}}`` substitution so callers never have to do string formatting
themselves.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# Resolve the prompts directory relative to this file.
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Supported prompt roles (= file stem without extension).
PROMPT_ROLES = ("router", "planner", "developer", "tester", "context", "reviewer")


class PromptLibraryError(Exception):
    """Raised when a prompt template cannot be loaded or rendered."""


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split YAML front-matter from Markdown body.

    Returns ``(metadata_dict, body_without_front_matter)``.
    The front-matter block is optional; if absent an empty dict is returned.
    """
    front_matter: dict[str, str] = {}
    body = text

    if text.startswith("---"):
        # Find the closing ``---`` fence.
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end].strip()
            body = text[end + 4 :].lstrip("\n")
            for line in block.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    front_matter[key.strip()] = value.strip().strip('"')

    return front_matter, body


def load_prompt(role: str, prompts_dir: Path | None = None) -> str:
    """Load the raw Markdown template for *role* (e.g. ``"router"``).

    Parameters
    ----------
    role:
        One of ``PROMPT_ROLES``.
    prompts_dir:
        Override the default prompts directory (useful in tests).

    Returns
    -------
    str
        The Markdown body (front-matter stripped).

    Raises
    ------
    PromptLibraryError
        If the file does not exist or the role is unknown.
    """
    if role not in PROMPT_ROLES:
        raise PromptLibraryError(
            f"Unknown prompt role {role!r}. Available: {PROMPT_ROLES}"
        )

    base = prompts_dir or _PROMPTS_DIR
    path = base / f"{role}_prompt.md"
    if not path.exists():
        raise PromptLibraryError(f"Prompt file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    _, body = _parse_front_matter(raw)
    return body


def render_prompt(role: str, variables: dict[str, Any], prompts_dir: Path | None = None) -> str:
    """Load and render the prompt for *role* by substituting *variables*.

    Template placeholders use the ``{{variable_name}}`` syntax.  Extra
    variables are silently ignored; missing ones cause a
    :class:`PromptLibraryError`.

    Parameters
    ----------
    role:
        Prompt role (``"router"``, ``"planner"``, ``"developer"``, ``"tester"``).
    variables:
        Mapping of placeholder names to values.
    prompts_dir:
        Override the default prompts directory (useful in tests).
    """
    body = load_prompt(role, prompts_dir=prompts_dir)

    # Find all {{placeholders}} in the template.
    required = set(re.findall(r"\{\{(\w+)\}\}", body))
    missing = required - set(variables.keys())
    if missing:
        raise PromptLibraryError(
            f"Missing template variables for role {role!r}: {sorted(missing)}"
        )

    for key, value in variables.items():
        body = body.replace("{{" + key + "}}", str(value))

    return body
