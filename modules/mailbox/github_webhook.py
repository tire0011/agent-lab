from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from typing import Any

_SIG_PREFIX = "sha256="
_INBOX_PREFIX = "inbox/"
# Optional ``owner/repo`` the webhook is configured for; when set, payloads from
# any other repository are ignored even with a valid signature.
_REPO_ENV = "AGENT_MAILBOX_REPO"


def verify_signature(*, secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header:
        return False
    signature = signature_header.strip()
    if not signature.startswith(_SIG_PREFIX):
        return False
    expected = _SIG_PREFIX + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def is_ping_event(event_name: str | None) -> bool:
    return (event_name or "").strip().lower() == "ping"


def expected_repository() -> str | None:
    """The ``owner/repo`` this webhook should accept, lowercased, if configured."""
    return os.environ.get(_REPO_ENV, "").strip().lower() or None


def repository_matches(payload: Any, expected: str | None) -> bool:
    """True when *payload* comes from *expected* (or none is configured). The HMAC
    signature proves delivery with the configured secret, not the target repo; this
    check closes that gap when ``AGENT_MAILBOX_REPO`` is set."""
    if not expected:
        return True
    repository = payload.get("repository") if isinstance(payload, Mapping) else None
    full_name = repository.get("full_name") if isinstance(repository, Mapping) else None
    return (
        isinstance(full_name, str)
        and full_name.strip().lower() == expected.strip().lower()
    )


def has_inbox_changes(push_payload: dict[str, Any]) -> bool:
    commits = push_payload.get("commits")
    if not isinstance(commits, list):
        return False
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for key in ("added", "modified"):
            paths = commit.get(key)
            if not isinstance(paths, list):
                continue
            for path in paths:
                if isinstance(path, str) and path.startswith(_INBOX_PREFIX):
                    return True
    return False
