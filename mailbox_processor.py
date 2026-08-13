#!/usr/bin/env python3
"""Mailbox processor CLI — process one batch of inbox messages."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mailbox_processor")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        default="/agent/state/agent.db",
        help="Path to SQLite database (default: /agent/state/agent.db)",
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
    args = parser.parse_args(argv)

    repo_path = Path(args.repo_path)
    lock_path = repo_path / "state" / "mailbox.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    from modules.mailbox import MailboxLock, MailboxProcessor

    lock = MailboxLock(lock_path)
    if not lock.acquire():
        logger.error("Another mailbox processor is running; exiting")
        return 1

    try:
        processor = MailboxProcessor(
            repo_path=repo_path,
            db_path=args.db_path,
            dry_run=args.dry_run,
        )
        try:
            # 1. Retry any previously committed-but-not-pushed commits
            if not args.dry_run:
                processor.retry_unpushed_commits()

            # 2. git pull --ff-only when safe
            if not args.dry_run:
                processor.pull()

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
    finally:
        lock.release()

    return 0


if __name__ == "__main__":
    sys.exit(main())
