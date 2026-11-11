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


# --- git -------------------------------------------------------------------
#
# Each read-only subcommand has its own option allowlist.  Rules that keep the
# allowlist sound:
#
# * Only full option names are listed.  Git accepts unique abbreviations of
#   long options (``--out=f`` means ``--output=f``), so anything not listed
#   verbatim is rejected.
# * Only options that never consume the *next* token are listed.  Options that
#   take a value are allowed only in their attached form (``--format=...``,
#   ``-n5``).  Otherwise ``git log --grep -- --output=x`` would make git read
#   ``--`` as the pattern and ``--output=x`` as an option.
# * Options that write files (``--output``) or execute helpers (``--ext-diff``,
#   ``--textconv``, ``--show-signature`` runs gpg) are never listed.
#
# Repository configuration (``diff.external``, textconv attributes, pagers,
# fsmonitor) can still make git run programs; that is environment state, not
# something the command text controls, and is out of scope here.

_GIT_DIFF_FLAGS = frozenset(
    {
        "-p", "-u", "--patch", "-s", "--no-patch", "--raw", "--stat",
        "--shortstat", "--numstat", "--dirstat", "--summary", "--name-only",
        "--name-status", "--check", "--full-index", "--binary", "--no-color",
        "--color", "--word-diff", "--no-renames", "--find-renames", "-M",
        "--minimal", "--patience", "--histogram", "-w", "--ignore-all-space",
        "-b", "--ignore-space-change", "--ignore-blank-lines", "-R",
        "--relative", "--no-relative", "--abbrev", "--no-ext-diff",
        "--no-textconv", "--exit-code", "--quiet", "-z",
    }
)
_GIT_DIFF_VALUED = frozenset(
    {
        "--stat", "--dirstat", "--unified", "--color", "--word-diff",
        "--diff-filter", "--abbrev", "--find-renames", "--diff-algorithm",
        "--relative",
    }
)
_GIT_LOG_FLAGS = _GIT_DIFF_FLAGS | frozenset(
    {
        "--oneline", "--graph", "--decorate", "--no-decorate", "--all",
        "--branches", "--tags", "--remotes", "--reverse", "--first-parent",
        "--no-merges", "--merges", "--follow", "--abbrev-commit",
        "--no-abbrev-commit", "--date-order", "--topo-order", "--left-right",
        "--cherry-pick", "--boundary", "--source", "--full-history",
        "--simplify-merges", "--walk-reflogs", "-g", "--parents",
        "--children", "--name-only", "-i", "--regexp-ignore-case",
        "--all-match", "--invert-grep", "-E", "--extended-regexp", "-F",
        "--fixed-strings", "--no-notes", "--mailmap", "--use-mailmap",
    }
)
_GIT_LOG_VALUED = _GIT_DIFF_VALUED | frozenset(
    {
        "--format", "--pretty", "--date", "--author", "--committer", "--grep",
        "--since", "--after", "--until", "--before", "--max-count", "--skip",
        "--decorate", "--branches", "--tags", "--remotes",
    }
)
_GIT_STATUS_FLAGS = frozenset(
    {
        "-s", "--short", "-b", "--branch", "--porcelain", "--long", "-v",
        "--verbose", "-u", "--untracked-files", "--ignored",
        "--ignore-submodules", "-z", "--column", "--no-column",
        "--ahead-behind", "--no-ahead-behind", "--renames", "--no-renames",
        "--show-stash",
    }
)
_GIT_STATUS_VALUED = frozenset(
    {"--porcelain", "--untracked-files", "--ignored", "--ignore-submodules"}
)
_GIT_STATUS_ATTACHED = re.compile(r"^-u(?:no|normal|all)$")
_GIT_LOG_ATTACHED = re.compile(r"^-(?:\d+|n\d+|U\d+)$")
_GIT_DIFF_ATTACHED = re.compile(r"^-U\d+$")

_GIT_OPTIONS = {
    "status": (_GIT_STATUS_FLAGS, _GIT_STATUS_VALUED, _GIT_STATUS_ATTACHED),
    "diff": (
        _GIT_DIFF_FLAGS | {"--cached", "--staged", "--merge-base", "--no-index"},
        _GIT_DIFF_VALUED,
        _GIT_DIFF_ATTACHED,
    ),
    "log": (_GIT_LOG_FLAGS, _GIT_LOG_VALUED, _GIT_LOG_ATTACHED),
    "show": (_GIT_LOG_FLAGS, _GIT_LOG_VALUED, _GIT_LOG_ATTACHED),
}


