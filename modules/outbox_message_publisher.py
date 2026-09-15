"""Canonical outbox-message publishing.

Producers provide only an :class:`OutboxMessage` (logical id + rendered content).
This layer owns filename/path creation below ``MailboxConfig.outbox_dir`` and
then delegates the durable git lifecycle to ``modules.outbox_publisher``.

The legacy ``outbox_file`` columns remain persisted for recovery/UI/retention,
but they are outputs of this publisher rather than caller-controlled inputs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from modules.outbox_publisher import (
    PUBLISH_COMMITTED,
    PUBLISH_COMMITTING,
    PUBLISH_FAILED,
    PUBLISH_PENDING_WRITE,
    PUBLISH_WRITTEN,
    FailureAnalysisOutboxPublisher as _LegacyFailureAnalysisOutboxPublisher,
    OperationChainOutboxPublisher as _LegacyOperationChainOutboxPublisher,
    PublishRecord,
    TaskApprovalOutboxPublisher as _LegacyTaskApprovalOutboxPublisher,
)

_PENDING_COMPAT = "pending"
_ACTIONABLE_STATUSES = (
    PUBLISH_PENDING_WRITE,
    PUBLISH_WRITTEN,
    _PENDING_COMPAT,
    PUBLISH_COMMITTING,
    PUBLISH_COMMITTED,
    PUBLISH_FAILED,
)


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """Logical message handed to the outbox processor.

    ``id`` is correlation identity, not a filesystem path. ``content`` is the
    complete UTF-8 markdown payload. Callers never choose a filename.
    """

    id: str
    content: str


class _CanonicalMessagePublisherMixin:
    """Convert id+content objects to safe canonical outbox files."""

    def _canonical_relative_path(self, message_id: str) -> str:
        # Hash the logical id instead of sanitising caller text into a path.
        # This makes path traversal impossible by construction and keeps the
        # filename stable across retries/restarts.
        digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
        path = self._outbox_dir / f"message-{digest}.md"

        outbox_root = self._outbox_dir.resolve()
        resolved_parent = path.parent.resolve()
        if resolved_parent != outbox_root:
            raise ValueError("canonical outbox path escapes configured outbox directory")
        if path.is_symlink():
            raise ValueError("symlinks are not allowed")
        try:
            rel = path.relative_to(self._repo_root)
        except ValueError as exc:
            raise ValueError("configured outbox directory escapes mailbox repository") from exc
        return rel.as_posix()

    def _persist_message(self, message: OutboxMessage, outbox_file: str) -> None:
        self._conn.execute(
            f"""UPDATE {self._TABLE}
                SET outbox_file = ?, outbox_content = ?, updated_at = datetime('now')
                WHERE {self._ID_COLUMN} = ?""",
            (outbox_file, message.content, message.id),
        )
        self._conn.commit()

    def _set_status(self, message_id: str, status: str) -> None:
        self._conn.execute(
            f"""UPDATE {self._TABLE}
                SET publish_status = ?, updated_at = datetime('now')
                WHERE {self._ID_COLUMN} = ?""",
            (status, message_id),
        )
        self._conn.commit()

    def _record_from_message(
        self,
        *,
        message: OutboxMessage,
        publish_status: str,
        publish_commit_sha: str | None,
        publish_retry_count: int,
    ) -> PublishRecord:
        outbox_file = self._canonical_relative_path(message.id)
        self._persist_message(message, outbox_file)

        # Old rows may say written/committing/failed while their historical
        # caller-selected path was rejected. If our canonical file is absent
        # and no commit exists, recovery must restart at the write stage.
        path = self._repo_root / outbox_file
        status = publish_status
        if (
            publish_commit_sha is None
            and status in (PUBLISH_WRITTEN, _PENDING_COMPAT, PUBLISH_COMMITTING, PUBLISH_FAILED)
            and not path.is_file()
        ):
            status = PUBLISH_PENDING_WRITE
            self._set_status(message.id, status)

        return PublishRecord(
            record_id=message.id,
            outbox_file=outbox_file,
            publish_status=status,
            publish_commit_sha=publish_commit_sha,
            publish_retry_count=publish_retry_count,
            content_fn=lambda content=message.content: content,
        )

    def publish_message(
        self,
        message: OutboxMessage,
        *,
        publish_status: str = PUBLISH_PENDING_WRITE,
        publish_commit_sha: str | None = None,
        publish_retry_count: int = 0,
    ) -> bool:
        """Publish one logical message; callers never supply a path."""
        record = self._record_from_message(
            message=message,
            publish_status=publish_status,
            publish_commit_sha=publish_commit_sha,
            publish_retry_count=publish_retry_count,
        )
        return self.publish(record)

    def fetch_pending(self) -> list[PublishRecord]:
        rows = self._conn.execute(
            f"""SELECT {self._ID_COLUMN} AS record_id, outbox_file, outbox_content,
                       publish_status, publish_commit_sha, publish_retry_count
                FROM {self._TABLE}
                WHERE publish_status IN (?, ?, ?, ?, ?, ?)
                ORDER BY created_at ASC""",
            _ACTIONABLE_STATUSES,
        ).fetchall()

        records: list[PublishRecord] = []
        for row in rows:
            content = row["outbox_content"]
            if content is not None:
                records.append(
                    self._record_from_message(
                        message=OutboxMessage(id=row["record_id"], content=content),
                        publish_status=row["publish_status"],
                        publish_commit_sha=row["publish_commit_sha"],
                        publish_retry_count=row["publish_retry_count"] or 0,
                    )
                )
                continue

            # Backward compatibility for old persisted rows that predate
            # outbox_content. They still use their already-persisted path.
            legacy_path = row["outbox_file"]
            if legacy_path:
                records.append(
                    PublishRecord(
                        record_id=row["record_id"],
                        outbox_file=legacy_path,
                        publish_status=row["publish_status"],
                        publish_commit_sha=row["publish_commit_sha"],
                        publish_retry_count=row["publish_retry_count"] or 0,
                        content_fn=None,
                    )
                )
        return records

    def publish_pending(self) -> int:
        return self.publish_many(self.fetch_pending())


class OperationChainMessagePublisher(
    _CanonicalMessagePublisherMixin,
    _LegacyOperationChainOutboxPublisher,
):
    """Canonical id+content publisher for operation-chain results."""


class TaskApprovalMessagePublisher(
    _CanonicalMessagePublisherMixin,
    _LegacyTaskApprovalOutboxPublisher,
):
    """Canonical id+content publisher for mailbox task results."""


class FailureAnalysisMessagePublisher(
    _CanonicalMessagePublisherMixin,
    _LegacyFailureAnalysisOutboxPublisher,
):
    """Canonical id+content publisher for failure-analysis notifications."""
