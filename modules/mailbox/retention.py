"""Retention for the Git-backed mailbox: prune what is finished.

The mailbox is a queue, but nothing ever drained it: outbox responses
accumulated indefinitely, and an inbox message that exhausted its retries
stayed on disk forever, re-read and re-skipped on every single poll
without anyone being told (Issue #129).

Pruning lives here - inside the process that owns the mailbox SQLite
state - rather than in an external cron job or a `git rm` script, because
deciding what is safe to delete requires that state. An outbox file whose
record has not finished publishing is still *needed*: the publishers and
``MailboxProcessor.retry_pending_git_commits()`` re-read it to finish the
commit/push. Deleting it would strand that record permanently, and a
git-side script could not even see the problem.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import MAX_RETRY, STATUS_DONE

logger = logging.getLogger(__name__)

ENV_RETENTION_DAYS = "NEXUS_MAILBOX_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 5

# Upper bound on one poll's deletions, so a first run against a long
# backlog still produces a reviewable commit instead of an enormous one.
# Whatever is left over is picked up by the next poll.
MAX_DELETIONS_PER_POLL = 500

# Outbox names are built by ``_outbox_filename`` and always start with a
# ``YYYYMMDDHHMMSS`` stamp.
_TIMESTAMP_PREFIX_LENGTH = 14


def retention_days(raw: str | None = None) -> int:
    """Configured retention window in days; ``0`` disables pruning.

    Anything unparsable or negative is treated as ``0`` (disabled) rather
    than falling back to the default: a typo in the environment must not
    silently start deleting messages.
    """
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

    @property
    def paths(self) -> list[Path]:
        return [*self.outbox, *self.inbox]

    def __bool__(self) -> bool:
        return bool(self.outbox or self.inbox)

    def summary(self) -> str:
        parts = []
        if self.outbox:
            parts.append(f"{len(self.outbox)} outbox")
        if self.inbox:
            parts.append(f"{len(self.inbox)} inbox")
        return ", ".join(parts) or "nothing"


def message_timestamp(name: str) -> datetime | None:
    """Parse an outbox filename's leading ``YYYYMMDDHHMMSS`` stamp.

    Returns ``None`` for anything that does not carry one, which callers
    treat as "do not touch": an unrecognized file is never old enough to
    delete, because its age cannot be established.
    """
    prefix = name[:_TIMESTAMP_PREFIX_LENGTH]
    if len(prefix) < _TIMESTAMP_PREFIX_LENGTH or not prefix.isdigit():
        return None
    try:
        return datetime.strptime(prefix, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def protected_outbox_names(conn: sqlite3.Connection) -> set[str]:
    """Outbox files that are still needed and must never be pruned.

    Three producers write to the outbox, each with its own record of
    whether publication finished:

    * ``mailbox_messages`` - anything not yet ``done`` still needs its file
      for ``retry_pending_git_commits()`` to finalize the commit.
    * ``operation_chains`` and ``mailbox_task_approvals`` - any
      ``publish_status`` other than ``pushed`` means a publisher will
      re-read the file to finish writing, committing or pushing it.
    """
    protected: set[str] = set()

    if _table_exists(conn, "mailbox_messages"):
        rows = conn.execute(
            "SELECT outbox_file FROM mailbox_messages "
            "WHERE outbox_file IS NOT NULL AND status != ?",
            (STATUS_DONE,),
        ).fetchall()
        protected.update(Path(str(row[0])).name for row in rows if row[0])

    for table in ("operation_chains", "mailbox_task_approvals"):
        if not _table_exists(conn, table):
            continue
        try:
            rows = conn.execute(
                f"SELECT outbox_file FROM {table} "  # noqa: S608 - fixed table names
                "WHERE outbox_file IS NOT NULL AND publish_status != 'pushed'"
            ).fetchall()
        except sqlite3.OperationalError:
            # An older schema without publish tracking: protect everything
            # this table references rather than guessing it is finished.
            rows = conn.execute(
                f"SELECT outbox_file FROM {table} WHERE outbox_file IS NOT NULL"  # noqa: S608
            ).fetchall()
        protected.update(Path(str(row[0])).name for row in rows if row[0])

    return protected


def exhausted_inbox_paths(conn: sqlite3.Connection) -> list[Path]:
    """Inbox messages that can no longer make progress.

    A message that reached ``MAX_RETRY`` is terminal: every later poll
    re-reads and re-skips it forever. Those are returned so the caller can
    log why and remove them from the queue.
    """
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
    """Decide what to delete, without touching the filesystem.

    Kept separate from the deletion itself so the policy is testable on
    its own, and so a caller can log exactly what is about to go.
    """
    if days <= 0:
        return PruneResult()

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    protected = protected_outbox_names(conn)

    stale_outbox: list[Path] = []
    if outbox.is_dir():
        for path in sorted(outbox.iterdir()):
            if not path.is_file() or path.suffix != ".md":
                continue
            if path.name in protected:
                continue
            stamp = message_timestamp(path.name)
            if stamp is None or stamp >= cutoff:
                continue
            stale_outbox.append(path)

    stale_inbox = [
        path for path in exhausted_inbox_paths(conn)
        if path.exists() and path.parent == inbox
    ]

    truncated = False
    budget = max(0, max_deletions)
    if len(stale_outbox) + len(stale_inbox) > budget:
        truncated = True
        # Inbox messages come first: they are actively re-processed on
        # every poll, so draining them matters more than reclaiming space.
        keep_inbox = stale_inbox[:budget]
        stale_outbox = stale_outbox[: max(0, budget - len(keep_inbox))]
        stale_inbox = keep_inbox

    return PruneResult(
        outbox=stale_outbox,
        inbox=stale_inbox,
        protected=len(protected),
        truncated=truncated,
    )
