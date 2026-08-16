"""Shared redaction/sanitization helpers.

Centralized here so both the write path (``ExperimentService``, which
persists activity-log messages) and the read path (``api.py`` response
builders) use exactly one implementation. Applying redaction *before*
persistence (not only at response time) means activity events are safe
by design: even a bug in a future read-path formatter cannot leak a
secret or absolute filesystem path that was never written to the
database in the first place.

Nothing here ever returns raw command output, stack traces with
internal paths, environment variables, prompts, or credentials.
"""

from __future__ import annotations

import re

_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|credential|authorization|cookie)(\s*[:=]\s*|\s+)\S+"
)
# Two additional secret shapes that the generic keyword-based pattern above
# does not fully cover on its own: an HTTP "Bearer <token>" credential (the
# keyword pattern only consumes the single word "Bearer", leaving the actual
# token exposed) and bare OpenAI-style "sk-..." API keys that can appear
# without a preceding keyword at all (e.g. copy-pasted into a prompt).
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+\S+")
_SK_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9]{10,}")
# HTTPS remote URLs with embedded credentials, e.g. ******github.com/...
_URL_CRED_PATTERN = re.compile(r"https?://[^@\s]*:[^@\s]*@\S*")
# Any remote URL that could reveal repository identity (plain HTTPS, SSH, SCP-style git@)
_REMOTE_URL_PATTERN = re.compile(
    r"(?:"
    r"https?://\S+"          # plain HTTPS remote
    r"|ssh://\S+"            # ssh:// remote
    r"|git://\S+"            # git:// remote
    r"|git@[^\s:]+:[^\s]+"   # SCP-style git@host:org/repo
    r")"
)
_PATH_PATTERN = re.compile(r"(?<!\w)(?:/[\w.~-]+){2,}")


def redact(text: str) -> str:
    """Scrub secret-shaped substrings and absolute filesystem paths."""
    text = _URL_CRED_PATTERN.sub("[REDACTED]", text)
    text = _REMOTE_URL_PATTERN.sub("[REMOTE]", text)
    text = _BEARER_PATTERN.sub("[REDACTED]", text)
    text = _SK_KEY_PATTERN.sub("[REDACTED]", text)
    text = _SECRET_PATTERN.sub(r"\1=[REDACTED]", text)
    text = _PATH_PATTERN.sub("[PATH]", text)
    return text


def bounded_lines(raw: str | None, *, max_lines: int = 100, max_line_chars: int = 500) -> list[str]:
    """Return bounded, redacted lines; never the raw/unbounded text."""
    lines = (raw or "").splitlines()[-max_lines:]
    return [redact(line)[:max_line_chars] for line in lines]


_FAILURE_TAIL_MAX_LINES = 30
_FAILURE_TAIL_MAX_CHARS = 4000


def test_failure_tail(raw: str | None) -> list[str]:
    """Return a bounded, redacted tail of test output for diagnostics.

    At most ``_FAILURE_TAIL_MAX_LINES`` lines are returned, each truncated to
    ``max_line_chars`` characters, and the total character count is capped at
    ``_FAILURE_TAIL_MAX_CHARS``.  When there is no output an empty list is
    returned, so callers can omit the field cleanly.
    """
    if not raw or not raw.strip():
        return []
    lines = raw.splitlines()[-_FAILURE_TAIL_MAX_LINES:]
    result: list[str] = []
    total = 0
    for line in lines:
        line = redact(line)[:500]
        total += len(line) + 1  # +1 for the implicit newline
        if total > _FAILURE_TAIL_MAX_CHARS:
            break
        result.append(line)
    return result


def sanitized_reason(raw: str | None, *, max_chars: int = 500) -> str | None:
    """Bounded, redacted single-line reason/error text for diagnostics."""
    if not raw:
        return None
    return redact(raw)[:max_chars]


def changed_file_names(git_status: str | None, *, limit: int = 100) -> list[str]:
    """Extract basenames only; responses must never contain paths."""
    names: list[str] = []
    for line in (git_status or "").splitlines():
        candidate = line[3:].strip() if len(line) > 3 else ""
        if " -> " in candidate:
            candidate = candidate.rsplit(" -> ", 1)[-1]
        name = candidate.replace("\\", "/").rsplit("/", 1)[-1]
        if name and re.fullmatch(r"[A-Za-z0-9_. -]{1,120}", name) and name not in names:
            names.append(name)
    return names[:limit]


def test_command_name(test_command: list[str] | None) -> str | None:
    """Return a safe, path-free descriptor of the test command (never argv paths)."""
    if not test_command:
        return None
    tokens = []
    for token in test_command:
        name = str(token).replace("\\", "/").rsplit("/", 1)[-1]
        if name:
            tokens.append(name)
    return " ".join(tokens) if tokens else None
