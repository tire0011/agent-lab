"""Centralised path configuration for the mailbox repositories.

Separates the *code* repository (experiments, IDEAS.md, state/runs) from the
*mailbox* repository (inbox, outbox, git operations on messages).

Environment variables:

    AGENT_CODE_REPO_PATH    – root of the code checkout (default: /agent)
    AGENT_MAILBOX_REPO_PATH – root of the mailbox checkout (required)

The mailbox repository contains ``inbox/``, ``outbox/``, and ``archive/`` at
its root.  There is no single-repository compatibility layout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_REPO = "/agent"


@dataclass(frozen=True)
class MailboxConfig:
    """Typed path configuration for the two-repository mailbox layout.

    Attributes:
        code_repo_path    – root of the code repository.  Artifacts like
                            IDEAS.md and state/runs live here.
        mailbox_repo_path – root of the mailbox repository.  Git operations
                            for inbox/outbox messages run here.
        inbox_dir         – absolute path to the inbox directory.
        outbox_dir        – absolute path to the outbox directory.
    """

    code_repo_path: Path
    mailbox_repo_path: Path
    inbox_dir: Path
    outbox_dir: Path

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

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
        """Build from environment variables.

        ``AGENT_MAILBOX_REPO_PATH`` is required.  Falling back to the code
        checkout would make a valid message appear accepted while the live
        processor watches a different repository.
        """
        code = Path(os.environ.get("AGENT_CODE_REPO_PATH", _DEFAULT_REPO))
        mailbox_value = os.environ.get("AGENT_MAILBOX_REPO_PATH")
        if not mailbox_value or not mailbox_value.strip():
            raise ValueError("AGENT_MAILBOX_REPO_PATH is required")
        mailbox = Path(mailbox_value)
        return cls.from_paths(code, mailbox)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def repos_are_separate(self) -> bool:
        """True when code and mailbox paths point to different directories."""
        return self.code_repo_path.resolve() != self.mailbox_repo_path.resolve()


# ---------------------------------------------------------------------------
# Drain limits
# ---------------------------------------------------------------------------

# The mailbox worker keeps re-scanning the inbox until no new work appears, so
# a message that arrives while it is busy is picked up by the processor that
# already owns the lock instead of waiting for a timer invocation that can only
# report `busy`.  These bounds stop that loop from running forever when inbox
# traffic never stops.  They are deliberately defined here, next to the rest of
# the mailbox configuration, rather than inline in the poll runner.
#
# The timer fires once a minute, so draining past one timer period gains
# nothing over letting the next invocation continue the remaining inbox work,
# and it keeps a `Type=oneshot` run inside systemd's default start timeout.
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
    """Safety bounds for the inbox drain loop.

    Attributes:
        max_passes  – how many processing passes a single worker may run,
                      including the first one.
        max_seconds – how long a worker may keep starting new passes.

    Both bounds are checked *between* passes, never inside one: the worker
    always finishes the message batch it started, then leaves whatever is left
    in the inbox for the next timer invocation.
    """

    max_passes: int = _DEFAULT_DRAIN_MAX_PASSES
    max_seconds: float = _DEFAULT_DRAIN_MAX_SECONDS

    @classmethod
    def from_env(cls) -> "MailboxDrainLimits":
        """Build from environment variables, falling back to the defaults.

        Unset, blank, and unparseable values keep the default; a value below
        the minimum is clamped so a misconfiguration can never disable
        processing altogether.
        """
        return cls(
            max_passes=_env_int(
                "AGENT_MAILBOX_DRAIN_MAX_PASSES", _DEFAULT_DRAIN_MAX_PASSES, minimum=1
            ),
            max_seconds=_env_float(
                "AGENT_MAILBOX_DRAIN_MAX_SECONDS", _DEFAULT_DRAIN_MAX_SECONDS, minimum=0.0
            ),
        )
