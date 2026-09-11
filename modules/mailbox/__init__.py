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
    SUPPORTED_TYPES,
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
from .processing import (
    MailboxProcessor,
    _CHAIN_ACCEPTED,
    _ChainError,
    _parse_operation_chain_body,
    _process_operation_chain,
    _PROCESSORS,
)

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
    "SUPPORTED_TYPES",
    "_CHAIN_ACCEPTED",
    "_ChainError",
    "_DEFERRED_PROCESSING",
    "_atomic_write",
    "_build_outbox_content",
    "_git",
    "_has_secret",
    "_now_iso",
    "_outbox_filename",
    "_parse_operation_chain_body",
    "_process_operation_chain",
    "_PROCESSORS",
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

# Keep the historic ``modules.mailbox.task_service`` import surface while
# extending its service with the persisted post-verification lifecycle.
from .finalizing_task_service import install as _install_finalizing_task_service

_install_finalizing_task_service()
del _install_finalizing_task_service

# Install the failure-analysis diagnostics only after the finalizing mailbox
# class is behind the historic import surface.  This instruments both direct
# task failures and resumed/finalization paths without changing their lifecycle
# behavior or eagerly constructing an LLM client.
from modules.failure_analysis_observability import install as _install_failure_analysis_observability

_install_failure_analysis_observability()
del _install_failure_analysis_observability
