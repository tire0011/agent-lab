"""Persistent publisher for completed operation-chain results."""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from modules.mailbox import git_push
from modules.run_store import DEFAULT_DB_PATH, init_db

_COMMIT_PREFIX = "operation-chain: publish "

logger = logging.getLogger(__name__)

PUBLISH_PENDING = "pending"
PUBLISH_COMMITTING = "committing"
PUBLISH_COMMITTED = "committed"
PUBLISH_PUSHED = "pushed"
PUBLISH_FAILED = "failed"


@dataclass(slots=True)
class PublishCandidate:
    request_id: str
    outbox_file: str
    publish_status: str
    publish_commit_sha: str | None
    publish_retry_count: int


class OperationChainPublisher:
    def __init__(self, db_path: str = DEFAULT_DB_PATH, repo_root: str = "/agent") -> None:
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.outbox_dir = self.repo_root / "communication" / "outbox"
        self._conn = init_db(db_path)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def publish_pending(self) -> int:
        rows = self._conn.execute(
            """
            SELECT request_id, outbox_file, publish_status, publish_commit_sha, publish_retry_count
            FROM operation_chains
            WHERE outbox_file IS NOT NULL
              AND publish_status IN (?, ?, ?, ?)
            ORDER BY created_at ASC
            """,
            (PUBLISH_PENDING, PUBLISH_COMMITTING, PUBLISH_COMMITTED, PUBLISH_FAILED),
        ).fetchall()
        published = 0
        for row in rows:
            if self._publish_one(PublishCandidate(*row)):
                published += 1
        return published

    def _publish_one(self, candidate: PublishCandidate) -> bool:
        try:
            path = self._validate_outbox_path(candidate.outbox_file)
        except ValueError as exc:
            self._mark_failed(candidate.request_id, str(exc))
            logger.error("operation-chain publish rejected: %s", self._redact(str(exc)))
            return False

        if candidate.publish_status == PUBLISH_PUSHED:
            return True

        if candidate.publish_commit_sha:
            # Already committed in a previous run: never touch git add/commit
            # again, only retry the push of the exact same commit.
            if not self._push_existing_commit(candidate.request_id, candidate.publish_commit_sha):
                self._mark_failed(candidate.request_id, "push failed")
                return False
            self._mark_pushed(candidate.request_id)
            return True

        self._mark_committing(candidate.request_id)
        if not self._stage_exact_file(path):
            self._mark_failed(candidate.request_id, "stage failed")
            return False

        commit_sha = self._commit_exact_file(candidate.request_id, path)
        if commit_sha is None:
            self._mark_failed(candidate.request_id, "commit failed")
            return False

        self._mark_committed(candidate.request_id, commit_sha)
        if not git_push(self.repo_root):
            self._mark_failed(candidate.request_id, "push failed")
            return False
        self._mark_pushed(candidate.request_id)
        return True

    def _validate_outbox_path(self, outbox_file: str) -> Path:
        rel = Path(outbox_file)
        if rel.is_absolute():
            raise ValueError("outbox path must be relative")
        parts = rel.parts
        if len(parts) < 3 or parts[0] != "communication" or parts[1] != "outbox":
            raise ValueError("outbox path must stay within communication/outbox")
        if any(part == ".." for part in parts):
            raise ValueError("outbox path must not contain path traversal segments")
        if rel.name != Path(outbox_file).name or not rel.name.endswith(".md"):
            raise ValueError("unexpected outbox filename")
        path = self.repo_root / rel
        resolved = path.resolve()
        if not str(resolved).startswith(str(self.outbox_dir.resolve()) + os.sep):
            raise ValueError("outbox path escapes communication/outbox")
        if path.is_symlink():
            raise ValueError("symlinks are not allowed")
        if not path.is_file():
            raise ValueError("outbox file missing")
        return path

    def _stage_exact_file(self, path: Path) -> bool:
        rel = path.relative_to(self.repo_root)
        result = subprocess.run(
            ["git", "add", "--", str(rel)],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error("git add failed for %s: %s", rel, result.stderr[:300])
        return result.returncode == 0

    def _commit_exact_file(self, request_id: str, path: Path) -> str | None:
        """Commit only *path* (never ``git add -A``) and return the new commit SHA.

        Scopes the commit itself to the exact pathspec so that any unrelated
        files that happen to already be staged in the index (by another
        process) are never swept into this commit.
        """
        rel = str(path.relative_to(self.repo_root))
        message = f"{_COMMIT_PREFIX}{request_id}"
        result = subprocess.run(
            ["git", "commit", "--no-verify", "-m", message, "--", rel],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return self._current_commit_sha()

        # Self-heal: if a previous run committed this exact file but crashed
        # before persisting the commit SHA to the DB, the working tree will
        # already match HEAD for this path and "git commit" reports nothing
        # to commit. Treat that as success instead of creating a duplicate
        # commit or re-running anything.
        diff = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", rel],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if diff.returncode == 0:
            logger.info("operation-chain publish: %s already committed, recovering SHA", rel)
            return self._current_commit_sha()

        logger.error("git commit failed for %s: %s", rel, result.stderr[:300])
        return None

    def _push_existing_commit(self, request_id: str, commit_sha: str) -> bool:
        return git_push(self.repo_root)

    def _current_commit_sha(self) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip()

    def _mark_committing(self, request_id: str) -> None:
        self._conn.execute(
            "UPDATE operation_chains SET publish_status=?, publish_retry_count=publish_retry_count+1, updated_at=datetime('now') WHERE request_id=?",
            (PUBLISH_COMMITTING, request_id),
        )
        self._conn.commit()

    def _mark_committed(self, request_id: str, commit_sha: str) -> None:
        self._conn.execute(
            "UPDATE operation_chains SET publish_status=?, publish_commit_sha=?, updated_at=datetime('now') WHERE request_id=?",
            (PUBLISH_COMMITTED, commit_sha, request_id),
        )
        self._conn.commit()

    def _mark_pushed(self, request_id: str) -> None:
        self._conn.execute(
            "UPDATE operation_chains SET publish_status=?, publish_pushed_at=datetime('now'), updated_at=datetime('now') WHERE request_id=?",
            (PUBLISH_PUSHED, request_id),
        )
        self._conn.commit()

    def _mark_failed(self, request_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE operation_chains SET publish_status=?, publish_last_error=?, updated_at=datetime('now') WHERE request_id=?",
            (PUBLISH_FAILED, error[:500], request_id),
        )
        self._conn.commit()

    def _redact(self, value: str) -> str:
        return value[:200]
