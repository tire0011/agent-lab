"""Git-backed communication mailbox for the agent-lab repository.

Processes Markdown files with YAML front matter from communication/inbox/,
writes responses to communication/outbox/, and records everything in SQLite.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 65536  # 64 KB
MAX_RETRY = 3
SUPPORTED_TYPES = {"idea", "task", "question", "context"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"AZURE_[A-Z_]+\s*=\s*\S"),
    re.compile(r"API_KEY\s*=\s*\S"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S"),
]

ALLOWED_WRITE_PATHS = {"communication/inbox", "communication/outbox", "IDEAS.md"}

# ---------------------------------------------------------------------------
# Database migration
# ---------------------------------------------------------------------------

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
    outbox_file TEXT NULL
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


# ---------------------------------------------------------------------------
# YAML front-matter parsing (stdlib only)
# ---------------------------------------------------------------------------

def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML front matter from a Markdown string.

    Returns (metadata_dict, body_text).  Raises ValueError on parse errors.
    """
    if not text.startswith("---"):
        raise ValueError("Missing YAML front matter (must start with '---')")

    # Find the closing ---
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


# ---------------------------------------------------------------------------
# Message validation
# ---------------------------------------------------------------------------

def _is_binary(data: bytes) -> bool:
    """Heuristic: contains NUL bytes → binary."""
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
    except ValueError:
        raise ValueError(f"Path traversal detected: {path}")


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
            raise ValueError(
                f"File too large: {stat.st_size} bytes (max {MAX_FILE_SIZE})"
            )

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
            raise ValueError(
                f"Unknown message type: {msg_type!r}. Supported: {SUPPORTED_TYPES}"
            )

        return cls(file_path, msg_type, created_by, meta, body)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

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


