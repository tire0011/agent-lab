"""Shared, persistent publish lifecycle for all outbox results. Every async producer
supplies only an ``OutboxMessage`` (``id`` and ``content``); this module derives the
canonical path, atomically writes the file, commits the exact file, pushes, and
persists the state: pending_write -> written -> committing -> committed -> pushed,
failed retryable. Recovery is idempotent per the documented contract (re-write from
content, commit+push existing file, push existing commit, adopt matching untracked
file, never stage foreign files).

Security: only the configured mailbox repo/outbox is touched; path-traversal and
symlink protection on every path; exact pathspec on add/commit, never ``git add -A``;
no force-push; ``shell=False`` throughout; per-repo flock serialises across threads and
processes; no secrets/URLs in logs; atomic temp-file+rename writes; a publish error
never mutates the caller's business-logic DB state."""

from __future__ import annotations

import fcntl
import logging
import os
import sqlite3
import subprocess
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modules.redaction import redact as _redact_full
from modules.run_store import DEFAULT_DB_PATH, init_db

logger = logging.getLogger(__name__)


def git_push(repo_root: Any) -> bool:
    """Push the mailbox repo, resolving ``modules.mailbox`` at call time - imported lazily
    because the mailbox package __init__ transitively imports this module, and importing it
    first raised ImportError from the partially initialized module."""
    from modules.mailbox import git_push as _git_push

    return _git_push(repo_root)

# ---------------------------------------------------------------------------
# Lifecycle state constants
# ---------------------------------------------------------------------------

PUBLISH_PENDING_WRITE = "pending_write"
PUBLISH_WRITTEN = "written"
PUBLISH_COMMITTING = "committing"
PUBLISH_COMMITTED = "committed"
PUBLISH_PUSHED = "pushed"
PUBLISH_FAILED = "failed"

# Legacy alias: operation_chains used "pending" before this module existed; treat it
# as PUBLISH_WRITTEN so old rows are handled correctly.
_PENDING_COMPAT = "pending"

_ACTIONABLE_STATUSES = {
    PUBLISH_PENDING_WRITE,
    PUBLISH_WRITTEN,
    _PENDING_COMPAT,
    PUBLISH_COMMITTING,
    PUBLISH_COMMITTED,
    PUBLISH_FAILED,
}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """Producer-owned outbox payload - deliberately no filename or path: the publisher is
    the sole owner of mapping a message to a location in the configured outbox."""

    id: str
    content: str


@dataclass(slots=True)
class PublishRecord:
    """Publisher-owned persistent lifecycle state for an outbox message."""

    message: OutboxMessage
    publish_status: str
    publish_commit_sha: str | None = None
    publish_retry_count: int = 0
    content_available: bool = True
    # Read only for rows created before #180; new producers never supply paths.
    legacy_outbox_file: str | None = field(default=None, compare=False)

    @property
    def record_id(self) -> str:
        return self.message.id


# ---------------------------------------------------------------------------
# Lock helper
# ---------------------------------------------------------------------------


class RepoLock:
    """Advisory per-repo file lock (fcntl.LOCK_EX) for write/commit/push."""

    def __init__(self, repo_root: Path) -> None:
        lock_dir = repo_root / ".git"
        if not lock_dir.is_dir():
            # Bare repo or misconfigured; fall back to the repo root.
            lock_dir = repo_root
        self._lock_path = lock_dir / "outbox_publisher.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        fcntl.flock(self._fd, fcntl.LOCK_EX)

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "RepoLock":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


