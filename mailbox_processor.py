#!/usr/bin/env python3
"""Mailbox processor CLI — process one batch of inbox messages."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from modules.run_store import DEFAULT_DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mailbox_processor")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--repo-path",
        default="/agent",
        help="Path to the git repository root (default: /agent)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate messages but do not write files or commit",
    )
    parser.add_argument(
        "--lock-path",
        default=None,
        help="Path to mailbox lock file (default: AGENT_MAILBOX_LOCK_PATH or /var/lib/nexus/mailbox.lock)",
    )
    args = parser.parse_args(argv)

    repo_path = Path(args.repo_path)
    lock_path = Path(
        args.lock_path or os.environ.get("AGENT_MAILBOX_LOCK_PATH", "/var/lib/nexus/mailbox.lock")
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    from modules.mailbox import MailboxLock, MailboxProcessor
    from modules.experiment_service import ExperimentService
    from modules.operation_chain_publisher import OperationChainPublisher
    from modules.operation_chain_service import OperationChainService
    from modules.operations_registry import OperationsRegistry

    lock = MailboxLock(lock_path)
    if not lock.acquire():
        logger.error("Another mailbox processor is running; exiting")
        return 1

    try:
        experiment_service = ExperimentService(db_path=args.db_path)
        operations_registry = OperationsRegistry(
            experiment_service=experiment_service,
            db_path=args.db_path,
            repo_root=str(repo_path),
        )
        chain_service = OperationChainService(
            db_path=args.db_path,
            repo_root=str(repo_path),
            outbox_dir=str(repo_path / "communication" / "outbox"),
            operations_registry=operations_registry,
        )
        processor = MailboxProcessor(
            repo_path=repo_path,
            db_path=args.db_path,
            dry_run=args.dry_run,
            operation_chain_service=chain_service,
        )
        publisher = OperationChainPublisher(db_path=args.db_path, repo_root=str(repo_path))
        try:
            # 1. Retry any previously committed-but-not-pushed commits
            if not args.dry_run:
                publisher.publish_pending()
                processor.retry_unpushed_commits()
                processor.retry_pending_git_commits()
                if not processor.ensure_clean_worktree():
                    logger.error("Working tree is dirty; refusing mailbox processing")
                    return 1

            # 2. git pull --ff-only when safe
            if not args.dry_run:
                if not processor.pull():
                    logger.error("git pull --ff-only failed; refusing mailbox processing")
                    return 1

            # 3. Process inbox messages
            results = processor.process_inbox()

            for r in results:
                status = r.get("status", "?")
                file_ = r.get("file", "?")
                if status == "done":
                    logger.info("done: %s -> %s", file_, r.get("outbox", ""))
                elif status == "skipped":
                    logger.info("skipped: %s (%s)", file_, r.get("reason", ""))
                else:
                    logger.warning("failed: %s: %s", file_, r.get("error", ""))

            done = sum(1 for r in results if r.get("status") == "done")
            failed = sum(1 for r in results if r.get("status") == "failed")
            logger.info("Processed %d message(s): %d done, %d failed", len(results), done, failed)

        finally:
            processor.close()
            chain_service.close()
            publisher.close()
            operations_registry.close()
    finally:
        lock.release()

    return 0


if __name__ == "__main__":
    sys.exit(main())