def git_current_commit(repo_path: Path) -> str:
    result = _git(["rev-parse", "HEAD"], repo_path, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def git_pull_ff_only(repo_path: Path) -> bool:
    """Pull with --ff-only. Return True on success."""
    if not git_is_clean(repo_path):
        logger.warning("Working tree is not clean; skipping git pull")
        return False
    result = _git(["pull", "--ff-only"], repo_path, check=False)
    if result.returncode != 0:
        logger.warning("git pull --ff-only failed: %s", result.stderr.strip())
        return False
    return True


def git_add_and_commit(repo_path: Path, files: list[Path], message: str) -> bool:
    """Stage only allowed files and commit. Return True on success."""
    for f in files:
        rel = f.relative_to(repo_path)
        parts = str(rel).split("/")
        top = parts[0] if len(parts) == 1 else "/".join(parts[:2])
        if top not in ALLOWED_WRITE_PATHS and str(rel) not in ALLOWED_WRITE_PATHS:
            raise PermissionError(f"Not allowed to commit: {rel}")
        _git(["add", str(rel)], repo_path)

    result = _git(
        ["commit", "--no-verify", "-m", message],
        repo_path,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("git commit failed: %s", result.stderr.strip())
        return False
    return True


def git_push(repo_path: Path) -> bool:
    """Push to the tracked remote branch (no --force). Return True on success."""
    result = _git(["push"], repo_path, check=False)
    if result.returncode != 0:
        logger.warning("git push failed: %s", result.stderr.strip())
        return False
    return True


# ---------------------------------------------------------------------------
# Outbox helpers
# ---------------------------------------------------------------------------

def _outbox_filename(msg: MailboxMessage, ts: str) -> str:
    h = hashlib.sha256(str(msg.file_path).encode()).hexdigest()[:8]
    safe_ts = ts.replace(":", "").replace("-", "").replace("T", "").replace("Z", "")[:15]
    return f"{safe_ts}-{msg.msg_type}-{h}.md"


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via a .tmp file then rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_outbox_content(
    msg: MailboxMessage,
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


# ---------------------------------------------------------------------------
# Message processors
# ---------------------------------------------------------------------------

def _process_idea(
    msg: MailboxMessage,
    repo_path: Path,
    conn: sqlite3.Connection,
    db_msg_id: int,
) -> tuple[str, str | None]:
    """Append idea to IDEAS.md. Returns (result_body, idea_id)."""
    ideas_path = repo_path / "IDEAS.md"
    content = ideas_path.read_text(encoding="utf-8")

    idea_text = msg.body.strip().replace("\n", " ")
    idea_hash = hashlib.sha256(idea_text.encode()).hexdigest()[:8]
    idea_id = f"mailbox-{idea_hash}"

    subsection_header = "### Mailbox submissions"
    entry = f"- {idea_text} `new`\n"

    if subsection_header in content:
        # Insert after the subsection header line
        idx = content.index(subsection_header) + len(subsection_header)
        # Skip to end of that line
        nl = content.find("\n", idx)
        insert_pos = nl + 1 if nl != -1 else len(content)
        content = content[:insert_pos] + entry + content[insert_pos:]
    else:
        # Find end of ## Ideas section and append subsection before next ## or EOF
        ideas_section = "## Ideas"
        if ideas_section not in content:
            content += f"\n{ideas_section}\n\n{subsection_header}\n{entry}"
        else:
            # Find next ## section after ## Ideas
            idx = content.index(ideas_section) + len(ideas_section)
            next_section = content.find("\n## ", idx)
            if next_section == -1:
                content = content.rstrip() + f"\n\n{subsection_header}\n{entry}"
            else:
                insert = f"\n{subsection_header}\n{entry}\n"
                content = content[:next_section] + insert + content[next_section:]

    _atomic_write(ideas_path, content)

    result_body = (
        f"Your idea has been added to IDEAS.md with state `new`.\n\n"
        f"**Summary:** Added idea: {idea_text[:120]}"
    )
    return result_body, idea_id


def _process_task(
    msg: MailboxMessage,
    repo_path: Path,
    conn: sqlite3.Connection,
    db_msg_id: int,
) -> tuple[str, None]:
    """Record task request. Returns (result_body, None)."""
    result_body = (
        "Your task request has been recorded and is queued for human review.\n\n"
        "Tasks submitted via the mailbox require human approval before execution. "
        "A Nexus operator will evaluate the request and decide whether to run it."
    )
    return result_body, None


def _process_question(
    msg: MailboxMessage,
    repo_path: Path,
    conn: sqlite3.Connection,
    db_msg_id: int,
) -> tuple[str, None]:
    """Acknowledge question. Returns (result_body, None)."""
    result_body = (
        "Your question has been received and will be reviewed by a Nexus operator.\n\n"
        "Questions submitted via the mailbox are logged and reviewed periodically. "
        "If a response is warranted, it will be placed in the outbox."
    )
    return result_body, None


def _process_context(
    msg: MailboxMessage,
    repo_path: Path,
    conn: sqlite3.Connection,
    db_msg_id: int,
) -> tuple[str, None]:
    """Store context in knowledge table. Returns (result_body, None)."""
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO knowledge (timestamp, run_id, category, key, value, confidence, source)
        VALUES (?, NULL, 'mailbox_context', ?, ?, NULL, 'mailbox')
        """,
        (now, msg.file_path.name, msg.body.strip()),
    )
    conn.commit()

    result_body = (
        "Your context has been acknowledged and stored in the knowledge base "
        "under category `mailbox_context`."
    )
    return result_body, None


_PROCESSORS = {
    "idea": _process_idea,
    "task": _process_task,
    "question": _process_question,
    "context": _process_context,
}


# ---------------------------------------------------------------------------
# Core mailbox processor
# ---------------------------------------------------------------------------

class MailboxProcessor:
    """Processes inbox messages and produces outbox responses."""

    def __init__(
        self,
        repo_path: Path,
        db_path: str,
        dry_run: bool = False,
    ) -> None:
        self.repo_path = repo_path
        self.inbox = repo_path / "communication" / "inbox"
        self.outbox = repo_path / "communication" / "outbox"
        self.db_path = db_path
        self.dry_run = dry_run
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            from modules.run_store import init_db  # avoid circular import at module load
            self._conn = init_db(self.db_path)
            init_mailbox_db(self._conn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retry_unpushed_commits(self) -> None:
        """Attempt to push if there are local commits not yet on remote."""
        result = _git(["log", "@{u}..", "--oneline"], self.repo_path, check=False)
        if result.returncode == 0 and result.stdout.strip():
            logger.info("Retrying push of unpushed commits")
            git_push(self.repo_path)

    def pull(self) -> bool:
        return git_pull_ff_only(self.repo_path)

    def process_inbox(self) -> list[dict[str, Any]]:
        """Process all pending inbox messages. Returns list of result dicts."""
        files = self._get_inbox_files()
        results = []
        for f in files:
            results.append(self._process_file(f))
        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_inbox_files(self) -> list[Path]:
        """Return .md files sorted by (mtime, name), skipping .gitkeep."""
        files = [
            f for f in self.inbox.iterdir()
            if f.is_file() and f.suffix == ".md" and f.name != ".gitkeep"
        ]
        files.sort(key=lambda f: (f.stat().st_mtime, f.name))
        return files

    def _is_already_processed(self, file_path: Path) -> bool:
        row = self.conn.execute(
            "SELECT status FROM mailbox_messages WHERE file_path = ? AND status IN ('done', 'processing')",
            (str(file_path),),
        ).fetchone()
        return row is not None

    def _get_existing_record(self, file_path: Path) -> dict | None:
        row = self.conn.execute(
            "SELECT id, status, retry_count FROM mailbox_messages WHERE file_path = ?",
            (str(file_path),),
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "status": row[1], "retry_count": row[2]}

    def _should_retry(self, record: dict) -> bool:
        if record["status"] == "done":
            return False
        if record["retry_count"] >= MAX_RETRY:
            return False
        return True

    def _process_file(self, file_path: Path) -> dict[str, Any]:
        rel = str(file_path.relative_to(self.repo_path))

        existing = self._get_existing_record(file_path)
        if existing:
            if existing["status"] == "done":
                return {"file": rel, "status": "skipped", "reason": "already done"}
            if not self._should_retry(existing):
                return {
                    "file": rel,
                    "status": "skipped",
                    "reason": f"max retries ({MAX_RETRY}) reached",
                }

        # Validate and parse
        try:
            msg = MailboxMessage.load(file_path, self.inbox)
        except (ValueError, UnicodeDecodeError) as exc:
            self._record_failure(file_path, str(exc), existing)
            return {"file": rel, "status": "failed", "error": str(exc)}

        commit_sha = git_current_commit(self.repo_path)
        now = _now_iso()

        # Upsert DB record as processing
        if existing:
            self.conn.execute(
                "UPDATE mailbox_messages SET status='processing', retry_count=retry_count+1 WHERE id=?",
                (existing["id"],),
            )
            db_msg_id = existing["id"]
        else:
            cur = self.conn.execute(
                """
                INSERT INTO mailbox_messages
                    (file_path, source_commit_sha, msg_type, created_by, status, received_at)
                VALUES (?, ?, ?, ?, 'processing', ?)
                """,
                (str(file_path), commit_sha, msg.msg_type, msg.created_by, now),
            )
            db_msg_id = cur.lastrowid
        self.conn.commit()

        # Process
        try:
            processor = _PROCESSORS[msg.msg_type]
            result_body, idea_id = processor(msg, self.repo_path, self.conn, db_msg_id)
        except Exception as exc:
            logger.exception("Error processing %s", file_path)
            self._record_failure(file_path, str(exc), {"id": db_msg_id})
            return {"file": rel, "status": "failed", "error": str(exc)}

        # Write outbox
        processed_at = _now_iso()
        outbox_name = _outbox_filename(msg, processed_at)
        outbox_path = self.outbox / outbox_name
        outbox_content = _build_outbox_content(
            msg, "done", processed_at, result_body, idea_id
        )

        if not self.dry_run:
            _atomic_write(outbox_path, outbox_content)

        # Mark done in DB
        self.conn.execute(
            """
            UPDATE mailbox_messages
            SET status='done', processed_at=?, idea_id=?, outbox_file=?, last_error=NULL
            WHERE id=?
            """,
            (processed_at, idea_id, outbox_name, db_msg_id),
        )
        self.conn.commit()

        # Git commit + push
        if not self.dry_run:
            files_to_commit: list[Path] = [outbox_path]
            if msg.msg_type == "idea":
                files_to_commit.append(self.repo_path / "IDEAS.md")
            commit_msg = f"mailbox: process {msg.msg_type} from {msg.created_by} [{file_path.name}]"
            if git_add_and_commit(self.repo_path, files_to_commit, commit_msg):
                git_push(self.repo_path)

        return {
            "file": rel,
            "status": "done",
            "type": msg.msg_type,
            "outbox": outbox_name,
            "idea_id": idea_id,
        }

    def _record_failure(
        self, file_path: Path, error: str, existing: dict | None
    ) -> None:
        now = _now_iso()
        if existing and "id" in existing:
            self.conn.execute(
                "UPDATE mailbox_messages SET status='failed', last_error=?, processed_at=? WHERE id=?",
                (error, now, existing["id"]),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO mailbox_messages
                    (file_path, source_commit_sha, msg_type, created_by, status, received_at, last_error)
                VALUES (?, 'unknown', 'unknown', 'unknown', 'failed', ?, ?)
                """,
                (str(file_path), now, error),
            )
        self.conn.commit()


# ---------------------------------------------------------------------------
# Lock context manager
# ---------------------------------------------------------------------------

class MailboxLock:
    """Single-process lock via lockfile and fcntl.flock."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fd: int | None = None

    def acquire(self, timeout: float = 0.0) -> bool:
        """Try to acquire the lock. Returns False immediately if already held."""
        self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_WRONLY)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(self._fd, str(os.getpid()).encode())
            return True
        except BlockingIOError:
            os.close(self._fd)
            self._fd = None
            return False

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "MailboxLock":
        if not self.acquire():
            raise RuntimeError("Another mailbox processor instance is running")
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()
