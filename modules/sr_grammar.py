"""GBNF grammar for SEARCH/REPLACE edit answers (#694)."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import PurePosixPath

ENV_SR_GRAMMAR = "NEXUS_LOCAL_SR_GRAMMAR"
_FALSE_VALUES = {"0", "false", "no", "off"}
SEARCH_MARKER = "<<<<<<< SEARCH"
DIVIDER_MARKER = "======="
REPLACE_MARKER = ">>>>>>> REPLACE"
_MARKER_CHARS = ("<", "=", ">")
_MARKER_LENGTH = 7


def sr_grammar_enabled() -> bool:
    """Return whether SR grammar constraints are enabled (default: on)."""
    return os.environ.get(ENV_SR_GRAMMAR, "1").strip().lower() not in _FALSE_VALUES


def usable_path(path: str) -> bool:
    """Return whether *path* is a repository-relative path accepted by the parser."""
    if not path or path != path.strip() or any(char in path for char in "\r\n\\\0"):
        return False
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts or "." in posix.parts:
        return False
    return not any(path.startswith(char * _MARKER_LENGTH) for char in _MARKER_CHARS)


def _literal(text: str) -> str:
    escaped = []
    for char in text:
        escaped.append({"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t"}.get(char, char))
    return '"' + "".join(escaped) + '"'


def _class_char(char: str) -> str:
    return {"\\": "\\\\", "]": "\\]"}.get(char, char)


def build_sr_grammar(allowed_paths: Iterable[str]) -> str:
    """Build a GBNF grammar for one or more SR blocks on *allowed_paths*.

    Unusable and duplicate paths are omitted. A grammar without an allowed
    path could never produce a valid answer, so that case raises ``ValueError``.
    """
    paths = sorted({path for path in allowed_paths
                    if isinstance(path, str) and usable_path(path)})
    if not paths:
        raise ValueError("build_sr_grammar needs at least one usable allowed path")
    marker_set = "".join(_class_char(char) for char in _MARKER_CHARS)
    lines = [
        'root ::= block ("\\n"? block)*',
        "block ::= path " + _literal("\n" + SEARCH_MARKER + "\n") + " body "
        + _literal(DIVIDER_MARKER + "\n") + " body " + _literal(REPLACE_MARKER + "\n"),
        "path ::= " + " | ".join(_literal(path) for path in paths),
        "body ::= line*",
        "line ::= (plain | " + " | ".join(f"run{i}" for i in range(len(_MARKER_CHARS)))
        + ')? "\\n"',
        f"plain ::= [^{marker_set}\\n] rest",
        "rest ::= [^\\n]*",
    ]
    for index, char in enumerate(_MARKER_CHARS):
        runs = " | ".join(_literal(char * size) for size in range(1, _MARKER_LENGTH))
        lines.append(f"run{index} ::= ({runs}) ([^{_class_char(char)}\\n] rest)?")
    return "\n".join(lines) + "\n"