class OutboxPublisher:
    """Shared git-lifecycle publisher for outbox results: instantiate once per process; a
    per-repo file lock serialises concurrent calls so threads or a concurrent process never
    produce duplicate commits for the same file. Takes a ``MailboxConfig`` (or a bare
    ``repo_root``) plus the SQLite ``db_path`` for publish state."""

    def __init__(
        self,
        mailbox_config: Any = None,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str | Path | None = None,
        notifier: Any = None,
    ) -> None:
        if mailbox_config is not None:
            self._repo_root: Path = Path(mailbox_config.mailbox_repo_path)
            self._outbox_dir: Path = Path(mailbox_config.outbox_dir)
        elif repo_root is not None:
            self._repo_root = Path(repo_root)
            self._outbox_dir = self._repo_root / "outbox"
        else:
            raise ValueError("Either mailbox_config or repo_root must be provided")

        self._conn = init_db(db_path)
        self._conn.row_factory = sqlite3.Row
        self._db_path = db_path
        self._repo_lock = RepoLock(self._repo_root)
        self._notifier = notifier

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Public helpers: state transitions used by producers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Core publish API
    # ------------------------------------------------------------------

    def publish(self, record: PublishRecord) -> bool:
        """Publish one record through the full lifecycle; True once it reached PUBLISH_PUSHED.
        The caller's business-logic state is never changed by this method."""
        with self._repo_lock:
            self._enqueue_notification(record)
            published = self._publish_locked(record)
        if published:
            self._notify(record)
        return published

    def _enqueue_notification(self, record: PublishRecord) -> None:
        """Persist the intent to announce this message *before* pushing it: a record that
        reaches ``pushed`` is never selected by ``fetch_pending`` again, so a crash between push
        and enqueue would strand the message with no notification row - the one outcome the
        contract disallows. Enqueuing first trades that for a self-healing failure mode (a
        failed publish is still announced from the producer's durable content, while the file
        is retried by the next poll)."""
        if self._notifier is None:
            return
        try:
            self._notifier.enqueue(record.record_id, record.message.content)
        except Exception as exc:  # noqa: BLE001 - publishing must not depend on it
            logger.warning(
                "outbox PR notification enqueue failed for %s: %s",
                record.record_id,
                _redact(str(exc)),
            )

    def _notify(self, record: PublishRecord) -> None:
        """Announce a published message on the communication PR (#196). Best-effort: the row is
        already durable, so a failure only delays delivery to the next ``deliver_pending`` sweep
        and never undoes a pushed message."""
        if self._notifier is None:
            return
        try:
            self._notifier.notify(record.record_id, record.message.content)
        except Exception as exc:  # noqa: BLE001 - publication already succeeded
            logger.warning(
                "outbox PR notification failed for %s: %s",
                record.record_id,
                _redact(str(exc)),
            )

    def publish_many(self, records: list[PublishRecord]) -> int:
        """Publish a list of records; return count of successfully pushed."""
        published = 0
        for record in records:
            if self.publish(record):
                published += 1
        return published

    # ------------------------------------------------------------------
    # Internal locked implementation
    # ------------------------------------------------------------------

    def _publish_locked(self, record: PublishRecord) -> bool:
        status = record.publish_status

        if status == PUBLISH_PUSHED:
            return True

        # ---- Stage 3: commit exists → only push ------------------------------
        if record.publish_commit_sha and status == PUBLISH_COMMITTED:
            return self._push_and_record(record)

        # ---- Stage 2: file written → commit + push ---------------------------
        if status in (PUBLISH_WRITTEN, _PENDING_COMPAT, PUBLISH_COMMITTING):
            try:
                path = self._existing_path(record)
            except ValueError as exc:
                self._on_error(record, str(exc))
                logger.error("outbox publish rejected (%s): %s", record.record_id, _redact(str(exc)))
                return False
            return self._commit_and_push(record, path)

        # ---- Stage 1: pending_write → write file, then commit + push ---------
        if status == PUBLISH_PENDING_WRITE:
            return self._write_then_publish(record)

        # ---- Retryable failure: re-evaluate from current disk state ----------
        if status == PUBLISH_FAILED:
            return self._recover_failed(record)

        logger.warning("outbox publish: unknown status %r for %s", status, record.record_id)
        return False

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _write_then_publish(self, record: PublishRecord) -> bool:
        """pending_write → write file → written → commit → push."""
        try:
            path = (
                self._validate_outbox_path_for_write(record.legacy_outbox_file)
                if record.legacy_outbox_file
                else self._canonical_path(record.message)
            )
        except ValueError as exc:
            self._on_error(record, str(exc))
            logger.error("outbox publish rejected (%s): %s", record.record_id, _redact(str(exc)))
            return False

        if path.is_symlink():
            self._on_error(record, "symlinks are not allowed")
            return False

        if path.is_file():
            # File already exists (previous run wrote it but crashed before advancing the state):
            # adopt as-is.
            self._record_outbox_file(record, path)
            self._advance_status(record, PUBLISH_WRITTEN)
            try:
                self._validate_path(path, require_file=True)
            except ValueError as exc:
                self._on_error(record, str(exc))
                return False
            return self._commit_and_push(record, path)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.write(record.message.content)
                Path(tmp_name).replace(path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            self._on_error(record, f"write failed: {exc!r}")
            return False

        self._record_outbox_file(record, path)
        self._advance_status(record, PUBLISH_WRITTEN)

        try:
            self._validate_path(path, require_file=True)
        except ValueError as exc:
            self._on_error(record, str(exc))
            return False

        return self._commit_and_push(record, path)

    def _commit_and_push(self, record: PublishRecord, path: Path) -> bool:
        """written/committing → commit → committed → push → pushed."""
        self._advance_status(record, PUBLISH_COMMITTING)

        # Check for foreign dirty files before touching the index.
        expected_rel = str(path.relative_to(self._repo_root))
        if self._has_foreign_dirty_files(expected_rel):
            self._on_error(record, "foreign dirty files in mailbox repo; aborting")
            return False

        if not self._stage_exact_file(path):
            self._on_error(record, "git add failed")
            return False

        commit_sha = self._commit_exact_file(record.record_id, path)
        if commit_sha is None:
            self._on_error(record, "git commit failed")
            return False

        self._advance_committed(record, commit_sha)

        if not git_push(self._repo_root):
            self._on_error(record, "git push failed")
            return False

        self._advance_status(record, PUBLISH_PUSHED)
        return True

    def _push_and_record(self, record: PublishRecord) -> bool:
        """committed If the commit is already on the remote (push
        succeeded but the DB update crashed), detect it via ``git branch -r --contains <sha>``
        and mark pushed without a duplicate push."""
        sha = record.publish_commit_sha
        if sha and self._commit_already_on_remote(sha):
            logger.info(
                "outbox publish: commit %s already on remote for %s; marking pushed",
                sha[:12], record.record_id,
            )
            self._advance_status(record, PUBLISH_PUSHED)
            return True
        if not git_push(self._repo_root):
            self._on_error(record, "git push failed")
            return False
        self._advance_status(record, PUBLISH_PUSHED)
        return True

    def _recover_failed(self, record: PublishRecord) -> bool:
        """Best-effort recovery for a previously-failed record."""
        if record.publish_commit_sha:
            # Commit exists -> just retry the push.
            return self._push_and_record(record)

        try:
            path = self._existing_path(record)
        except ValueError:
            path = None
        if path is not None and path.is_file():
            # Synthesise a written record to resume the pipeline.
            pseudo = PublishRecord(
                message=record.message,
                publish_status=PUBLISH_WRITTEN,
                publish_commit_sha=None,
                publish_retry_count=record.publish_retry_count,
                legacy_outbox_file=record.legacy_outbox_file,
            )
            return self._commit_and_push(pseudo, path)

        if record.content_available:
            pseudo = PublishRecord(
                message=record.message,
                publish_status=PUBLISH_PENDING_WRITE,
                publish_retry_count=record.publish_retry_count,
                content_available=record.content_available,
            )
            return self._write_then_publish(pseudo)

        self._on_error(record, "failed: file missing and no stored content")
        return False

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _canonical_path(self, message: OutboxMessage) -> Path:
        if not message.id or message.id in {".", ".."} or ".." in Path(message.id).parts:
            raise ValueError("outbox message id must not contain path traversal segments")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", message.id):
            raise ValueError("outbox message id contains unsafe filename characters")
        path = self._outbox_dir / self._canonical_filename(message.id)
        return self._validate_path(path, require_file=False)

    def _canonical_filename(self, message_id: str) -> str:
        return f"outbox-{message_id}.md"

    def _existing_path(self, record: PublishRecord) -> Path:
        if record.legacy_outbox_file:
            return self._validate_outbox_path(record.legacy_outbox_file)
        return self._validate_path(self._canonical_path(record.message), require_file=True)

    def _validate_path(self, path: Path, *, require_file: bool) -> Path:
        """Validate containment and symlinks for publisher-owned paths."""
        repo_resolved = self._repo_root.resolve()
        outbox_resolved = self._outbox_dir.resolve()
        if outbox_resolved != repo_resolved and not outbox_resolved.is_relative_to(repo_resolved):
            raise ValueError("outbox directory escapes mailbox repository")
        if self._outbox_dir.is_symlink():
            raise ValueError("symlinks are not allowed")
        resolved_parent = path.parent.resolve()
        if resolved_parent != outbox_resolved and not resolved_parent.is_relative_to(outbox_resolved):
            raise ValueError("outbox path escapes outbox directory")
        if path.is_symlink():
            raise ValueError("symlinks are not allowed")
        if require_file and not path.is_file():
            raise ValueError(f"outbox file missing: {path.name}")
        return path

    def _validate_outbox_path_for_write(self, outbox_file: str) -> Path:
        """Validate path structure and containment *before* the file exists (safe to call at
        the start of ``_write_then_publish``). The parent dir is resolved to check containment
        without requiring the file itself; ``is_relative_to`` avoids false positives from
        string-prefix matching (e.g. /outbox_extra vs /outbox)."""
        rel = Path(outbox_file)
        if rel.is_absolute():
            raise ValueError("outbox path must be relative")
        if any(part == ".." for part in rel.parts):
            raise ValueError("outbox path must not contain path traversal segments")
        if not rel.name.endswith(".md"):
            raise ValueError(f"unexpected outbox filename: {rel.name!r}")
        path = self._repo_root / rel
        # Resolve parent dir (which must already exist or be creatable inside
        # outbox) to check containment without requiring the file itself.
        outbox_resolved = self._outbox_dir.resolve()
        try:
            resolved_parent = path.parent.resolve()
        except OSError:
            resolved_parent = path.parent
        # Use is_relative_to to avoid false positives from string-prefix matching
        # (e.g. /outbox_extra vs /outbox).
        if resolved_parent != outbox_resolved and not resolved_parent.is_relative_to(outbox_resolved):
            raise ValueError("outbox path escapes outbox directory")
        if path.is_symlink():
            raise ValueError("symlinks are not allowed")
        return path

    def _validate_outbox_path(self, outbox_file: str) -> Path:
        rel = Path(outbox_file)
        if rel.is_absolute():
            raise ValueError("outbox path must be relative")
        if any(part == ".." for part in rel.parts):
            raise ValueError("outbox path must not contain path traversal segments")
        if not rel.name.endswith(".md"):
            raise ValueError(f"unexpected outbox filename: {rel.name!r}")
        path = self._repo_root / rel
        resolved = path.resolve()
        outbox_resolved = self._outbox_dir.resolve()
        if not str(resolved).startswith(str(outbox_resolved) + os.sep):
            raise ValueError("outbox path escapes outbox directory")
        if path.is_symlink():
            raise ValueError("symlinks are not allowed")
        if not path.is_file():
            raise ValueError(f"outbox file missing: {rel}")
        return path

    def _has_foreign_dirty_files(self, expected_rel: str) -> bool:
        """Return True if there are foreign *staged* changes in the index. Unstaged modifications
        and untracked files are harmless - ``git add -- <path>``/``git commit -- <path>`` touch
        only the exact pathspec; abort only when another process already staged foreign changes
        that would pollute the index irreversibly."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self._repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return True
        for line in result.stdout.splitlines():
            if len(line) < 3:
                continue
            # Column 1 is the staging-area status.
            index_status = line[0]  # column 1: staging-area status
            file_path = line[3:].strip('"')
            # Skip untracked files and unstaged-only changes (index_status == ' ').
            if index_status in (" ", "?"):
                continue
            # A staged change for a file other than our expected outbox file is foreign and must
            # not be swept into the commit.
            if file_path != expected_rel:
                return True
        return False

    def _stage_exact_file(self, path: Path) -> bool:
        rel = path.relative_to(self._repo_root)
        result = subprocess.run(
            ["git", "add", "--", str(rel)],
            cwd=str(self._repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error("git add failed for %s: %s", rel, result.stderr[:300])
        return result.returncode == 0

    def _commit_exact_file(self, record_id: str, path: Path) -> str | None:
        """Commit only *path* and return the new HEAD SHA; if the file is already committed
        (crash after commit, before DB write) return the existing SHA without a duplicate."""
        rel = str(path.relative_to(self._repo_root))
        message = f"outbox: publish {record_id}"
        result = subprocess.run(
            ["git", "commit", "--no-verify", "-m", message, "--", rel],
            cwd=str(self._repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return self._current_commit_sha()

        # Self-heal: file already in HEAD (crash after commit, before DB write).
        diff = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", rel],
            cwd=str(self._repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if diff.returncode == 0:
            logger.info("outbox publish: %s already committed, recovering SHA", rel)
            return self._current_commit_sha()

        logger.error("git commit failed for %s: %s", rel, result.stderr[:300])
        return None

    def _current_commit_sha(self) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self._repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip()

    def _commit_already_on_remote(self, sha: str) -> bool:
        """Return True if *sha* is reachable from any remote-tracking branch - detects "push
        succeeded, DB update crashed" so the record is marked pushed without a duplicate push."""
        result = subprocess.run(
            ["git", "branch", "-r", "--contains", sha],
            cwd=str(self._repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    # ------------------------------------------------------------------
    # DB state helpers (generic — callers supply their own SQL or use
    # the table-specific subclasses / wrapper methods below)
    # ------------------------------------------------------------------

    def _advance_status(self, record: PublishRecord, new_status: str) -> None:
        """Advance the publish_status for a record.  Subclasses override."""

    def _advance_committed(self, record: PublishRecord, commit_sha: str) -> None:
        """Persist commit_sha and advance status to PUBLISH_COMMITTED."""
        self._advance_status(record, PUBLISH_COMMITTED)

    def _record_outbox_file(self, record: PublishRecord, path: Path) -> None:
        """Persist the canonical repo-relative path after the publisher chose it."""

    def _on_error(self, record: PublishRecord, error: str) -> None:
        """Persist a failure.  Subclasses override."""
        logger.error("outbox publish failed for %s: %s", record.record_id, _redact(error))


# ---------------------------------------------------------------------------
# Table-specific sub-publishers
# ---------------------------------------------------------------------------


class _TablePublisher(OutboxPublisher):
    """OutboxPublisher backed by a specific SQLite table; subclasses set ``_TABLE``,
    ``_ID_COLUMN`` and ``_COMMIT_SHA_COLUMN``."""

    _TABLE: str = ""
    _ID_COLUMN: str = "request_id"
    _COMMIT_SHA_COLUMN: str = "publish_commit_sha"

    def write_message(self, message: OutboxMessage) -> str:
        """Atomically create the publisher-owned file for a producer payload (only ``id`` and
        ``content`` supplied); persisting the canonical path and advancing pending_write ->
        written happen under the same repository lock as publication."""
        record = PublishRecord(message=message, publish_status=PUBLISH_PENDING_WRITE)
        with self._repo_lock:
            path = self._canonical_path(message)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                self._validate_path(path, require_file=True)
            else:
                fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                        tmp.write(message.content)
                    Path(tmp_name).replace(path)
                except BaseException:
                    Path(tmp_name).unlink(missing_ok=True)
                    raise
            self._record_outbox_file(record, path)
            self._advance_status(record, PUBLISH_WRITTEN)
            return str(path.relative_to(self._repo_root))

    def _record_outbox_file(self, record: PublishRecord, path: Path) -> None:
        rel = str(path.relative_to(self._repo_root))
        self._conn.execute(
            f"""UPDATE {self._TABLE}
                SET outbox_file = ?, updated_at = datetime('now')
                WHERE {self._ID_COLUMN} = ?""",
            (rel, record.record_id),
        )
        self._conn.commit()

    def _advance_status(self, record: PublishRecord, new_status: str) -> None:
        self._conn.execute(
            f"""UPDATE {self._TABLE}
                SET publish_status = ?,
                    updated_at = datetime('now')
                WHERE {self._ID_COLUMN} = ?""",
            (new_status, record.record_id),
        )
        self._conn.commit()

    def _advance_committed(self, record: PublishRecord, commit_sha: str) -> None:
        self._conn.execute(
            f"""UPDATE {self._TABLE}
                SET publish_status = ?,
                    {self._COMMIT_SHA_COLUMN} = ?,
                    updated_at = datetime('now')
                WHERE {self._ID_COLUMN} = ?""",
            (PUBLISH_COMMITTED, commit_sha, record.record_id),
        )
        self._conn.commit()

    def _on_error(self, record: PublishRecord, error: str) -> None:
        logger.error("outbox publish failed for %s: %s", record.record_id, _redact(error))
        self._conn.execute(
            f"""UPDATE {self._TABLE}
                SET publish_status = ?,
                    publish_last_error = ?,
                    publish_retry_count = publish_retry_count + 1,
                    updated_at = datetime('now')
                WHERE {self._ID_COLUMN} = ?""",
            (PUBLISH_FAILED, _redact(error)[:500], record.record_id),
        )
        self._conn.commit()


class OperationChainOutboxPublisher(_TablePublisher):
    """Publishes operation-chain outbox results."""

    _TABLE = "operation_chains"
    _ID_COLUMN = "request_id"
    _COMMIT_SHA_COLUMN = "publish_commit_sha"

    def _canonical_filename(self, message_id: str) -> str:
        return f"operation-chain-result-{message_id}.md"

    def fetch_pending(self) -> list[PublishRecord]:
        rows = self._conn.execute(
            """SELECT request_id, outbox_file, publish_status,
                      publish_commit_sha, publish_retry_count, outbox_content
               FROM operation_chains
               WHERE (outbox_content IS NOT NULL OR outbox_file IS NOT NULL)
                 AND publish_status IN (?, ?, ?, ?, ?, ?)
               ORDER BY created_at ASC""",
            (
                PUBLISH_PENDING_WRITE,
                PUBLISH_WRITTEN,
                _PENDING_COMPAT,
                PUBLISH_COMMITTING,
                PUBLISH_COMMITTED,
                PUBLISH_FAILED,
            ),
        ).fetchall()
        records = []
        for row in rows:
            stored_content: str | None = row["outbox_content"]
            records.append(PublishRecord(
                message=OutboxMessage(id=row["request_id"], content=stored_content or ""),
                publish_status=row["publish_status"],
                publish_commit_sha=row["publish_commit_sha"],
                publish_retry_count=row["publish_retry_count"] or 0,
                content_available=stored_content is not None,
                legacy_outbox_file=row["outbox_file"],
            ))
        return records

    def publish_pending(self) -> int:
        return self.publish_many(self.fetch_pending())


class TaskApprovalOutboxPublisher(_TablePublisher):
    """Publishes mailbox task results (file name kept from the approval era, #264)."""

    _TABLE = "mailbox_task_approvals"
    _ID_COLUMN = "id"
    _COMMIT_SHA_COLUMN = "publish_commit_sha"

    def _canonical_filename(self, message_id: str) -> str:
        return f"task-approval-result-{message_id}.md"

    def fetch_pending(self) -> list[PublishRecord]:
        rows = self._conn.execute(
            """SELECT id, outbox_file, publish_status,
                      publish_commit_sha, publish_retry_count, outbox_content
               FROM mailbox_task_approvals
               WHERE (outbox_content IS NOT NULL OR outbox_file IS NOT NULL)
                 AND publish_status IN (?, ?, ?, ?, ?, ?)
               ORDER BY created_at ASC""",
            (
                PUBLISH_PENDING_WRITE,
                PUBLISH_WRITTEN,
                _PENDING_COMPAT,
                PUBLISH_COMMITTING,
                PUBLISH_COMMITTED,
                PUBLISH_FAILED,
            ),
        ).fetchall()
        records = []
        for row in rows:
            stored_content: str | None = row["outbox_content"]
            records.append(PublishRecord(
                message=OutboxMessage(id=row["id"], content=stored_content or ""),
                publish_status=row["publish_status"],
                publish_commit_sha=row["publish_commit_sha"],
                publish_retry_count=row["publish_retry_count"] or 0,
                content_available=stored_content is not None,
                legacy_outbox_file=row["outbox_file"],
            ))
        return records

    def publish_pending(self) -> int:
        return self.publish_many(self.fetch_pending())


class FailureAnalysisOutboxPublisher(_TablePublisher):
    """Publishes failure-analysis outbox notifications (#148): same shared git lifecycle -
    the analysis service persists the content, this publisher owns path, atomic write,
    commit and push. A ``pushed`` row is never selected by ``fetch_pending`` again, so
    restarts and re-polls cannot produce a second commit for the same analysis."""

    _TABLE = "failure_analyses"
    _ID_COLUMN = "id"
    _COMMIT_SHA_COLUMN = "publish_commit_sha"

    def _canonical_filename(self, message_id: str) -> str:
        return f"failure-analysis-{message_id}.md"

    def fetch_pending(self) -> list[PublishRecord]:
        rows = self._conn.execute(
            """SELECT id, outbox_file, publish_status,
                      publish_commit_sha, publish_retry_count, outbox_content
               FROM failure_analyses
               WHERE (outbox_content IS NOT NULL OR outbox_file IS NOT NULL)
                 AND publish_status IN (?, ?, ?, ?, ?, ?)
               ORDER BY created_at ASC""",
            (
                PUBLISH_PENDING_WRITE,
                PUBLISH_WRITTEN,
                _PENDING_COMPAT,
                PUBLISH_COMMITTING,
                PUBLISH_COMMITTED,
                PUBLISH_FAILED,
            ),
        ).fetchall()
        records = []
        for row in rows:
            stored_content: str | None = row["outbox_content"]
            records.append(PublishRecord(
                message=OutboxMessage(id=row["id"], content=stored_content or ""),
                publish_status=row["publish_status"],
                publish_commit_sha=row["publish_commit_sha"],
                publish_retry_count=row["publish_retry_count"] or 0,
                content_available=stored_content is not None,
                legacy_outbox_file=row["outbox_file"],
            ))
        return records

    def publish_pending(self) -> int:
        return self.publish_many(self.fetch_pending())


def _redact(value: str) -> str:
    """Scrub secrets/paths using the shared redaction module, then bound length."""
    return _redact_full(value)[:500]