def _git_option_allowed(arg: str, subcommand: str) -> bool:
    flags, valued, attached = _GIT_OPTIONS[subcommand]
    if arg in flags or attached.match(arg):
        return True
    name, sep, _value = arg.partition("=")
    return bool(sep) and name in valued


def _git_is_read_only(args: list[str]) -> bool:
    if not args:
        return False
    subcommand, rest = args[0], args[1:]
    if subcommand == "branch":
        return rest in ([], ["--list"])
    if subcommand not in _GIT_OPTIONS:
        return False
    for index, arg in enumerate(rest):
        if arg == "--":
            # Everything after a standalone ``--`` is a pathspec.  It is safe
            # only because no allowed option consumes the following token.
            return True
        if arg.startswith("-") and not _git_option_allowed(arg, subcommand):
            return False
    return True


# --- gh api ----------------------------------------------------------------
#
# ``gh api`` sends GET by default but switches to POST as soon as fields
# (-f/-F) or a request body (--input) are present.  ``--cache`` writes a local
# response cache.  Only the options below are accepted; everything else,
# including those, fails closed.

_GH_API_BOOL = frozenset(
    {"--paginate", "--slurp", "-i", "--include", "--silent", "--verbose"}
)
# Options that take exactly one value, mapped to their canonical long name.
_GH_API_VALUED = {
    "-X": "--method",
    "--method": "--method",
    "-H": "--header",
    "--header": "--header",
    "-q": "--jq",
    "--jq": "--jq",
    "-t": "--template",
    "--template": "--template",
    "-p": "--preview",
    "--preview": "--preview",
    "--hostname": "--hostname",
}
# A header could carry e.g. ``X-HTTP-Method-Override: POST``; allow only
# headers that cannot change the request semantics.
_GH_API_SAFE_HEADERS = frozenset({"accept", "x-github-api-version"})


def _gh_api_value_ok(option: str, value: str) -> bool:
    if option == "--method":
        return value.upper() == "GET"
    if option == "--header":
        name, sep, _rest = value.partition(":")
        return bool(sep) and name.strip().lower() in _GH_API_SAFE_HEADERS
    return True


def _gh_api_is_read_only(argv: list[str]) -> bool:
    if argv[:2] != ["gh", "api"]:
        return False
    endpoints: list[str] = []
    index = 2
    while index < len(argv):
        arg = argv[index]
        index += 1
        if not arg.startswith("-"):
            endpoints.append(arg)
            continue
        if arg in _GH_API_BOOL:
            continue
        if arg in _GH_API_VALUED:
            if index >= len(argv):
                return False
            option, value = _GH_API_VALUED[arg], argv[index]
            index += 1
        elif arg.startswith("--") and "=" in arg:
            name, _sep, value = arg.partition("=")
            if not name.startswith("--") or name not in _GH_API_VALUED:
                return False
            option = _GH_API_VALUED[name]
        elif len(arg) > 2 and arg[:2] in _GH_API_VALUED:
            # Attached short form such as ``-XGET``.  ``-X=POST`` yields the
            # value ``=POST`` and is rejected by the value check.
            option, value = _GH_API_VALUED[arg[:2]], arg[2:]
        else:
            # Unknown flags, ``--``, -f/-F/--field/--raw-field, --input,
            # --cache and combined short flags all land here.
            return False
        if not _gh_api_value_ok(option, value):
            return False
    if len(endpoints) != 1:
        return False
    # The GraphQL endpoint is always POSTed and can run mutations.
    return endpoints[0].strip("/").lower() != "graphql"


def _part_is_read_only(part: str) -> bool:
    # Command substitution can hide arbitrary commands inside one shlex token.
    if "$(" in part or "`" in part:
        return False
    try:
        argv = shlex.split(part, posix=True)
    except ValueError:
        return False
    if not argv:
        return False
    if argv[0] in _SIMPLE_READ_ONLY:
        return True
    if argv[0] == "git":
        return _git_is_read_only(argv[1:])
    if argv[0] == "gh":
        return _gh_api_is_read_only(argv)
    # sudo, env assignments, subshells and every unknown program.
    return False


def classify_command(cmd: str) -> CommandClassification:
    """Classify *cmd*, treating anything outside the allowlist as mutating."""

    if not isinstance(cmd, str):
        return "mutating"
    try:
        parts = _shell_parts(cmd)
        if parts is None or not all(_part_is_read_only(part) for part in parts):
            return "mutating"
    except Exception:  # pragma: no cover - defensive: any surprise fails closed
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
