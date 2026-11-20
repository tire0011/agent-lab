"""Retention for the Git-backed mailbox: prune what is finished (#129). Pruning
lives inside the process that owns the mailbox SQLite state, not in an external cron
or `git rm` script, because deciding what is safe to delete requires that state: an
outbox file whose record has not finished publishing is still *needed* by the
publishers and ``retry_pending_git_commits()`` - deleting it would strand that
record permanently.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import MAX_RETRY, STATUS_DONE, _git

logger = logging.getLogger(__name__)

ENV_RETENTION_DAYS = "NEXUS_MAILBOX_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 5

# Upper bound on one poll's deletions, so a first run against a long backlog still
# produces a reviewable commit; the rest is picked up by the next poll.
MAX_DELETIONS_PER_POLL = 500

# Outbox names built by ``_outbox_filename`` start with a ``YYYYMMDDHHMMSS`` stamp.
_TIMESTAMP_PREFIX_LENGTH = 14


def retention_days(raw: str | None = None) -> int:
    """Configured retention window in days; ``0`` disables pruning. Unparsable or
    negative values are treated as ``0``, not the default: a typo in the environment
    must not silently start deleting messages."""
    value = raw if raw is not None else os.environ.get(ENV_RETENTION_DAYS)
    if value is None or not str(value).strip():
        return DEFAULT_RETENTION_DAYS
    try:
        days = int(str(value).strip())
    except ValueError:
        logger.warning("ignoring invalid %s value; pruning disabled", ENV_RETENTION_DAYS)
        return 0
    return days if days > 0 else 0


@dataclass(frozen=True)
class PruneResult:
    """What one prune pass removed, for logging and commit messages."""

    outbox: list[Path] = field(default_factory=list)
    inbox: list[Path] = field(default_factory=list)
    protected: int = 0
    truncated: bool = False
    archive: list[Path] = field(default_factory=list)

    @property
    def paths(self) -> list[Path]:
        return [*self.outbox, *self.inbox, *self.archive]

    def __bool__(self) -> bool:
        return bool(self.outbox or self.inbox or self.archive)

    def summary(self) -> str:
        parts = []
        if self.outbox:
            parts.append(f"{len(self.outbox)} outbox")
        if self.inbox:
            parts.append(f"{len(self.inbox)} inbox")
        if self.archive:
            parts.append(f"{len(self.archive)} archive")
        return ", ".join(parts) or "nothing"


def message_timestamp(name: str) -> datetime | None:
    """Parse an outbox filename's leading ``YYYYMMDDHHMMSS`` stamp, if present."""
    prefix = name[:_TIMESTAMP_PREFIX_LENGTH]
    if len(prefix) < _TIMESTAMP_PREFIX_LENGTH or not prefix.isdigit():
        return None
    try:
        return datetime.strptime(prefix, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _last_modified(path: Path) -> datetime | None:
    """Use Git history so a fresh checkout preserves the file's age."""
    try:
        result = _git(
            ["--literal-pathspecs", "log", "-1", "--format=%cI", "--", path.name],
            path.parent,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return datetime.fromisoformat(result.stdout.strip())
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except (OSError, ValueError):
        logger.warning("could not determine retention age for %s", path.name)
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def protected_outbox_names(conn: sqlite3.Connection) -> set[str]:
    """Outbox files that are still needed and must never be pruned: ``mailbox_messages``
    rows not yet ``done`` (``retry_pending_git_commits()`` re-reads their file), and
    publisher rows whose ``publish_status`` is not ``pushed``."""
    protected: set[str] = set()

    if _table_exists(conn, "mailbox_messages"):
        rows = conn.execute(
            "SELECT outbox_file FROM mailbox_messages "
            "WHERE outbox_file IS NOT NULL AND status != ?",
            (STATUS_DONE,),
        ).fetchall()
        protected.update(Path(str(row[0])).name for row in rows if row[0])

    for table in ("operation_chains", "mailbox_task_approvals", "failure_analyses"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "outbox_file" not in columns:
            continue
        # Older schemas without publish tracking still protect every referenced file.
        unfinished = " AND publish_status IS NOT 'pushed'" if "publish_status" in columns else ""
        rows = conn.execute(
            f"SELECT outbox_file FROM {table} WHERE outbox_file IS NOT NULL{unfinished}"  # noqa: S608
        )
        protected.update(Path(str(row[0])).name for row in rows if row[0])

    return protected


def exhausted_inbox_paths(conn: sqlite3.Connection) -> list[Path]:
    """Inbox messages that can no longer make progress: a message at ``MAX_RETRY`` is
    terminal and would be re-read and re-skipped on every poll forever, so they are
    returned for the caller to log and remove from the queue."""
    if not _table_exists(conn, "mailbox_messages"):
        return []
    rows = conn.execute(
        "SELECT file_path FROM mailbox_messages "
        "WHERE status != ? AND retry_count >= ? AND file_path IS NOT NULL",
        (STATUS_DONE, MAX_RETRY),
    ).fetchall()
    return [Path(str(row[0])) for row in rows if row[0]]


def plan_prune(
    conn: sqlite3.Connection,
    *,
    inbox: Path,
    outbox: Path,
    days: int,
    now: datetime | None = None,
    max_deletions: int = MAX_DELETIONS_PER_POLL,
) -> PruneResult:
    """Decide what to delete, without touching the filesystem - kept separate so the
    policy is testable on its own and the caller can log exactly what will go."""
    if days <= 0:
        return PruneResult()

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    protected = protected_outbox_names(conn)

    stale_outbox: list[Path] = []
    if outbox.is_dir():
        for path in sorted(outbox.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix != ".md":
                continue
            if path.name in protected:
                continue
            stamp = message_timestamp(path.name) or _last_modified(path)
            if stamp is None or stamp >= cutoff:
                continue
            stale_outbox.append(path)

    stale_inbox = [
        path for path in exhausted_inbox_paths(conn)
        if path.exists() and path.parent == inbox
    ]

    archive = outbox.parent / "archive"
    stale_archive: list[Path] = []
    if archive.is_dir() and not archive.is_symlink():
        for path in sorted(archive.rglob("*")):
            if path.is_symlink() or not path.is_file() or path.name == ".gitkeep":
                continue
            if path.name in protected:
                continue
            stamp = _last_modified(path)
            if stamp is not None and stamp < cutoff:
                stale_archive.append(path)

    budget = max(0, max_deletions)
    truncated = len(stale_outbox) + len(stale_inbox) + len(stale_archive) > budget
    # Drain terminal inbox messages first, then reclaim space in outbox and archive.
    stale_inbox = stale_inbox[:budget]
    budget -= len(stale_inbox)
    stale_outbox = stale_outbox[:budget]
    budget -= len(stale_outbox)

    return PruneResult(
        outbox=stale_outbox,
        inbox=stale_inbox,
        protected=len(protected),
        truncated=truncated,
        archive=stale_archive[:budget],
    )
