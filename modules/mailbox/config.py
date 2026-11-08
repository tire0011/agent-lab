"""Centralised path configuration for the mailbox repositories: the *code* repo
(``AGENT_CODE_REPO_PATH``, default /agent; experiments, IDEAS.md, state/runs) is
separate from the *mailbox* repo (``AGENT_MAILBOX_REPO_PATH``, required; inbox/,
outbox/, archive/ at its root - no single-repo compatibility layout).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_REPO = "/agent"


@dataclass(frozen=True)
class MailboxConfig:
    """Typed path configuration for the two-repository mailbox layout: code_repo_path
    (code checkout), mailbox_repo_path (git operations on messages), inbox_dir and
    outbox_dir (absolute paths under the mailbox repo)."""

    code_repo_path: Path
    mailbox_repo_path: Path
    inbox_dir: Path
    outbox_dir: Path

    # Factories

    @classmethod
    def from_paths(cls, code_path: Path, mailbox_path: Path) -> "MailboxConfig":
        """Build from explicit code and mailbox repository paths."""
        return cls(
            code_repo_path=code_path,
            mailbox_repo_path=mailbox_path,
            inbox_dir=mailbox_path / "inbox",
            outbox_dir=mailbox_path / "outbox",
        )

    @classmethod
    def from_env(cls) -> "MailboxConfig":
        """Build from environment variables. ``AGENT_MAILBOX_REPO_PATH`` is required:
        falling back to the code checkout would make a valid message appear accepted
        while the live processor watches a different repository."""
        code = Path(os.environ.get("AGENT_CODE_REPO_PATH", _DEFAULT_REPO))
        mailbox_value = os.environ.get("AGENT_MAILBOX_REPO_PATH")
        if not mailbox_value or not mailbox_value.strip():
            raise ValueError("AGENT_MAILBOX_REPO_PATH is required")
        mailbox = Path(mailbox_value)
        return cls.from_paths(code, mailbox)

    # Convenience



# Drain limits

# The worker re-scans the inbox until no new work appears (a message arriving while
# it is busy is picked up by the lock owner, not a later timer run), so these bounds
# stop that loop from running forever; the timer fires once a minute, so draining
# past one period gains nothing and stays inside systemd's default start timeout.
_DEFAULT_DRAIN_MAX_PASSES = 5
_DEFAULT_DRAIN_MAX_SECONDS = 60.0


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw.strip()))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, float(raw.strip()))
    except ValueError:
        return default


@dataclass(frozen=True)
class MailboxDrainLimits:
    """Safety bounds for the inbox drain loop (max_passes, max_seconds), checked
    *between* passes, never inside one: the batch in flight always finishes, and
    whatever is left waits for the next timer invocation."""

    max_passes: int = _DEFAULT_DRAIN_MAX_PASSES
    max_seconds: float = _DEFAULT_DRAIN_MAX_SECONDS

    @classmethod
    def from_env(cls) -> "MailboxDrainLimits":
        """Build from environment variables; unset/blank/unparseable keep the default
        and below-minimum values are clamped, so a misconfiguration can never disable
        processing altogether."""
        return cls(
            max_passes=_env_int(
                "AGENT_MAILBOX_DRAIN_MAX_PASSES", _DEFAULT_DRAIN_MAX_PASSES, minimum=1
            ),
            max_seconds=_env_float(
                "AGENT_MAILBOX_DRAIN_MAX_SECONDS", _DEFAULT_DRAIN_MAX_SECONDS, minimum=0.0
            ),
        )
