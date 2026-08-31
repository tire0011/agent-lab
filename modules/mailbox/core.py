"""Core mailbox parsing, validation, persistence, and git helpers.

This module holds the low-level mailbox primitives that were previously embedded
in modules/mailbox.py. The higher-level inbox/outbox processing remains in
mailbox_processing.py to keep the responsibilities separated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:  # pragma: no cover - Windows fallback
    import msvcrt
except ImportError:  # pragma: no cover - Windows fallback
    msvcrt = None

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 65536  # 64 KB
MAX_RETRY = 3
STATUS_DONE = "done"
STATUS_DONE_PENDING_COMMIT = "done_pending_commit"
STATUS_DEFERRED = "deferred"
_DEFERRED_PROCESSING = object()
SUPPORTED_TYPES = {"idea", "task", "question", "context", "operation_chain"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"AZURE_[A-Z_]+\s*=\s*\S"),
    re.compile(r"API_KEY\s*=\s*\S"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S"),
]

MAILBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    source_commit_sha TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    idea_id TEXT NULL,
    outbox_file TEXT NULL,
    chain_request_id TEXT NULL,
    content_hash TEXT NULL,
    task_body TEXT NULL
);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    run_id INTEGER NULL,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NULL,
    source TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mailbox_file_path ON mailbox_messages(file_path);
CREATE INDEX IF NOT EXISTS idx_mailbox_status ON mailbox_messages(status);
"""


