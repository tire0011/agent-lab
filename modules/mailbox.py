"""Git-backed communication mailbox for the agent-lab repository.

Processes Markdown files with YAML front matter from communication/inbox/,
writes responses to communication/outbox/, and records everything in SQLite.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
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
STATUS_DONE = "done"
STATUS_DONE_PENDING_COMMIT = "done_pending_commit"
SUPPORTED_TYPES = {"idea", "task", "question", "context", "operation_chain"}
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
# Read-only listing helpers for the web UI (Inbox/Outbox tab).
#
# These return raw, unsanitized data (front matter + body text) meant only
# for internal consumption. Callers (api.py) are responsible for applying
# an explicit field allowlist and redaction before exposing anything in an
# HTTP response.
# ---------------------------------------------------------------------------

MAILBOX_MESSAGE_COLUMNS = (
    "id", "file_path", "msg_type", "created_by", "status",
    "received_at", "processed_at", "outbox_file",
)


def list_recent_db_messages(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent ``mailbox_messages`` rows, newest first.

    Read-only: creates the mailbox schema if missing (idempotent) but never
    writes message rows. Values are raw database contents.
    """
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
    """Best-effort raw body text of a still-pending inbox file.

    Returns ``None`` (not an error) once the file no longer exists, e.g.
    because it was already processed, committed, and deleted -- this is the
    normal end state for a handled message, not a failure.
    """
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
    """Return recent outbox files as raw ``{filename, meta, body}`` dicts.

    Newest first (outbox filenames are timestamp-prefixed, so lexicographic
    order is chronological). Read-only; never writes or deletes files.
    """
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


