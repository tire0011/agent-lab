"""Recipient routing for the shared mailbox (mailbox issue #9, PR #8).

Other agents (for example ``home-gpu``) share the inbox. This processor may
only touch what is addressed to Nexus:

- no ``to`` field: a legacy message, for Nexus;
- ``to: nexus`` (case-insensitive, comma-separated lists allowed): for Nexus;
- any other recipient, an empty or malformed ``to``, or several ``to`` lines:
  for somebody else - left completely untouched (no record, log, retry,
  archive, outbox entry or command).

The rules mirror ``mailbox_client.message.recipients_of`` in the mailbox repo.
The check reads the header text only and fails closed: when a file cannot be
read, it is not Nexus'. A file without a parseable header and without any
``to`` line keeps its legacy handling, so its diagnostics still reach the
owner.
"""

from __future__ import annotations

import re
from pathlib import Path

NEXUS_AGENT_ID = "nexus"
_HEAD_BYTES = 64 * 1024
_TO_LINE = re.compile(r"^\s*to\s*:(.*)$", re.IGNORECASE)


def _header_lines(text: str) -> list[str]:
    """The front-matter lines, or every line when the block is not closed
    (a ``to`` there still counts, conservatively)."""
    if not text.startswith("---"):
        return text.splitlines()[:40]
    rest = text[3:]
    end = rest.find("\n---")
    return (rest if end == -1 else rest[:end]).splitlines()


def recipients(text: str) -> frozenset[str] | None:
    """``None`` for a legacy message without ``to``; otherwise the recipients
    (empty when the field is blank, repeated or otherwise malformed)."""
    values = [m.group(1) for m in (_TO_LINE.match(line) for line in _header_lines(text)) if m]
    if not values:
        return None
    if len(values) > 1:
        return frozenset()
    raw = values[0].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1].strip()
    return frozenset(token.strip().lower() for token in raw.split(",") if token.strip())


def is_for_nexus(file_path: Path) -> bool:
    """Whether the legacy Nexus processor may handle *file_path* at all."""
    if file_path.is_symlink():
        # Not read here; the processor's own validation rejects it with a diagnostic.
        return True
    try:
        with open(file_path, "rb") as handle:
            head = handle.read(_HEAD_BYTES)
    except OSError:
        return False
    found = recipients(head.decode("utf-8", errors="replace"))
    return found is None or NEXUS_AGENT_ID in found
