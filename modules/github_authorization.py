"""Centralized authorization for external GitHub content entering Nexus.

One rule, enforced at every boundary where GitHub content enters Nexus: only
trusted actors - the repository owner by default - may trigger autonomous
execution. Content from anyone else may be read and analyzed, but never
executed without an explicit, verified owner approval.

Author identity is NEVER taken from issue, PR, comment or mailbox file text:
a body can claim ``created_by: tire0011`` or "approved by the owner" and that
text is attacker-controlled. Only actor fields returned by the GitHub API
(``author.login``, ``user.login``, ``actor.login``) or the git author of the
commit that added a mailbox inbox file decide. Everything fails closed: a
missing or unparseable actor is untrusted.

Configuration (all optional):

- ``NEXUS_REPO_OWNER`` / ``GITHUB_REPOSITORY`` (``owner/repo``) - the owner
  login; defaults to ``tire0011``.
- ``NEXUS_TRUSTED_ACTORS`` - comma-separated logins that may trigger execution
  in addition to / instead of the owner (e.g. a Nexus bot account).
- ``NEXUS_APPROVAL_LABEL`` - the label whose application by a trusted actor
  counts as explicit owner approval of an untrusted item (default
  ``nexus-approved``). The label alone proves nothing; the timeline event's
  ``actor.login`` is what is verified.
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
    "approval_label",
    "api_actor_login",
    "authenticated_actor_login",
    "classify_comment",
    "classify_issue",
    "classify_label_event",
    "classify_mailbox_message",
    "commit_actor_login",
    "file_adding_commit_actor_login",
    "is_trusted_actor",
    "login_from_noreply_email",
    "owner_login",
    "trusted_actors",
]

DEFAULT_OWNER_LOGIN = "tire0011"

_TRUSTED_ACTORS_ENV = "NEXUS_TRUSTED_ACTORS"
_REPO_OWNER_ENV = "NEXUS_REPO_OWNER"
_APPROVAL_LABEL_ENV = "NEXUS_APPROVAL_LABEL"
_DEFAULT_APPROVAL_LABEL = "nexus-approved"

# ``12345678+login@users.noreply.github.com`` and ``login@users.noreply.github.com``.
_NOREPLY_EMAIL_RE = re.compile(
    r"^(?:\d+\+)?([A-Za-z0-9][A-Za-z0-9-]*)@users\.noreply\.github\.com$",
    re.IGNORECASE,
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


def approval_label() -> str:
    """The label a trusted actor applies to approve an untrusted item."""
    return os.environ.get(_APPROVAL_LABEL_ENV, "").strip() or _DEFAULT_APPROVAL_LABEL


def api_actor_login(user: Any) -> str | None:
    """The ``login`` field of a GitHub API user/author/actor object, if any."""
    if not isinstance(user, Mapping):
        return None
    login = user.get("login")
    if not isinstance(login, str):
        return None
    login = login.strip()
    return login or None


def classify_issue(
    issue: Any, *, trusted: frozenset[str] | None = None
) -> ActorDecision:
    """Classify an issue by its API ``author.login`` - never by its body text."""
    if not isinstance(issue, Mapping):
        return ActorDecision(False, None, "issue payload is not an object")
    login = api_actor_login(issue.get("author"))
    if login is None:
        return ActorDecision(False, None, "issue has no API-verified author; refusing to trust it")
    if is_trusted_actor(login, trusted=trusted):
        return ActorDecision(True, login, "issue author is a trusted actor")
    return ActorDecision(False, login, "issue author is not a trusted actor")


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


def classify_label_event(
    event: Any,
    *,
    label: str | None = None,
    trusted: frozenset[str] | None = None,
) -> ActorDecision:
    """A ``labeled`` timeline event counts only if a trusted actor applied it.

    The label name in an issue-list response carries no actor information, so a
    label alone never authorizes anything; the event's ``actor.login`` does.
    """
    if not isinstance(event, Mapping) or str(event.get("event") or "").lower() != "labeled":
        return ActorDecision(False, None, "not a labeled event")
    wanted = (label or approval_label()).strip().lower()
    applied = event.get("label")
    name = applied.get("name") if isinstance(applied, Mapping) else applied
    if not isinstance(name, str) or name.strip().lower() != wanted:
        return ActorDecision(False, None, f"event did not apply the approval label {wanted!r}")
    login = api_actor_login(event.get("actor"))
    if login is None:
        return ActorDecision(False, None, "label event has no API-verified actor")
    if is_trusted_actor(login, trusted=trusted):
        return ActorDecision(True, login, f"trusted actor applied the approval label {wanted!r}")
    return ActorDecision(False, login, "approval label was applied by an untrusted actor")


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


def commit_actor_login(repo_path: Path | str, sha: Any) -> str | None:
    """GitHub login of the author of *sha*, via the commit's noreply email."""
    sha = str(sha or "").strip()
    if not sha or sha == "unknown":
        return None
    email = _git(repo_path, ["show", "-s", "--format=%ae", sha])
    return login_from_noreply_email(email)


def file_adding_commit_actor_login(repo_path: Path | str, file_path: Path | str) -> str | None:
    """GitHub login of the commit that added *file_path* to the repository."""
    repo = Path(repo_path)
    try:
        rel = str(Path(file_path).relative_to(repo))
    except ValueError:
        rel = str(file_path)
    email = _git(repo, ["log", "--diff-filter=A", "-1", "--format=%ae", "--", rel])
    return login_from_noreply_email(email)


def classify_mailbox_message(
    created_by: Any,
    repo_path: Path | str,
    file_path: Path | str,
    *,
    trusted: frozenset[str] | None = None,
) -> ActorDecision:
    """Classify a mailbox inbox file by the git commit that added it.

    ``created_by`` is front matter written inside the file itself - text the
    pusher controls - so it is never used for the decision. The commit author
    is the only signal that cannot be forged from within the file.
    """
    login = file_adding_commit_actor_login(repo_path, file_path)
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


_authenticated_login_cache: str | None = None
_authenticated_login_resolved = False


def authenticated_actor_login() -> str | None:
    """Login of the ``gh`` token Nexus itself uses, if it can be resolved.

    Cached: this is one ``gh api user`` call per process. Returns ``None`` when
    it cannot be determined - the caller must then rely on the configured
    trusted actors only.
    """
    global _authenticated_login_cache, _authenticated_login_resolved
    if _authenticated_login_resolved:
        return _authenticated_login_cache
    _authenticated_login_resolved = True
    login: str | None = None
    try:
        proc = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        candidate = proc.stdout.strip().lower()
        if candidate:
            login = candidate
    _authenticated_login_cache = login
    return login
