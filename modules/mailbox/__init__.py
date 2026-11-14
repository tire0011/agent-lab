"""Backward-compatible mailbox facade: keeps the historical import surface while
delegating to the split modules below.
"""

from __future__ import annotations

from .core import (
    DEFAULT_AGENT_ID,
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
    agent_id_from_env,
    git_add_and_commit,
    git_current_commit,
    git_dirty_entries,
    git_is_clean,
    git_pull_ff_only,
    git_push,
    init_mailbox_db,
    is_addressed_to,
    list_outbox_files,
    list_recent_db_messages,
    parse_front_matter,
    peek_front_matter,
    read_inbox_preview,
    recipients_of,
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
    "DEFAULT_AGENT_ID",
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
    "agent_id_from_env",
    "init_mailbox_db",
    "is_addressed_to",
    "list_outbox_files",
    "list_recent_db_messages",
    "parse_front_matter",
    "peek_front_matter",
    "read_inbox_preview",
    "recipients_of",
    "redact",
]

# Keep the historic ``modules.mailbox.task_service`` import surface while extending
# its service with the persisted post-verification lifecycle.
from .finalizing_task_service import install as _install_finalizing_task_service

_install_finalizing_task_service()
del _install_finalizing_task_service

# Install failure-analysis diagnostics after the finalizing class is behind the
# historic import surface; instruments task failures without changing lifecycle
# behavior or eagerly constructing an LLM client.
from modules.failure_analysis_observability import install as _install_failure_analysis_observability

_install_failure_analysis_observability()
del _install_failure_analysis_observability

# This package is imported before configure_logging() in the API/mailbox entry
# points; patch the service constructor now so it emits the startup feature-status
# event later, once central logging is active.
from modules.failure_analysis_startup_observability import install as _install_failure_analysis_startup_observability

_install_failure_analysis_startup_observability()
del _install_failure_analysis_startup_observability
