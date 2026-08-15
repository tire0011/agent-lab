"""Centralised path configuration for the mailbox repositories.

Separates the *code* repository (experiments, IDEAS.md, state/runs) from the
*mailbox* repository (inbox, outbox, git operations on messages).

Environment variables:

    AGENT_CODE_REPO_PATH    – root of the code checkout (default: /agent)
    AGENT_MAILBOX_REPO_PATH – root of the mailbox checkout (default: /agent)

Path resolution rules
---------------------
* **Single-repo (legacy) layout** — both paths point to the same directory:
    inbox  → ``<repo>/communication/inbox``
    outbox → ``<repo>/communication/outbox``

* **Separate-repo (external) layout** — paths differ:
    inbox  → ``<mailbox_repo>/inbox``
    outbox → ``<mailbox_repo>/outbox``

The external mailbox repository is expected to contain ``inbox/``, ``outbox/``,
and ``archive/`` at its root, not nested under ``communication/``.
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
        """Build from explicit Path objects, applying the layout selection rule.

        When *code_path* and *mailbox_path* resolve to the same directory the
        legacy layout is used (``communication/inbox``, ``communication/outbox``).
        When they differ, the external layout is used (``inbox``, ``outbox`` at
        the root of the mailbox repository).
        """
        separate = code_path.resolve() != mailbox_path.resolve()
        if separate:
            inbox_dir = mailbox_path / "inbox"
            outbox_dir = mailbox_path / "outbox"
        else:
            inbox_dir = mailbox_path / "communication" / "inbox"
            outbox_dir = mailbox_path / "communication" / "outbox"
        return cls(
            code_repo_path=code_path,
            mailbox_repo_path=mailbox_path,
            inbox_dir=inbox_dir,
            outbox_dir=outbox_dir,
        )

    @classmethod
    def from_env(cls) -> "MailboxConfig":
        """Build from environment variables, applying the layout selection rule.

        Single-repo (legacy) layout when both variables are unset or equal:
            inbox  → ``/agent/communication/inbox``
            outbox → ``/agent/communication/outbox``

        Separate-repo (external) layout when the variables differ:
            inbox  → ``<AGENT_MAILBOX_REPO_PATH>/inbox``
            outbox → ``<AGENT_MAILBOX_REPO_PATH>/outbox``
        """
        code = Path(os.environ.get("AGENT_CODE_REPO_PATH", _DEFAULT_REPO))
        mailbox = Path(os.environ.get("AGENT_MAILBOX_REPO_PATH", _DEFAULT_REPO))
        return cls.from_paths(code, mailbox)

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
