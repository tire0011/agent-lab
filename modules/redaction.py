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

import os
import re

# Credentials whose *literal value* is scrubbed wherever it appears, in
# addition to the keyword/shape patterns below. The patterns only catch a
# secret that is introduced by a recognizable keyword ("api_key=", "Bearer
# ", "sk-..."); a bare value echoed on its own - by a shell command, a
# config dump, a stack trace - would otherwise pass straight through.
# That matters because the log surface is readable without an API key
# (see DECISIONS.md D-012), so a leaked AGENT_API_KEY would hand a reader
# the *write* endpoints, including shell-backed operations.
_SECRET_ENV_VARS = (
    "AGENT_API_KEY",
    "AGENT_GITHUB_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
    "AZURE_OPENAI_API_KEY",
)
# Short values are ignored: a 1-2 character secret would otherwise match
# almost every line and destroy the logs' usefulness.
_MIN_SECRET_VALUE_CHARS = 8

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
# GitHub credentials have the same problem as bare "sk-..." keys and now the
# same exposure: a diagnosis that reads GitHub lifecycle state (#172) handles
# text where a token can appear with no keyword next to it - and the generic
# keyword pattern above only consumes one word, so "token is ghp_..." leaves
# the credential itself in the clear.
_GITHUB_TOKEN_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})"
)
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


def redact_known_secret_values(text: str) -> str:
    """Scrub the literal values of this process's configured credentials.

    Complements the shape/keyword patterns: those need a recognizable
    marker next to the secret, this one matches the value itself no
    matter how it was printed.
    """
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if len(value) >= _MIN_SECRET_VALUE_CHARS and value in text:
            text = text.replace(value, "[REDACTED]")
    return text


def redact_secrets(text: str) -> str:
    """Scrub secret-shaped substrings while preserving filesystem paths.

    Used where a path is the meaningful payload (for example the command a
    trusted local runner actually executed, including its interpreter), and
    where the value never reaches a persisted activity log or API response.
    """
    text = redact_known_secret_values(text)
    text = _URL_CRED_PATTERN.sub("[REDACTED]", text)
    text = _REMOTE_URL_PATTERN.sub("[REMOTE]", text)
    text = _BEARER_PATTERN.sub("[REDACTED]", text)
    text = _SK_KEY_PATTERN.sub("[REDACTED]", text)
    text = _GITHUB_TOKEN_PATTERN.sub("[REDACTED]", text)
    return _SECRET_PATTERN.sub(r"\1=[REDACTED]", text)


def redact(text: str) -> str:
    """Scrub secret-shaped substrings and absolute filesystem paths."""
    return _PATH_PATTERN.sub("[PATH]", redact_secrets(text))


def bounded_lines(raw: str | None, *, max_lines: int = 100, max_line_chars: int = 500) -> list[str]:
    """Return bounded, redacted lines; never the raw/unbounded text."""
    lines = (raw or "").splitlines()[-max_lines:]
    return [redact(line)[:max_line_chars] for line in lines]


_FAILURE_TAIL_MAX_LINES = 30
_FAILURE_TAIL_MAX_CHARS = 4000


def test_failure_tail(raw: str | None) -> list[str]:
    """Return a bounded, redacted tail of test output for diagnostics.

    At most ``_FAILURE_TAIL_MAX_LINES`` lines are returned, each truncated to
    500 characters, and the total character count is capped at
    ``_FAILURE_TAIL_MAX_CHARS``.  The budget is built from the *end* so that
    the final assertion/FAILED lines are always preserved when the limit is
    hit.  When there is no output an empty list is returned, so callers can
    omit the field cleanly.
    """
    if not raw or not raw.strip():
        return []
    lines = raw.splitlines()[-_FAILURE_TAIL_MAX_LINES:]
    # Collect from the end so the last lines are always kept under the budget.
    collected: list[str] = []
    total = 0
    for line in reversed(lines):
        line = redact(line)[:500]
        cost = len(line) + 1  # +1 for the implicit newline
        if total + cost > _FAILURE_TAIL_MAX_CHARS:
            break
        collected.append(line)
        total += cost
    collected.reverse()
    return collected


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
