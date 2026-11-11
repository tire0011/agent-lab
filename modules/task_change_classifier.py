"""Conservative classification for free-form tasks and shell commands.

This module is intentionally not connected to command execution yet.  Its
contract is fail-closed: a command is read-only only when every command in it
matches the small allowlist below.
"""

from __future__ import annotations

import re
import shlex
from typing import Literal

CommandClassification = Literal["read_only", "mutating"]
TaskClassification = Literal["question", "change"]

_SIMPLE_READ_ONLY = {"ls", "cat", "df", "free"}
_READ_ONLY_GIT = {"status", "log", "diff", "show"}
_QUESTION_START = re.compile(
    r"^(?:can|could|did|do|does|has|have|how|is|are|should|what|when|where|"
    r"which|who|why|will|would)\b",
    re.IGNORECASE,
)
_READ_REQUEST_START = re.compile(
    r"^(?:check|describe|diagnose|explain|find|inspect|investigate|list|report|"
    r"review|show|summarize|tell me|verify|what is|what are)\b",
    re.IGNORECASE,
)
_CHANGE_WORD = re.compile(
    r"\b(?:add|apply|build|change|commit|create|delete|deploy|edit|fix|implement|"
    r"install|merge|modify|move|push|remove|rename|replace|restart|revert|run|"
    r"set|update|upgrade|write)\b",
    re.IGNORECASE,
)


def _shell_parts(command: str) -> list[str] | None:
    """Split on unquoted chains/pipes; reject redirects and malformed quotes."""

    parts: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char in "<>":
            return None
        if char in "|&;\n":
            # A lone '&' and empty chain components are not supported shell
            # forms here, so they fail closed below.
            width = 2 if index + 1 < len(command) and command[index + 1] == char else 1
            part = command[start:index].strip()
            if not part or char == "&" and width == 1:
                return None
            parts.append(part)
            index += width
            start = index
            continue
        index += 1

    if quote or escaped:
        return None
    final = command[start:].strip()
    if not final:
        return None
    parts.append(final)
    return parts


def _gh_api_is_read_only(argv: list[str]) -> bool:
    if len(argv) < 2 or argv[:2] != ["gh", "api"]:
        return False
    method = "GET"
    index = 2
    while index < len(argv):
        arg = argv[index]
        if arg in {"-f", "-F", "--field", "--raw-field"}:
            return False
        if arg.startswith(("-f", "-F", "--field=", "--raw-field=")):
            return False
        if arg in {"-X", "--method"}:
            if index + 1 >= len(argv):
                return False
            method = argv[index + 1]
            index += 2
            continue
        if arg.startswith("-X") and len(arg) > 2:
            method = arg[2:]
        elif arg.startswith("--method="):
            method = arg.partition("=")[2]
        index += 1
    return method.upper() == "GET"


def _part_is_read_only(part: str) -> bool:
    # Command substitution can hide arbitrary commands inside one shlex token.
    if "$(" in part or "`" in part:
        return False
    try:
        argv = shlex.split(part, posix=True)
    except ValueError:
        return False
    if not argv or argv[0] == "sudo":
        return False
    if argv[0] in _SIMPLE_READ_ONLY:
        return True
    if argv[0] == "git" and len(argv) >= 2:
        if argv[1] in _READ_ONLY_GIT:
            return True
        return argv[1:] in (["branch"], ["branch", "--list"])
    return _gh_api_is_read_only(argv)


def classify_command(cmd: str) -> CommandClassification:
    """Classify *cmd*, treating anything outside the allowlist as mutating."""

    if not isinstance(cmd, str):
        return "mutating"
    parts = _shell_parts(cmd)
    if parts is None or not all(_part_is_read_only(part) for part in parts):
        return "mutating"
    return "read_only"


def classify_task(text: str) -> TaskClassification:
    """Classify clearly informational task text; ambiguity is a change."""

    if not isinstance(text, str) or not text.strip():
        return "change"
    normalized = " ".join(text.split())
    if _CHANGE_WORD.search(normalized):
        return "change"
    if normalized.endswith("?") and _QUESTION_START.match(normalized):
        return "question"
    if _READ_REQUEST_START.match(normalized):
        return "question"
    return "change"
