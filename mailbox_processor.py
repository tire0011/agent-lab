#!/usr/bin/env python3
"""Mailbox processor CLI — process one batch of inbox messages."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from modules.mailbox_poll_runner import run_mailbox_poll_once
from modules.run_store import DEFAULT_DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mailbox_processor")


def _resolve_repo_paths(
    code_repo_path_arg: str | None,
    mailbox_repo_path_arg: str | None,
    repo_path_arg: str | None,
) -> tuple[Path, Path]:
    """Resolve code and mailbox repository paths from CLI arguments and env vars.

    Priority: explicit flags > ``--repo-path`` alias > environment variables >
    ``/agent`` default.  Raises :exc:`ValueError` if ``--repo-path`` is
    combined with ``--code-repo-path`` or ``--mailbox-repo-path``.

    Returns ``(code_repo_path, mailbox_repo_path)``.
    """
    explicit_alias = repo_path_arg is not None
    explicit_code = code_repo_path_arg is not None
    explicit_mailbox = mailbox_repo_path_arg is not None

    if explicit_alias and (explicit_code or explicit_mailbox):
        raise ValueError(
            "--repo-path cannot be combined with --code-repo-path or "
            "--mailbox-repo-path; use the explicit flags instead."
        )

    if explicit_alias:
        p = Path(repo_path_arg)
        return p, p

    return (
        Path(code_repo_path_arg or os.environ.get("AGENT_CODE_REPO_PATH", "/agent")),
        Path(mailbox_repo_path_arg or os.environ.get("AGENT_MAILBOX_REPO_PATH", "/agent")),
    )


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

    # Resolve repo paths via the module-level helper so that the same logic
    # is reachable from tests without starting the full processor.
    try:
        code_repo_path, mailbox_repo_path = _resolve_repo_paths(
            args.code_repo_path, args.mailbox_repo_path, args.repo_path
        )
    except ValueError as exc:
        parser.error(str(exc))

    lock_path = Path(
        args.lock_path or os.environ.get("AGENT_MAILBOX_LOCK_PATH", "/var/lib/nexus/mailbox.lock")
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    from modules.mailbox_config import MailboxConfig

    mailbox_config = MailboxConfig.from_paths(code_repo_path, mailbox_repo_path)
    result = run_mailbox_poll_once(
        db_path=args.db_path,
        mailbox_config=mailbox_config,
        lock_path=lock_path,
        dry_run=args.dry_run,
    )

    if result.status == "busy":
        logger.error("Another mailbox processor is running; exiting")
        return 1
    if result.status == "dirty_worktree":
        logger.error("Working tree is dirty; refusing mailbox processing")
        return 1
    if result.status == "pull_failed":
        logger.error("git pull --ff-only failed; refusing mailbox processing")
        return 1
    if result.status == "error":
        logger.error("Mailbox processing failed: %s", result.error or "unknown")
        return 1

    for r in result.results:
        status = r.get("status", "?")
        file_ = r.get("file", "?")
        if status == "done":
            logger.info("done: %s -> %s", file_, r.get("outbox", ""))
        elif status == "skipped":
            logger.info("skipped: %s (%s)", file_, r.get("reason", ""))
        else:
            logger.warning("failed: %s: %s", file_, r.get("error", ""))

    done = sum(1 for r in result.results if r.get("status") == "done")
    failed = sum(1 for r in result.results if r.get("status") == "failed")
    logger.info("Processed %d message(s): %d done, %d failed", len(result.results), done, failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
