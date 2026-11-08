"""Trusted-actor checks for the channels Nexus trusts, and Git provenance for
mailbox messages. Issue execution authorization is NOT decided here (it lives in
``modules.issue_approvals``); this module answers which GitHub actor content
provably comes from, using only API actor fields (``user.login``) or the git
author of the commit that added a mailbox inbox file. Author information embedded
in issue, comment or file text is never trusted, and everything fails closed.

Config (all optional): ``NEXUS_REPO_OWNER``/``GITHUB_REPOSITORY`` (owner login),
``NEXUS_TRUSTED_ACTORS`` (extra trusted logins), ``NEXUS_TRUSTED_MAILBOX_EMAILS``
(git author emails or ``@domain`` suffixes whose mailbox commits are trusted).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ActorDecision",
    "api_actor_login",
    "classify_comment",
    "classify_mailbox_message",
    "file_adding_commit_actor_login",
    "file_adding_commit_email",
    "is_trusted_mailbox_email",
    "trusted_mailbox_emails",
    "is_trusted_actor",
    "login_from_noreply_email",
    "owner_login",
    "trusted_actors",
]

DEFAULT_OWNER_LOGIN = "tire0011"

_TRUSTED_ACTORS_ENV = "NEXUS_TRUSTED_ACTORS"
_REPO_OWNER_ENV = "NEXUS_REPO_OWNER"

# ``12345678+login@users.noreply.github.com`` and ``login@users.noreply.github.com``.
_NOREPLY_EMAIL_RE = re.compile(
    r"^(?:\d+\+)?([A-Za-z0-9][A-Za-z0-9-]*)@users\.noreply\.github\.com$",
    re.IGNORECASE,
)


_TRUSTED_MAILBOX_EMAILS_ENV = "NEXUS_TRUSTED_MAILBOX_EMAILS"

# Git author addresses of the owner and their coding agents; only accounts with
# push access can commit to the private mailbox at all, and this list decides
# which of those commits may execute.
DEFAULT_TRUSTED_MAILBOX_EMAILS = (
    "tire0011@gmx.net",
    "noreply@anthropic.com",
    "vibe@mistral.ai",
    "@openai.com",
)


@dataclass(frozen=True, slots=True)
class ActorDecision:
    """Outcome of checking one external actor. Fails closed by construction."""

    trusted: bool
    actor: str | None
    detail: str = ""


def owner_login() -> str:
    """The repository owner's login, lowercased."""
    explicit = os.environ.get(_REPO_OWNER_ENV, "").strip()
    if explicit:
        return explicit.lower()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if repo and "/" in repo:
        return repo.split("/", 1)[0].strip().lower()
    return DEFAULT_OWNER_LOGIN


def trusted_actors() -> frozenset[str]:
    """Logins (lowercased) allowed to trigger autonomous execution."""
    raw = os.environ.get(_TRUSTED_ACTORS_ENV, "").strip()
    if raw:
        return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
    return frozenset({owner_login()})


def is_trusted_actor(login: Any, *, trusted: frozenset[str] | None = None) -> bool:
    if not isinstance(login, str):
        return False
    normalized = login.strip().lower()
    if not normalized:
        return False
    return normalized in (trusted if trusted is not None else trusted_actors())


def api_actor_login(user: Any) -> str | None:
    """The ``login`` field of a GitHub API user/author/actor object, if any."""
    if not isinstance(user, Mapping):
        return None
    login = user.get("login")
    if not isinstance(login, str):
        return None
    login = login.strip()
    return login or None


def classify_comment(
    comment: Any, *, trusted: frozenset[str] | None = None
) -> ActorDecision:
    """Classify an issue/PR comment by its API ``user.login`` - never by its text."""
    if not isinstance(comment, Mapping):
        return ActorDecision(False, None, "comment payload is not an object")
    login = api_actor_login(comment.get("user"))
    if login is None:
        return ActorDecision(False, None, "comment has no API-verified author; refusing to trust it")
    if is_trusted_actor(login, trusted=trusted):
        return ActorDecision(True, login, "comment author is a trusted actor")
    return ActorDecision(False, login, "comment author is not a trusted actor")


def login_from_noreply_email(email: Any) -> str | None:
    """Map a GitHub noreply address to the login it belongs to, else ``None``."""
    if not isinstance(email, str):
        return None
    match = _NOREPLY_EMAIL_RE.match(email.strip())
    if not match:
        return None
    return match.group(1).lower()


def _git(repo_path: Path | str, args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def trusted_mailbox_emails() -> tuple[str, ...]:
    """Lowercased author emails / ``@domain`` suffixes trusted in the mailbox."""
    raw = os.environ.get(_TRUSTED_MAILBOX_EMAILS_ENV, "").strip()
    if raw:
        return tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return DEFAULT_TRUSTED_MAILBOX_EMAILS


def is_trusted_mailbox_email(email: Any) -> bool:
    if not isinstance(email, str) or "@" not in email:
        return False
    email = email.strip().lower()
    return any(
        email.endswith(entry) if entry.startswith("@") else email == entry
        for entry in trusted_mailbox_emails()
    )


def file_adding_commit_email(repo_path: Path | str, file_path: Path | str) -> str | None:
    """Author email of the commit that added *file_path* to the repository."""
    repo = Path(repo_path)
    try:
        rel = str(Path(file_path).relative_to(repo))
    except ValueError:
        rel = str(file_path)
    return _git(repo, ["log", "--diff-filter=A", "-1", "--format=%ae", "--", rel])


def file_adding_commit_actor_login(repo_path: Path | str, file_path: Path | str) -> str | None:
    """GitHub login of the commit that added *file_path* to the repository."""
    return login_from_noreply_email(file_adding_commit_email(repo_path, file_path))


def classify_mailbox_message(
    created_by: Any,
    repo_path: Path | str,
    file_path: Path | str,
    *,
    trusted: frozenset[str] | None = None,
) -> ActorDecision:
    """Classify a mailbox inbox file by the git commit that added it. ``created_by``
    is front matter inside the file - text the pusher controls - so it is never
    used; the commit author is the only signal not forgeable from within the file.
    """
    email = file_adding_commit_email(repo_path, file_path)
    if is_trusted_mailbox_email(email):
        return ActorDecision(True, email, "inbox file was committed with a trusted author email")
    login = login_from_noreply_email(email)
    if login is None:
        return ActorDecision(
            False,
            None,
            "inbox file has no commit with a verifiable GitHub author "
            f"(created_by={created_by!r} is file text and is not trusted)",
        )
    if is_trusted_actor(login, trusted=trusted):
        return ActorDecision(True, login, "inbox file was committed by a trusted actor")
    return ActorDecision(False, login, "inbox file was committed by an untrusted actor")
