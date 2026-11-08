#!/usr/bin/env python3
"""Mailbox processor CLI — process one batch of inbox messages."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from modules.mailbox.poll_runner import run_mailbox_poll_once
from modules.logging_config import configure_logging
from modules.redaction import sanitized_reason
from modules.run_store import DEFAULT_DB_PATH

configure_logging()
logger = logging.getLogger("mailbox_processor")


_PERMANENT_SKIP_MARKERS = ("max retries",)


def _is_permanent_skip(reason: str | None) -> bool:
    """Whether a skip reason holds on every future poll (exhausted retries do; already-done does not)."""
    text = (reason or "").lower()
    return any(marker in text for marker in _PERMANENT_SKIP_MARKERS)


def _resolve_repo_paths(
    code_repo_path_arg: str | None,
    mailbox_repo_path_arg: str | None,
    repo_path_arg: str | None,
) -> tuple[Path, Path]:
    """Resolve the code and mailbox repository paths from CLI flags and env vars.

    Explicit flags win over ``--repo-path`` and env vars; the code repo
    defaults to ``/agent``, the mailbox repo must be given. ``--repo-path``
    with either explicit flag raises :exc:`ValueError`. Returns
    ``(code_repo_path, mailbox_repo_path)``.
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

    mailbox_value = mailbox_repo_path_arg or os.environ.get("AGENT_MAILBOX_REPO_PATH")
    if not mailbox_value or not mailbox_value.strip():
        raise ValueError(
            "mailbox repository path is required; use --mailbox-repo-path or "
            "set AGENT_MAILBOX_REPO_PATH"
        )
    return (
        Path(code_repo_path_arg or os.environ.get("AGENT_CODE_REPO_PATH", "/agent")),
        Path(mailbox_value),
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
            "(default: required AGENT_MAILBOX_REPO_PATH env var). "
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

    from modules.mailbox.config import MailboxConfig

    mailbox_config = MailboxConfig.from_paths(code_repo_path, mailbox_repo_path)
    result = run_mailbox_poll_once(
        db_path=args.db_path,
        mailbox_config=mailbox_config,
        lock_path=lock_path,
        dry_run=args.dry_run,
    )

    if result.status == "busy":
        # A held lock is the timer doing its job (the holder rescans before
        # exiting), so this is not a failure.
        logger.info("Mailbox poll skipped: another processor is already running")
        return 0
    if result.status == "dirty_worktree":
        logger.error("Working tree is dirty; refusing mailbox processing")
        return 1
    if result.status == "pull_failed":
        logger.error("git pull --ff-only failed; refusing mailbox processing")
        return 1
    if result.status == "sync_failed":
        # Reported below: completed messages are still worth logging. Unlike a
        # held lock this is a real fault (checkout out of step with its remote).
        logger.error("Mailbox sync with the remote failed; remaining work is left for the next run")
    if result.status == "error":
        logger.error("Mailbox processing failed: %s", sanitized_reason(result.error) or "unknown")
        return 1

    for r in result.results:
        status = r.get("status", "?")
        file_ = r.get("file", "?")
        if status == "done":
            logger.info("done: %s -> %s", file_, r.get("outbox", ""))
        elif status == "skipped":
            # Permanently skipped messages are logged at DEBUG; INFO each poll
            # would flood the feed. The count stays in the summary.
            level = logging.DEBUG if _is_permanent_skip(r.get("reason")) else logging.INFO
            logger.log(level, "skipped: %s (%s)", file_, r.get("reason", ""))
        else:
            logger.warning("failed: %s: %s", file_, r.get("error", ""))

    done = sum(1 for r in result.results if r.get("status") == "done")
    failed = sum(1 for r in result.results if r.get("status") == "failed")
    skipped = sum(1 for r in result.results if r.get("status") == "skipped")
    logger.info(
        "Processed %d message(s): %d done, %d failed, %d skipped",
        len(result.results), done, failed, skipped,
    )
    return 1 if result.status == "sync_failed" else 0


if __name__ == "__main__":
    sys.exit(main())
