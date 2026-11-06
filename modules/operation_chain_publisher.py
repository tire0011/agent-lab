"""Persistent publisher for completed operation-chain results.

This module is kept as a thin backward-compatible shim.  All git logic now
lives in ``modules.outbox_publisher.OperationChainOutboxPublisher``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modules.outbox_publisher import OperationChainOutboxPublisher
from modules.run_store import DEFAULT_DB_PATH

class OperationChainPublisher:
    """Backward-compatible shim that delegates to OperationChainOutboxPublisher."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        mailbox_config: Any = None,
        notifier: Any = None,
    ) -> None:
        self._inner = OperationChainOutboxPublisher(
            mailbox_config=mailbox_config,
            db_path=db_path,
            repo_root=repo_root,
            notifier=notifier,
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
