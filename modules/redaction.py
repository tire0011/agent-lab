"""Shared redaction/sanitization helpers.

Used on the write path (before activity messages are persisted) and on the
read path (API responses), so a future formatter bug cannot leak a secret or
absolute path that was never written. Nothing here returns raw command
output, stack traces with internal paths, environment variables, prompts or
credentials.
"""

from __future__ import annotations

import os
import re

# Credentials whose literal value is scrubbed wherever it appears, in addition
# to the keyword/shape patterns: a bare value echoed by a command or a stack
# trace would otherwise pass. The log surface is readable without an API key
# (DECISIONS.md D-012), so a leaked AGENT_API_KEY would expose write endpoints.
_SECRET_ENV_VARS = (
    "AGENT_API_KEY",
    "AGENT_GITHUB_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
    "AZURE_OPENAI_API_KEY",
)
# Short values are ignored: a 1-2 character secret would otherwise match
# almost every line and destroy the logs' usefulness.
_MIN_SECRET_VALUE_CHARS = 8

_PLACEHOLDER = "[REDACTED]"

# A secret is a keyword plus an assignment (`token=...`, `"api_key": "..."`,
# `Authorization: Bearer ...`). The separator is required, so ordinary prose
# ("the token is not configured") is left alone (#175), and the value stops at
# structural punctuation so redacted JSON stays parseable. The separator may
# include the key's closing quote (`{"password": "..."}`).
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|credential|authorization|cookie)"
    # `=>` is one separator, not `=` followed by a value of `>`: reading it
    # the wrong way both mangles the text and leaves the secret behind it.
    r"([\"']?\s*(?:=>|[:=])\s*)"
    # An already-redacted value is matched whole so it is left alone: the
    # shape patterns above run first, and re-matching their placeholder used
    # to strip its closing bracket ("token=[REDACTED]]").
    r"(\[REDACTED\]|\"[^\"]*\"|'[^']*'|[^\s\"',;)\]}]+)"
)
# Two shapes the keyword pattern misses: the token after "Bearer", and bare
# OpenAI-style "sk-..." keys.
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+\S+")
_SK_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9]{10,}")
# GitHub tokens can appear without a keyword (#172), so their shapes are
# matched directly.
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


def _redacted_assignment(match: re.Match[str]) -> str:
    """Replace the value of a keyword assignment, keeping separator and quotes,
    so a redacted serialized structure stays parseable."""
    keyword, separator, value = match.group(1), match.group(2), match.group(3)
    if value == _PLACEHOLDER:
        return match.group(0)
    quote = value[:1] if value[:1] in {'"', "'"} else ""
    return f"{keyword}{separator}{quote}{_PLACEHOLDER}{quote}"


def redact_known_secret_values(text: str) -> str:
    """Scrub the literal values of this process's configured credentials, however printed."""
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if len(value) >= _MIN_SECRET_VALUE_CHARS and value in text:
            text = text.replace(value, "[REDACTED]")
    return text


def redact_secrets(text: str) -> str:
    """Scrub secret-shaped substrings but keep filesystem paths.

    Only where the path is the payload (e.g. the command a trusted local runner
    executed) and the value never reaches a persisted log or API response.
    """
    text = redact_known_secret_values(text)
    text = _URL_CRED_PATTERN.sub("[REDACTED]", text)
    text = _REMOTE_URL_PATTERN.sub("[REMOTE]", text)
    text = _BEARER_PATTERN.sub("[REDACTED]", text)
    text = _SK_KEY_PATTERN.sub("[REDACTED]", text)
    text = _GITHUB_TOKEN_PATTERN.sub("[REDACTED]", text)
    return _SECRET_PATTERN.sub(_redacted_assignment, text)


# Only the userinfo part of a URL, so host and path stay readable.
_URL_USERINFO_PATTERN = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^@\s/]*:[^@\s/]*@")


def redact_credentials(text: str) -> str:
    """Scrub credential values only; keep paths and URLs readable (stored prompts, #259)."""
    text = redact_known_secret_values(text)
    text = _URL_USERINFO_PATTERN.sub(r"\1[REDACTED]@", text)
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _SK_KEY_PATTERN.sub("[REDACTED]", text)
    text = _GITHUB_TOKEN_PATTERN.sub("[REDACTED]", text)
    return _SECRET_PATTERN.sub(_redacted_assignment, text)


#: Mask for credentials in mailbox command results (#220).
OUTBOUND_PLACEHOLDER = "****"
# An env var whose name marks it as a credential: its value is scrubbed from
# outbound text even if it is not in ``_SECRET_ENV_VARS`` (``env`` prints them
# all), so every credential stays on the VM.
_SENSITIVE_ENV_NAME = re.compile(
    r"(?i)(token|key|secret|password|passwd|credential|authorization|cookie|azure_openai)"
)


def sensitive_environment_values(environ: dict[str, str] | None = None) -> list[str]:
    """Literal values of every credential configured in *environ*.

    Longest first, so a secret that contains another one is masked whole
    instead of leaving its remainder readable.
    """
    source = os.environ if environ is None else environ
    values = {
        value.strip()
        for name, value in source.items()
        if name in _SECRET_ENV_VARS or _SENSITIVE_ENV_NAME.search(name)
    }
    return sorted(
        (value for value in values if len(value) >= _MIN_SECRET_VALUE_CHARS),
        key=len,
        reverse=True,
    )


def redact_outbound(text: str | None, *, environ: dict[str, str] | None = None) -> str:
    """Mask every credential in text that is about to leave the VM (#220).

    The redaction step for mailbox command results (stdout, stderr, errors and
    the outbox document): the literal value of every configured credential,
    wherever and however often it appears, then every generic credential
    shape, each replaced by ``****``. Paths and plain URLs stay readable.
    """
    if not text:
        return ""
    for value in sensitive_environment_values(environ):
        text = text.replace(value, OUTBOUND_PLACEHOLDER)
    mask = OUTBOUND_PLACEHOLDER
    text = _URL_USERINFO_PATTERN.sub(rf"\1{mask}@", text)
    text = _BEARER_PATTERN.sub(f"Bearer {mask}", text)
    text = _SK_KEY_PATTERN.sub(mask, text)
    text = _GITHUB_TOKEN_PATTERN.sub(mask, text)

    def _masked_assignment(match: re.Match[str]) -> str:
        keyword, separator, value = match.group(1), match.group(2), match.group(3)
        if value in (mask, _PLACEHOLDER) or (
            separator.rstrip().endswith(":") and value.lower() == "bearer"
        ):
            # Already masked, or "Authorization: Bearer ****" - whose
            # credential the bearer pattern above has masked already.
            return match.group(0)
        quote = value[:1] if value[:1] in {'"', "'"} else ""
        return f"{keyword}{separator}{quote}{mask}{quote}"

    return _SECRET_PATTERN.sub(_masked_assignment, text)


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
    """A bounded, redacted tail of test output for diagnostics.

    At most ``_FAILURE_TAIL_MAX_LINES`` lines of up to 500 characters and
    ``_FAILURE_TAIL_MAX_CHARS`` in total, built from the end so the final
    FAILED lines survive. Empty output gives an empty list.
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
