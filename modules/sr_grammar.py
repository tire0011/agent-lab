"""GBNF grammar for SEARCH/REPLACE edit answers (#694).

llama-server can constrain sampling to a GBNF grammar (the ``grammar`` field
of a completion request). The local coding worker sends the grammar built
here when its answer format is SEARCH/REPLACE, so the 7B model *cannot*
answer with prose, Markdown fences, a path it was not given, or a block
without its ``>>>>>>> REPLACE`` line. Such answers used to end a correction
round as ``edit_format`` / ``no_diff`` (#679).

The grammar accepts one or more blocks, optionally separated by one blank
line, and nothing else::

    path
    <<<<<<< SEARCH
    ...lines...
    =======
    ...lines...
    >>>>>>> REPLACE

``path`` is one of the allowed paths (a literal alternative each). A body line
is any line that does not start with seven ``<``, ``=`` or ``>`` characters,
so a marker can never appear inside SEARCH or REPLACE text. The grammar is
not recursive.

Env var ``NEXUS_LOCAL_SR_GRAMMAR`` (default on; ``0``/``false``/``no``/``off``
turns it off) is read by :func:`sr_grammar_enabled`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import PurePosixPath

ENV_SR_GRAMMAR = "NEXUS_LOCAL_SR_GRAMMAR"
_FALSE_VALUES = {"0", "false", "no", "off"}
SEARCH_MARKER = "<<<<<<< SEARCH"
DIVIDER_MARKER = "======="
REPLACE_MARKER = ">>>>>>> REPLACE"
# A marker line is seven of one of these characters at the start of a line.
_MARKER_CHARS = ("<", "=", ">")
_MARKER_LENGTH = 7


def sr_grammar_enabled() -> bool:
    """``NEXUS_LOCAL_SR_GRAMMAR`` (default on)."""
    return os.environ.get(ENV_SR_GRAMMAR, "1").strip().lower() not in _FALSE_VALUES


def usable_path(path: str) -> bool:
    """A repository-relative POSIX path the edit parser would accept on one line."""
    if not path or path != path.strip() or any(c in path for c in "\r\n\\\0"):
        return False
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts or "." in posix.parts:
        return False
    # The path line must not itself look like a marker line.
    return not any(path.startswith(c * _MARKER_LENGTH) for c in _MARKER_CHARS)


def _literal(text: str) -> str:
    """A GBNF string literal for *text*."""
    out = []
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _class_char(char: str) -> str:
    # llama.cpp's GBNF parser knows only these escapes inside a class.
    return {"\\": "\\\\", "]": "\\]"}.get(char, char)


def build_sr_grammar(allowed_paths: Iterable[str]) -> str:
    """The GBNF grammar for one or more SEARCH/REPLACE blocks on *allowed_paths*.

    Duplicates and unusable paths (absolute, ``..``, newlines, backslashes) are
    dropped; :class:`ValueError` if no usable path is left, because a grammar
    without a path would accept no answer at all.
    """
    paths = sorted({p for p in allowed_paths if isinstance(p, str) and usable_path(p)})
    if not paths:
        raise ValueError("build_sr_grammar needs at least one usable allowed path")
    marker_set = "".join(_class_char(c) for c in _MARKER_CHARS)
    lines = [
        'root ::= block ("\\n"? block)*',
        "block ::= path " + _literal("\n" + SEARCH_MARKER + "\n") + " body "
        + _literal(DIVIDER_MARKER + "\n") + " body " + _literal(REPLACE_MARKER + "\n"),
        "path ::= " + " | ".join(_literal(p) for p in paths),
        "body ::= line*",
        # A line: empty, or starting with a non-marker character, or a short
        # run (1-6) of one marker character not followed by another one.
        "line ::= (plain | " + " | ".join(f"run{i}" for i in range(len(_MARKER_CHARS))) + ')? "\\n"',
        f"plain ::= [^{marker_set}\\n] rest",
        "rest ::= [^\\n]*",
    ]
    for i, char in enumerate(_MARKER_CHARS):
        runs = " | ".join(_literal(char * n) for n in range(1, _MARKER_LENGTH))
        lines.append(f"run{i} ::= ({runs}) ([^{_class_char(char)}\\n] rest)?")
    return "\n".join(lines) + "\n"
