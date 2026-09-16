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
