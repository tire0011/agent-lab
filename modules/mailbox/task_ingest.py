"""Idempotent mailbox task ingestion (#378). Every task carries an idempotency key
under a unique index on ``mailbox_task_approvals.idempotency_key``, so redelivering a
message (retried poll, webhook, second API call) returns the existing task instead of
creating another. The key derives from the source message id unless given explicitly;
explicit keys are namespaced so they cannot collide with derived ones. Kept free of
service imports because both ``processing`` and ``task_service`` use it.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone

# Must match the backfill in ``modules/run_store/schema.py``.
DERIVED_KEY_PREFIX = "mailbox-msg:"
# A verified trusted sender's task goes straight to Nexus; anything else waits for
# owner approval and is never run by the worker.
_STATUS_QUEUED = "queued"
_STATUS_PENDING_APPROVAL = "pending_approval"
_ALLOWED_STATUSES = frozenset({_STATUS_QUEUED, _STATUS_PENDING_APPROVAL})
EXPLICIT_KEY_PREFIX = "client:"
MAX_EXPLICIT_KEY_LENGTH = 200
_EXPLICIT_KEY_RE = re.compile(r"^[\x21-\x7e]+$")


def derive_idempotency_key(mailbox_msg_id: int) -> str:
    return f"{DERIVED_KEY_PREFIX}{int(mailbox_msg_id)}"


def resolve_idempotency_key(mailbox_msg_id: int, explicit_key: str | None = None) -> str:
    """The stored key: the caller's explicit key if given, else derived from the message."""
    if explicit_key is None:
        return derive_idempotency_key(mailbox_msg_id)
    key = str(explicit_key).strip()
    if not key:
        raise ValueError("idempotency key must not be empty")
    if len(key) > MAX_EXPLICIT_KEY_LENGTH:
        raise ValueError(f"idempotency key longer than {MAX_EXPLICIT_KEY_LENGTH} characters")
    if not _EXPLICIT_KEY_RE.match(key):
        raise ValueError("idempotency key must be printable ASCII without whitespace")
    return f"{EXPLICIT_KEY_PREFIX}{key}"


def insert_task_approval(
    conn: sqlite3.Connection,
    *,
    mailbox_msg_id: int,
    task_body: str,
    sender: str,
    file_path: str,
    received_at: str,
    idempotency_key: str | None = None,
    status: str = _STATUS_QUEUED,
) -> tuple[str, bool]:
    """Create the pending task for a message, or find the one it already created;
    returns ``(approval_id, created)``. Insert and duplicate check are one statement,
    so concurrent deliveries cannot both create a task - the unique index lets one
    insert through and the others read that winner back. ``status`` is ``queued``
    only for gate-verified trusted senders (others get ``pending_approval``, never
    run); the no-merge constraint (#185) is recorded in the same transaction, and a
    failure there rolls the task back with it."""
    from modules.run_store.schema import record_merge_constraint

    if status not in _ALLOWED_STATUSES:
        raise ValueError(f"unknown task approval status {status!r}")
    key = resolve_idempotency_key(mailbox_msg_id, idempotency_key)
    approval_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        inserted = conn.execute(
            """INSERT INTO mailbox_task_approvals
               (id, mailbox_msg_id, task_body, sender, file_path, received_at,
                status, idempotency_key, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (approval_id, mailbox_msg_id, task_body or "", sender, file_path,
             received_at, status, key, now, now),
        ).rowcount
        if inserted:
            record_merge_constraint(conn, approval_id, task_body)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if inserted:
        return approval_id, True
    row = conn.execute(
        "SELECT id FROM mailbox_task_approvals WHERE idempotency_key = ?", (key,)
    ).fetchone()
    if row is None:  # pragma: no cover - the conflicting row cannot vanish
        raise RuntimeError(f"idempotency key {key!r} conflicted but no task holds it")
    return row[0], False
