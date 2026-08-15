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
        "--code-repo-path",
        default=None,
        help=(
            "Path to the code repository root "
            "(default: AGENT_CODE_REPO_PATH env var, or /agent). "
            "Artifacts like IDEAS.md and state/runs live here."
        ),
    )
    parser.add_argument(
        "--mailbox-repo-path",
        default=None,
        help=(
            "Path to the mailbox repository root "
            "(default: AGENT_MAILBOX_REPO_PATH env var, or /agent). "
            "Git operations for inbox/outbox messages run here."
        ),
    )
    parser.add_argument(
        "--repo-path",
        default=None,
        help=(
            "Backward-compatible alias: sets both --code-repo-path and "
            "--mailbox-repo-path when neither is specified explicitly."
        ),
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

    # Resolve repo paths, giving explicit flags priority over --repo-path
    # and env vars.
    explicit_code = args.code_repo_path is not None
    explicit_mailbox = args.mailbox_repo_path is not None
    explicit_alias = args.repo_path is not None

    if explicit_alias and (explicit_code or explicit_mailbox):
        parser.error(
            "--repo-path cannot be combined with --code-repo-path or "
            "--mailbox-repo-path; use the explicit flags instead."
        )

    if explicit_alias:
        code_repo_path = Path(args.repo_path)
        mailbox_repo_path = Path(args.repo_path)
    else:
        code_repo_path = Path(
            args.code_repo_path or os.environ.get("AGENT_CODE_REPO_PATH", "/agent")
        )
        mailbox_repo_path = Path(
            args.mailbox_repo_path or os.environ.get("AGENT_MAILBOX_REPO_PATH", "/agent")
        )

    lock_path = Path(
        args.lock_path or os.environ.get("AGENT_MAILBOX_LOCK_PATH", "/var/lib/nexus/mailbox.lock")
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    from modules.mailbox import MailboxLock, MailboxProcessor
    from modules.mailbox_config import MailboxConfig
    from modules.experiment_service import ExperimentService
    from modules.operation_chain_publisher import OperationChainPublisher
    from modules.operation_chain_service import OperationChainService
    from modules.operations_registry import OperationsRegistry

    mailbox_config = MailboxConfig.from_paths(code_repo_path, mailbox_repo_path)

    lock = MailboxLock(lock_path)
    if not lock.acquire():
        logger.error("Another mailbox processor is running; exiting")
        return 1

    try:
        experiment_service = ExperimentService(db_path=args.db_path)
        operations_registry = OperationsRegistry(
            experiment_service=experiment_service,
            db_path=args.db_path,
            repo_root=str(code_repo_path),
        )
        chain_service = OperationChainService(
            db_path=args.db_path,
            mailbox_config=mailbox_config,
            operations_registry=operations_registry,
        )
        processor = MailboxProcessor(
            repo_path=mailbox_repo_path,
            db_path=args.db_path,
            dry_run=args.dry_run,
            operation_chain_service=chain_service,
            mailbox_config=mailbox_config,
        )
        publisher = OperationChainPublisher(
            db_path=args.db_path,
            mailbox_config=mailbox_config,
        )
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