def init_mailbox_db(conn: sqlite3.Connection) -> None:
    """Apply mailbox schema migrations to an existing connection."""
    conn.executescript(MAILBOX_SCHEMA)
    conn.commit()


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML front matter from a Markdown string.

    Returns (metadata_dict, body_text).  Raises ValueError on parse errors.
    """
    if not text.startswith("---"):
        raise ValueError("Missing YAML front matter (must start with '---')")

    rest = text[3:]
    end = rest.find("\n---")
    if end == -1:
        raise ValueError("Unclosed YAML front matter (no closing '---')")

    front = rest[:end].strip()
    body = rest[end + 4:].strip()

    meta: dict[str, str] = {}
    for line in front.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"Invalid front-matter line (no colon): {line!r}")
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()

    return meta, body


def _is_binary(data: bytes) -> bool:
    """Heuristic: contains NUL bytes -> binary."""
    return b"\x00" in data


def _has_secret(text: str) -> bool:
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            return True
    return False


def _check_path_traversal(path: Path, inbox_root: Path) -> None:
    """Raise ValueError if path escapes inbox_root."""
    try:
        path.resolve().relative_to(inbox_root.resolve())
    except ValueError as exc:
        raise ValueError(f"Path traversal detected: {path}") from exc


class MailboxMessage:
    """Parsed and validated inbox message."""

    def __init__(
        self,
        file_path: Path,
        msg_type: str,
        created_by: str,
        meta: dict[str, str],
        body: str,
    ) -> None:
        self.file_path = file_path
        self.msg_type = msg_type
        self.created_by = created_by
        self.meta = meta
        self.body = body

    @classmethod
    def load(cls, file_path: Path, inbox_root: Path) -> "MailboxMessage":
        """Load and validate a message from *file_path*."""
        _check_path_traversal(file_path, inbox_root)

        if file_path.is_symlink():
            raise ValueError(f"Symlinks are not allowed: {file_path}")

        stat = file_path.stat()
        if stat.st_size > MAX_FILE_SIZE:
            raise ValueError(f"File too large: {stat.st_size} bytes (max {MAX_FILE_SIZE})")

        raw = file_path.read_bytes()
        if _is_binary(raw):
            raise ValueError("Binary content is not allowed")

        text = raw.decode("utf-8", errors="replace")

        if _has_secret(text):
            raise ValueError("Potential secret detected in message content")

        meta, body = parse_front_matter(text)

        msg_type = meta.get("type", "").strip()
        created_by = meta.get("created_by", "").strip()

        if not msg_type:
            raise ValueError("Missing required front-matter field: 'type'")
        if not created_by:
            raise ValueError("Missing required front-matter field: 'created_by'")
        if msg_type not in SUPPORTED_TYPES:
            raise ValueError(f"Unknown message type: {msg_type!r}. Supported: {SUPPORTED_TYPES}")

        return cls(file_path, msg_type, created_by, meta, body)


MAILBOX_MESSAGE_COLUMNS = (
    "id",
    "file_path",
    "msg_type",
    "created_by",
    "status",
    "received_at",
    "processed_at",
    "outbox_file",
)


def list_recent_db_messages(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent mailbox_messages rows, newest first."""
    init_mailbox_db(conn)
    rows = conn.execute(
        f"""
        SELECT {', '.join(MAILBOX_MESSAGE_COLUMNS)}
        FROM mailbox_messages
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(zip(MAILBOX_MESSAGE_COLUMNS, row)) for row in rows]


def read_inbox_preview(file_path: str | None, *, max_chars: int = 4000) -> str | None:
    """Best-effort raw body text of a still-pending inbox file."""
    if not file_path:
        return None
    try:
        path = Path(file_path)
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_FILE_SIZE:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        _, body = parse_front_matter(text)
        return body[:max_chars]
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def list_outbox_files(outbox_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent outbox files as raw {filename, meta, body} dicts."""
    if not outbox_dir.is_dir():
        return []
    try:
        candidates = sorted(
            (
                p for p in outbox_dir.iterdir()
                if p.suffix == ".md" and not p.is_symlink() and p.is_file()
            ),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        return []

    results: list[dict[str, Any]] = []
    for path in candidates[:limit]:
        try:
            if path.stat().st_size > MAX_FILE_SIZE:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            meta, body = parse_front_matter(text)
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        results.append({"filename": path.name, "meta": meta, "body": body})
    return results


def _git(args: list[str], repo_path: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git subcommand in *repo_path*. Never uses shell=True."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=check,
    )


def git_is_clean(repo_path: Path) -> bool:
    """Return True if the working tree has no uncommitted changes."""
    result = _git(["status", "--porcelain"], repo_path, check=False)
    return result.returncode == 0 and result.stdout.strip() == ""


def git_dirty_entries(repo_path: Path) -> list[str]:
    """Return porcelain status entries for dirty worktree paths."""
    result = _git(["status", "--porcelain"], repo_path, check=False)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def git_current_commit(repo_path: Path) -> str:
    result = _git(["rev-parse", "HEAD"], repo_path, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def git_pull_ff_only(repo_path: Path) -> bool:
    """Pull with --ff-only. Return True on success."""
    result = _git(["pull", "--ff-only"], repo_path, check=False)
    if result.returncode != 0:
        logger.warning("git pull --ff-only failed: %s", result.stderr.strip())
        return False
    return True


def git_add_and_commit(
    repo_path: Path,
    files: list[Path],
    message: str,
    extra_allowed_prefixes: frozenset[str] | None = None,
) -> bool:
    """Stage the requested repository files and commit. Return True on success."""
    del extra_allowed_prefixes
    for f in files:
        rel = f.relative_to(repo_path)
        if f.exists():
            _git(["add", "--", str(rel)], repo_path)
        else:
            _git(["rm", "--ignore-unmatch", "--", str(rel)], repo_path)

    result = _git(["commit", "--no-verify", "-m", message], repo_path, check=False)
    if result.returncode != 0:
        logger.warning("git commit failed: %s", result.stderr.strip())
        return False
    return True


def git_push(repo_path: Path) -> bool:
    """Push to the tracked remote branch (no --force). Return True on success."""
    result = _git(["push"], repo_path, check=False)
    if result.returncode != 0:
        logger.warning("git push failed (exit %d)", result.returncode)
        logger.debug("git push stderr (redacted): %s", redact(result.stderr.strip())[:500])
        return False
    return True


def redact(value: str) -> str:
    """Best-effort masking for common secrets, remote URLs and file paths."""
    remote_url_patterns = [
        r"(?:https?|ssh)://\S+",
        r"git@\S+",
    ]
    masked = value
    for expr in remote_url_patterns:
        masked = re.sub(expr, "[REMOTE]", masked)
    blocked = [
        r"sk-[A-Za-z0-9]+",
        r"(AZURE_[A-Z_]+\s*=\s*)\S+",
        r"(API_KEY\s*=\s*)\S+",
        r"/agent/[^\s]+",
    ]
    for expr in blocked:
        masked = re.sub(expr, "[redacted]", masked)
    return masked


def _outbox_filename(msg: "MailboxMessage", ts: str) -> str:
    h = hashlib.sha256(str(msg.file_path).encode()).hexdigest()[:8]
    safe_ts = ts.replace(":", "").replace("-", "").replace("T", "").replace("Z", "")[:15]
    return f"{safe_ts}-{msg.msg_type}-{h}.md"


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via a unique temp file and replace."""
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_outbox_content(
    msg: "MailboxMessage",
    status: str,
    processed_at: str,
    result_body: str,
    idea_id: str | None = None,
) -> str:
    source = msg.file_path.name
    lines = [
        "---",
        f"source: communication/inbox/{source}",
        f"type: {msg.msg_type}",
        f"status: {status}",
        f"processed_at: {processed_at}",
        f"created_by: {msg.created_by}",
    ]
    if idea_id:
        lines.append(f"idea_id: {idea_id}")
    lines += ["---", "", "## Result", "", result_body]
    return "\n".join(lines) + "\n"


class MailboxLock:
    """Single-process lock via lockfile and fcntl.flock with Windows fallback."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fd: int | None = None

    def acquire(self, timeout: float = 0.0) -> bool:
        """Try to acquire the lock. Returns False immediately if already held."""
        self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_WRONLY)
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            os.write(self._fd, str(os.getpid()).encode())
            return True
        except BlockingIOError:
            os.close(self._fd)
            self._fd = None
            return False
        except OSError:
            os.close(self._fd)
            self._fd = None
            return False

    def release(self) -> None:
        if self._fd is not None:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                try:
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "MailboxLock":
        if not self.acquire():
            raise RuntimeError("Another mailbox processor instance is running")
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()
