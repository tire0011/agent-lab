from __future__ import annotations

import hashlib
import hmac
from typing import Any

_SIG_PREFIX = "sha256="
_INBOX_PREFIX = "inbox/"


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
