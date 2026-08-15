"""Persistent publisher for completed operation-chain results.

This module is kept as a thin backward-compatible shim.  All git logic now
lives in ``modules.outbox_publisher.OperationChainOutboxPublisher``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modules.outbox_publisher import (
    PUBLISH_COMMITTED,
    PUBLISH_FAILED,
    PUBLISH_PUSHED,
    PUBLISH_WRITTEN,
    OperationChainOutboxPublisher,
)
from modules.run_store import DEFAULT_DB_PATH

# ---------------------------------------------------------------------------
# Backward-compatible state aliases
# ---------------------------------------------------------------------------

PUBLISH_PENDING = "pending"      # legacy alias for PUBLISH_WRITTEN
PUBLISH_COMMITTING = "committing"
# Re-export for tests and other callers.
PUBLISH_COMMITTED = PUBLISH_COMMITTED  # noqa: F811
PUBLISH_PUSHED = PUBLISH_PUSHED        # noqa: F811
PUBLISH_FAILED = PUBLISH_FAILED        # noqa: F811


@dataclass(slots=True)
class PublishCandidate:
    """Legacy dataclass kept for backward compatibility."""

    request_id: str
    outbox_file: str
    publish_status: str
    publish_commit_sha: str | None
    publish_retry_count: int


class OperationChainPublisher:
    """Backward-compatible shim that delegates to OperationChainOutboxPublisher."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        mailbox_config: Any = None,
    ) -> None:
        self._inner = OperationChainOutboxPublisher(
            mailbox_config=mailbox_config,
            db_path=db_path,
            repo_root=repo_root,
        )

    def close(self) -> None:
        self._inner.close()

    def publish_pending(self) -> int:
        return self._inner.publish_pending()

    # Expose internal attributes for backward compatibility with tests.

    @property
    def repo_root(self) -> Path:
        return self._inner._repo_root

    @property
    def outbox_dir(self) -> Path:
        return self._inner._outbox_dir

    def _validate_outbox_path(self, outbox_file: str) -> Path:
        return self._inner._validate_outbox_path(outbox_file)
