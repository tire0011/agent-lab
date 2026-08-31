"""Backward-compatible mailbox façade.

This module keeps the historical import surface used across the codebase while
delegating the real implementation to the split modules below.
"""

from __future__ import annotations

from .core import (
    MAX_FILE_SIZE,
    MAX_RETRY,
    MAILBOX_MESSAGE_COLUMNS,
    MAILBOX_SCHEMA,
    STATUS_DEFERRED,
    STATUS_DONE,
    STATUS_DONE_PENDING_COMMIT,
    MailboxLock,
    MailboxMessage,
    _DEFERRED_PROCESSING,
    _atomic_write,
    _build_outbox_content,
    _git,
    _has_secret,
    _now_iso,
    _outbox_filename,
    git_add_and_commit,
    git_current_commit,
    git_dirty_entries,
    git_is_clean,
    git_pull_ff_only,
    git_push,
    init_mailbox_db,
    list_outbox_files,
    list_recent_db_messages,
    parse_front_matter,
    read_inbox_preview,
    redact,
)
from .processing import MailboxProcessor

__all__ = [
    "MAX_FILE_SIZE",
    "MAX_RETRY",
    "MAILBOX_MESSAGE_COLUMNS",
    "MAILBOX_SCHEMA",
    "STATUS_DEFERRED",
    "STATUS_DONE",
    "STATUS_DONE_PENDING_COMMIT",
    "MailboxLock",
    "MailboxMessage",
    "MailboxProcessor",
    "_DEFERRED_PROCESSING",
    "_atomic_write",
    "_build_outbox_content",
    "_git",
    "_has_secret",
    "_now_iso",
    "_outbox_filename",
    "git_add_and_commit",
    "git_current_commit",
    "git_dirty_entries",
    "git_is_clean",
    "git_pull_ff_only",
    "git_push",
    "init_mailbox_db",
    "list_outbox_files",
    "list_recent_db_messages",
    "parse_front_matter",
    "read_inbox_preview",
    "redact",
]