def git_add_and_commit(repo_path: Path, files: list[Path], message: str) -> bool:
    """Stage only allowed files and commit. Return True on success."""
    for f in files:
        rel = f.relative_to(repo_path)
        parts = str(rel).split("/")
        top = parts[0] if len(parts) == 1 else "/".join(parts[:2])
        if top not in ALLOWED_WRITE_PATHS and str(rel) not in ALLOWED_WRITE_PATHS:
            raise PermissionError(f"Not allowed to commit: {rel}")
        if f.exists():
            _git(["add", "--", str(rel)], repo_path)
        else:
            _git(["rm", "--", str(rel)], repo_path)

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
    """Record task request, store body, create pending approval. Returns (result_body, None)."""
    # Store the task body in mailbox_messages (column added by migration)
    try:
        conn.execute(
            "UPDATE mailbox_messages SET task_body = ? WHERE id = ?",
            (msg.body, db_msg_id),
        )
        conn.commit()
    except Exception:
        logger.warning("_process_task: failed to store task_body for msg %s", db_msg_id)

    # Create a pending_approval record directly on the shared connection so that
    # the correct database is always used (temp DB in tests, production DB in
    # production) without hardcoding DEFAULT_DB_PATH here.
    try:
        existing = conn.execute(
            "SELECT id FROM mailbox_task_approvals WHERE mailbox_msg_id = ?",
            (db_msg_id,),
        ).fetchone()
        if not existing:
            import uuid as _uuid
            now = _now_iso()
            approval_id = _uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO mailbox_task_approvals
                    (id, mailbox_msg_id, task_body, sender, file_path,
                     received_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?)
                """,
                (approval_id, db_msg_id, msg.body, msg.created_by,
                 str(msg.file_path), now, now, now),
            )
            conn.commit()
    except Exception:
        logger.warning("_process_task: failed to create approval for msg %s", db_msg_id, exc_info=True)

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


def _parse_operation_chain_body(body: str) -> tuple[dict, str | None]:
    """Parse and validate JSON body of an operation_chain message.

    Returns (parsed_dict, error_reason).  error_reason is None on success.
    """
    stripped = body.strip()
    if not stripped:
        return {}, "Empty body: JSON is required"
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return {}, f"Invalid JSON: {exc}"
    if not isinstance(data, dict):
        return {}, "Body must be a JSON object"
    if "steps" not in data:
        return {}, "Missing required field: steps"
    if not isinstance(data["steps"], list):
        return {}, "Field 'steps' must be a list"
    if len(data["steps"]) == 0:
        return {}, "Field 'steps' must not be empty"
    if len(data["steps"]) > 10:
        return {}, "Field 'steps' must have at most 10 entries"
    for i, step in enumerate(data["steps"]):
        if not isinstance(step, dict):
            return {}, f"Step {i} must be a JSON object"
        if "id" not in step:
            return {}, f"Step {i} missing required field: id"
        if "action" not in step:
            return {}, f"Step {i} missing required field: action"
        step_id = step["id"]
        if not isinstance(step_id, str) or not step_id.strip():
            return {}, f"Step {i} field 'id' must be a non-empty string"
        step_action = step["action"]
        if not isinstance(step_action, str) or not step_action.strip():
            return {}, f"Step {i} field 'action' must be a non-empty string"
    return data, None




# Sentinel returned by _process_operation_chain to signal:
# "accepted – no mailbox outbox file; service owns the result outbox file"
_CHAIN_ACCEPTED = object()

# Sentinel for validation/idempotency errors that need a problem outbox file
class _ChainError:
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _process_operation_chain(
    msg: MailboxMessage,
    repo_path: Path,
    conn: sqlite3.Connection,
    db_msg_id: int,
    operation_chain_service: Any = None,
) -> Any:
    """Parse, validate, and start an operation_chain message.

    Returns:
      _CHAIN_ACCEPTED  – chain queued; the service writes the sole outbox file.
      _ChainError(...)  – validation/idempotency failed; caller writes a
                          problem outbox file so the sender gets feedback.
    """
    import hashlib as _hashlib

    # ── Parse body ──────────────────────────────────────────────────────────
    data, parse_error = _parse_operation_chain_body(msg.body)
    if parse_error:
        logger.warning("operation_chain parse error for %s: %s", msg.file_path.name, parse_error)
        return _ChainError(f"Operation chain rejected: {parse_error}")

    # ── Extract / derive request_id ─────────────────────────────────────────
    request_id: str | None = msg.meta.get("request_id", "").strip() or None
    if request_id and not request_id.replace("-", "").replace("_", "").isalnum():
        return _ChainError("Operation chain rejected: request_id must be alphanumeric with dashes/underscores")

    content_hash = _hashlib.sha256(msg.body.strip().encode()).hexdigest()[:16]
    if request_id is None:
        request_id = f"inbox-{content_hash}"

    # ── Idempotency check ────────────────────────────────────────────────────
    existing_row = conn.execute(
        "SELECT id, chain_request_id, content_hash FROM mailbox_messages WHERE chain_request_id = ?",
        (request_id,),
    ).fetchone()
    if existing_row:
        existing_hash = existing_row[2] if len(existing_row) > 2 else None
        if existing_hash == content_hash:
            logger.info(
                "operation_chain idempotent: request_id=%s already accepted", request_id
            )
            return _CHAIN_ACCEPTED  # already queued with identical content; no-op
        else:
            return _ChainError(
                f"Conflict: request_id `{request_id}` already exists with different content."
            )

    # ── Record idempotency data before starting the chain ───────────────────
    try:
        conn.execute(
            "UPDATE mailbox_messages SET chain_request_id = ?, content_hash = ? WHERE id = ?",
            (request_id, content_hash, db_msg_id),
        )
        conn.commit()
    except Exception:
        logger.warning(
            "_process_operation_chain: failed to persist idempotency data for msg %s", db_msg_id
        )

    if operation_chain_service is None:
        return _ChainError("Operation chain service not available; chain not started.")

    # ── Build and start the chain ────────────────────────────────────────────
    try:
        from models import OperationChainRequest, OperationChainStep
        stop_on_error = bool(data.get("stop_on_error", False))
        steps = [
            OperationChainStep(
                id=s["id"],
                action=s["action"],
                arguments=s.get("arguments", {}),
            )
            for s in data["steps"]
        ]
        chain_request = OperationChainRequest(
            steps=steps,
            stop_on_error=stop_on_error,
            request_id=request_id,
        )
    except Exception as exc:
        return _ChainError(f"Operation chain rejected: invalid step definition – {exc}")

    try:
        result = operation_chain_service.start_chain(chain_request)
    except Exception as exc:
        logger.exception("_process_operation_chain: start_chain failed")
        return _ChainError(f"Operation chain failed to start: {exc}")

    if result.overall_status == "rejected":
        return _ChainError(f"Operation chain rejected: {result.rejection_reason}")

    logger.info(
        "operation_chain queued: request_id=%s status=%s steps=%d",
        request_id, result.overall_status, len(steps),
    )
    return _CHAIN_ACCEPTED


_PROCESSORS = {
    "idea": _process_idea,
    "task": _process_task,
    "question": _process_question,
    "context": _process_context,
    # operation_chain is handled separately (needs service injection)
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
        operation_chain_service: Any = None,
    ) -> None:
        self.repo_path = repo_path
        self.inbox = repo_path / "communication" / "inbox"
        self.outbox = repo_path / "communication" / "outbox"
        self.db_path = db_path
        self.dry_run = dry_run
        self.operation_chain_service = operation_chain_service
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

    def ensure_clean_worktree(self) -> bool:
        return git_is_clean(self.repo_path)

    def retry_pending_git_commits(self) -> None:
        rows = self.conn.execute(
            """
            SELECT id, file_path, msg_type, created_by, outbox_file, status
            FROM mailbox_messages
            WHERE status IN (?, ?)
            ORDER BY id ASC
            """,
            (STATUS_DONE_PENDING_COMMIT, STATUS_DONE),
        ).fetchall()
        for row in rows:
            msg = {
                "id": row[0],
                "file_path": Path(row[1]),
                "msg_type": row[2],
                "created_by": row[3],
                "outbox_file": row[4],
                "status": row[5],
            }
            if msg["status"] == STATUS_DONE and not msg["file_path"].exists():
                continue
            if msg["msg_type"] == "operation_chain":
                self._finalize_git_commit_inbox_only(msg)
            else:
                self._finalize_git_commit(msg)

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
        if not self.inbox.exists():
            return []
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
            """
            SELECT id, status, retry_count, msg_type, created_by, outbox_file
            FROM mailbox_messages
            WHERE file_path = ?
            """,
            (str(file_path),),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "status": row[1],
            "retry_count": row[2],
            "msg_type": row[3],
            "created_by": row[4],
            "outbox_file": row[5],
        }

    def _should_retry(self, record: dict) -> bool:
        if record["status"] in (STATUS_DONE, STATUS_DONE_PENDING_COMMIT):
            return False
        if record["retry_count"] >= MAX_RETRY:
            return False
        return True

    def _process_file(self, file_path: Path) -> dict[str, Any]:
        rel = str(file_path.relative_to(self.repo_path))

        existing = self._get_existing_record(file_path)
        if existing:
            if existing["status"] in (STATUS_DONE, STATUS_DONE_PENDING_COMMIT):
                if self.dry_run:
                    return {"file": rel, "status": "skipped", "reason": "already done"}
                if self._finalize_git_commit(
                    {
                        "id": existing["id"],
                        "file_path": file_path,
                        "msg_type": existing["msg_type"],
                        "created_by": existing["created_by"],
                        "outbox_file": existing["outbox_file"],
                        "status": existing["status"],
                    }
                ):
                    return {"file": rel, "status": "done", "reason": "cleaned already-done inbox file"}
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
            if msg.msg_type == "operation_chain":
                chain_outcome = _process_operation_chain(
                    msg, self.repo_path, self.conn, db_msg_id,
                    operation_chain_service=self.operation_chain_service,
                )
                return self._handle_chain_outcome(
                    chain_outcome, file_path, rel, msg, db_msg_id
                )
            else:
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

        # Mark processing result durable before inbox deletion/commit.
        self.conn.execute(
            """
            UPDATE mailbox_messages
            SET status=?, processed_at=?, idea_id=?, outbox_file=?, last_error=NULL
            WHERE id=?
            """,
            (STATUS_DONE_PENDING_COMMIT, processed_at, idea_id, outbox_name, db_msg_id),
        )
        self.conn.commit()

        committed = False
        if not self.dry_run:
            committed = self._finalize_git_commit(
                {
                    "id": db_msg_id,
                    "file_path": file_path,
                    "msg_type": msg.msg_type,
                    "created_by": msg.created_by,
                    "outbox_file": outbox_name,
                    "status": STATUS_DONE_PENDING_COMMIT,
                }
            )
        else:
            self.conn.execute(
                "UPDATE mailbox_messages SET status=? WHERE id=?",
                (STATUS_DONE, db_msg_id),
            )
            self.conn.commit()

        return {
            "file": rel,
            "status": "done" if committed or self.dry_run else "skipped",
            "type": msg.msg_type,
            "outbox": outbox_name,
            "idea_id": idea_id,
        }

    def _finalize_git_commit(self, message_row: dict[str, Any]) -> bool:
        msg_id = int(message_row["id"])
        file_path = Path(message_row["file_path"])
        msg_type = str(message_row["msg_type"] or "unknown")
        created_by = str(message_row["created_by"] or "unknown")
        outbox_file = message_row.get("outbox_file")
        if not outbox_file:
            logger.warning("Cannot finalize mailbox row %s: missing outbox_file", msg_id)
            return False

        outbox_path = self.outbox / str(outbox_file)
        if not outbox_path.exists():
            logger.warning("Cannot finalize mailbox row %s: missing outbox %s", msg_id, outbox_path)
            return False

        if file_path.exists():
            file_path.unlink()

        files_to_commit: list[Path] = [file_path, outbox_path]
        if msg_type == "idea":
            files_to_commit.append(self.repo_path / "IDEAS.md")
        commit_msg = (
            f"mailbox: process {msg_type} from {created_by} [{Path(file_path).name}]"
        )
        if not git_add_and_commit(self.repo_path, files_to_commit, commit_msg):
            return False

        self.conn.execute(
            "UPDATE mailbox_messages SET status=? WHERE id=?",
            (STATUS_DONE, msg_id),
        )
        self.conn.commit()
        git_push(self.repo_path)
        return True

    def _handle_chain_outcome(
        self,
        outcome: Any,
        file_path: Path,
        rel: str,
        msg: "MailboxMessage",
        db_msg_id: int,
    ) -> dict[str, Any]:
        """Handle the return value of _process_operation_chain.

        _CHAIN_ACCEPTED  → no outbox file; commit only the inbox deletion.
        _ChainError(...)  → write a problem outbox file; commit inbox + outbox.
        """
        processed_at = _now_iso()

        if outcome is _CHAIN_ACCEPTED:
            # Persist acceptance in DB (outbox_file stays NULL)
            self.conn.execute(
                """
                UPDATE mailbox_messages
                SET status=?, processed_at=?, last_error=NULL
                WHERE id=?
                """,
                (STATUS_DONE_PENDING_COMMIT, processed_at, db_msg_id),
            )
            self.conn.commit()

            committed = False
            if not self.dry_run:
                committed = self._finalize_git_commit_inbox_only(
                    {
                        "id": db_msg_id,
                        "file_path": file_path,
                        "msg_type": msg.msg_type,
                        "created_by": msg.created_by,
                    }
                )
            else:
                self.conn.execute(
                    "UPDATE mailbox_messages SET status=? WHERE id=?",
                    (STATUS_DONE, db_msg_id),
                )
                self.conn.commit()

            return {
                "file": rel,
                "status": "done" if committed or self.dry_run else "skipped",
                "type": msg.msg_type,
            }

        # _ChainError: write a problem outbox file so the sender gets feedback
        assert isinstance(outcome, _ChainError), f"Unexpected chain outcome: {outcome!r}"
        outbox_name = _outbox_filename(msg, processed_at)
        outbox_path = self.outbox / outbox_name
        outbox_content = _build_outbox_content(
            msg, "rejected", processed_at, outcome.reason, None
        )
        if not self.dry_run:
            _atomic_write(outbox_path, outbox_content)

        self.conn.execute(
            """
            UPDATE mailbox_messages
            SET status=?, processed_at=?, outbox_file=?, last_error=?
            WHERE id=?
            """,
            (STATUS_DONE_PENDING_COMMIT, processed_at, outbox_name, outcome.reason, db_msg_id),
        )
        self.conn.commit()

        committed = False
        if not self.dry_run:
            committed = self._finalize_git_commit(
                {
                    "id": db_msg_id,
                    "file_path": file_path,
                    "msg_type": msg.msg_type,
                    "created_by": msg.created_by,
                    "outbox_file": outbox_name,
                    "status": STATUS_DONE_PENDING_COMMIT,
                }
            )
        else:
            self.conn.execute(
                "UPDATE mailbox_messages SET status=? WHERE id=?",
                (STATUS_DONE, db_msg_id),
            )
            self.conn.commit()

        return {
            "file": rel,
            "status": "done" if committed or self.dry_run else "skipped",
            "type": msg.msg_type,
            "outbox": outbox_name,
        }

    def _finalize_git_commit_inbox_only(self, message_row: dict[str, Any]) -> bool:
        """Commit only the inbox file deletion (no outbox file to add)."""
        msg_id = int(message_row["id"])
        file_path = Path(message_row["file_path"])
        msg_type = str(message_row["msg_type"] or "unknown")
        created_by = str(message_row["created_by"] or "unknown")

        if file_path.exists():
            file_path.unlink()

        commit_msg = (
            f"mailbox: process {msg_type} from {created_by} [{file_path.name}]"
        )
        if not git_add_and_commit(self.repo_path, [file_path], commit_msg):
            return False

        self.conn.execute(
            "UPDATE mailbox_messages SET status=? WHERE id=?",
            (STATUS_DONE, msg_id),
        )
        self.conn.commit()
        git_push(self.repo_path)
        return True

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
