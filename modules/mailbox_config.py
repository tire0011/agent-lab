"""Centralised path configuration for the mailbox repositories.

Separates the *code* repository (experiments, IDEAS.md, state/runs) from the
*mailbox* repository (inbox, outbox, git operations on messages).

Environment variables:

    AGENT_CODE_REPO_PATH    – root of the code checkout (default: /agent)
    AGENT_MAILBOX_REPO_PATH – root of the mailbox checkout (default: /agent)

When both variables are unset, the defaults reproduce the legacy layout:
    inbox  → /agent/communication/inbox
    outbox → /agent/communication/outbox
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
    def from_env(cls) -> "MailboxConfig":
        """Build from environment variables.

        Reproduces the legacy single-repo layout when neither variable is set:
            inbox  → ``mailbox_repo_path / communication / inbox``
            outbox → ``mailbox_repo_path / communication / outbox``
        """
        code = Path(os.environ.get("AGENT_CODE_REPO_PATH", _DEFAULT_REPO))
        mailbox = Path(os.environ.get("AGENT_MAILBOX_REPO_PATH", _DEFAULT_REPO))
        return cls(
            code_repo_path=code,
            mailbox_repo_path=mailbox,
            inbox_dir=mailbox / "communication" / "inbox",
            outbox_dir=mailbox / "communication" / "outbox",
        )

    @classmethod
    def legacy(cls, repo_path: Path) -> "MailboxConfig":
        """Create a config matching the legacy single-repository layout.

        Both code and mailbox operations target *repo_path*, with inbox/outbox
        under ``communication/``.
        """
        return cls(
            code_repo_path=repo_path,
            mailbox_repo_path=repo_path,
            inbox_dir=repo_path / "communication" / "inbox",
            outbox_dir=repo_path / "communication" / "outbox",
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def repos_are_separate(self) -> bool:
        """True when code and mailbox paths point to different directories."""
        return self.code_repo_path.resolve() != self.mailbox_repo_path.resolve()
