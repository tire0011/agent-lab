"""High-level mailbox processing workflow."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from modules.central_logging import DEFAULT_LOG_DIR, get_task_logger
from .refresh import process_refresh as _process_refresh
from .task_ingest import insert_task_approval, resolve_idempotency_key

from .core import (
    MAX_RETRY,
    STATUS_DEFERRED,
    STATUS_DONE,
    STATUS_DONE_PENDING_COMMIT,
    MailboxMessage,
    _DEFERRED_PROCESSING,
    _atomic_write,
    _build_outbox_content,
    _git,
    _now_iso,
    _outbox_filename,
    git_add_and_commit,
    git_current_commit,
    git_is_clean,
    git_pull_ff_only,
    git_push,
    init_mailbox_db,
)

logger = logging.getLogger(__name__)


def transient_mailbox_task_id(relative_source: str) -> str:
    """Stable, content-free correlation id available before parsing succeeds.

    Public because the log-trace read path (``modules.log_trace``) has to
    derive the same id from an inbox filename to find a message's log
    file - both sides must agree on one definition, not two.
    """
    digest = hashlib.sha256(relative_source.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"mailbox-{digest}"


# Backwards-compatible private alias for existing call sites/tests.
_transient_mailbox_task_id = transient_mailbox_task_id


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
        idx = content.index(subsection_header) + len(subsection_header)
        nl = content.find("\n", idx)
        insert_pos = nl + 1 if nl != -1 else len(content)
        content = content[:insert_pos] + entry + content[insert_pos:]
    else:
        ideas_section = "## Ideas"
        if ideas_section not in content:
            content += f"\n{ideas_section}\n\n{subsection_header}\n{entry}"
        else:
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
    """Record task request and create pending approval."""
    explicit_key = msg.meta.get("idempotency_key") or None
    # A malformed key fails the message visibly instead of being swallowed
    # by the best-effort approval insert below.
    resolve_idempotency_key(db_msg_id, explicit_key)
    try:
        conn.execute(
            "UPDATE mailbox_messages SET task_body = ? WHERE id = ?",
            (msg.body, db_msg_id),
        )
        conn.commit()
    except Exception:
        logger.warning("_process_task: failed to store task_body for msg %s", db_msg_id)

    try:
        # A retry of this same inbox row keeps the task it already created,
        # even if the sender edited the file's key in between.
        existing = conn.execute(
            "SELECT id FROM mailbox_task_approvals WHERE mailbox_msg_id = ?",
            (db_msg_id,),
        ).fetchone()
        if not existing:
            # An optional front-matter ``idempotency_key`` lets a sender
            # redeliver the same task as a new file without a second task
            # (#378); otherwise the key is derived from this inbox row.
            approval_id, created = insert_task_approval(
                conn,
                mailbox_msg_id=db_msg_id,
                task_body=msg.body,
                sender=msg.created_by,
                file_path=str(msg.file_path),
                received_at=_now_iso(),
                idempotency_key=explicit_key,
            )
            if not created:
                logger.info(
                    "_process_task: msg %s is a duplicate delivery of task %s",
                    db_msg_id, approval_id,
                )
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
    """Acknowledge question."""
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
    """Store context in knowledge table."""
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
    """Parse and validate JSON body of an operation_chain message."""
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


_CHAIN_ACCEPTED = object()


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
    """Parse, validate, and start an operation_chain message."""
    import hashlib as _hashlib

    data, parse_error = _parse_operation_chain_body(msg.body)
    if parse_error:
        logger.warning("operation_chain parse error for %s: %s", msg.file_path.name, parse_error)
        return _ChainError(f"Operation chain rejected: {parse_error}")

    request_id: str | None = msg.meta.get("request_id", "").strip() or None
    if request_id and not request_id.replace("-", "").replace("_", "").isalnum():
        return _ChainError("Operation chain rejected: request_id must be alphanumeric with dashes/underscores")

    content_hash = _hashlib.sha256(msg.body.strip().encode()).hexdigest()[:16]
    if request_id is None:
        request_id = f"inbox-{content_hash}"

    existing_row = conn.execute(
        "SELECT id, chain_request_id, content_hash FROM mailbox_messages WHERE chain_request_id = ?",
        (request_id,),
    ).fetchone()
    if existing_row:
        existing_hash = existing_row[2] if len(existing_row) > 2 else None
        if existing_hash == content_hash:
            logger.info("operation_chain idempotent: request_id=%s already accepted", request_id)
            return _CHAIN_ACCEPTED
        return _ChainError(f"Conflict: request_id `{request_id}` already exists with different content.")

    try:
        conn.execute(
            "UPDATE mailbox_messages SET chain_request_id = ?, content_hash = ? WHERE id = ?",
            (request_id, content_hash, db_msg_id),
        )
        conn.commit()
    except Exception:
        logger.warning("_process_operation_chain: failed to persist idempotency data for msg %s", db_msg_id)

    if operation_chain_service is None:
        return _ChainError("Operation chain service not available; chain not started.")

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
        request_id,
        result.overall_status,
        len(steps),
    )
    return _CHAIN_ACCEPTED


_PROCESSORS = {
    "idea": _process_idea,
    "task": _process_task,
    "question": _process_question,
    "context": _process_context,
    # Moving the deployment to a branch, with no shell in the message (#228).
    "refresh": _process_refresh,
}


class MailboxProcessor:
    """Processes inbox messages and produces outbox responses."""

    def __init__(
        self,
        repo_path: Path,
        db_path: str,
        dry_run: bool = False,
        operation_chain_service: Any = None,
        mailbox_config: Any = None,
        task_log_dir: str | Path = DEFAULT_LOG_DIR,
    ) -> None:
        if mailbox_config is not None:
            self._mailbox_repo_path: Path = mailbox_config.mailbox_repo_path
            self._code_repo_path: Path = mailbox_config.code_repo_path
            self.inbox = mailbox_config.inbox_dir
            self.outbox = mailbox_config.outbox_dir
            self.repo_path = mailbox_config.mailbox_repo_path
        else:
            self._mailbox_repo_path = repo_path
            self._code_repo_path = repo_path
            self.repo_path = repo_path
            self.inbox = repo_path / "inbox"
            self.outbox = repo_path / "outbox"
        self._mailbox_commit_prefixes: frozenset[str] = frozenset(
            str(d.relative_to(self._mailbox_repo_path))
            for d in (self.inbox, self.outbox)
        )
        self.db_path = db_path
        self.dry_run = dry_run
        self.operation_chain_service = operation_chain_service
        self.task_log_dir = Path(task_log_dir)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            from modules.run_store import init_db
            self._conn = init_db(self.db_path)
            init_mailbox_db(self._conn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def retry_unpushed_commits(self) -> None:
        result = _git(["log", "@{u}..", "--oneline"], self._mailbox_repo_path, check=False)
        if result.returncode == 0 and result.stdout.strip():
            logger.info("Retrying push of unpushed commits")
            git_push(self._mailbox_repo_path)

    def pull(self) -> bool:
        return git_pull_ff_only(self._mailbox_repo_path)

    def ensure_clean_worktree(self) -> bool:
        return git_is_clean(self._mailbox_repo_path)

    def prune_old_messages(self, *, now=None) -> Any:
        """Delete finished outbox responses and dead inbox messages.

        Runs as part of the normal poll (see ``run_mailbox_poll_once``),
        after pending commits have been retried - a file still needed to
        finalize a commit is protected, and retrying first lets those
        records reach ``done`` so the next poll can prune them.

        The deletions are committed with explicit pathspecs; pushing is
        left to the poll's existing fetch/rebase/push sync. A pass with
        nothing to prune makes no commit at all.
        """
        from . import retention

        days = retention.retention_days()
        plan = retention.plan_prune(
            self.conn, inbox=self.inbox, outbox=self.outbox, days=days, now=now,
        )
        if not plan or self.dry_run:
            return plan

        for path in plan.inbox:
            self._log_terminal_inbox_message(path)

        removed: list[Path] = []
        for path in plan.paths:
            try:
                path.unlink()
                removed.append(path)
            except FileNotFoundError:
                removed.append(path)  # already gone; still stage the deletion
            except OSError:
                logger.warning("could not delete mailbox file %s", path.name)

        if not removed:
            return plan

        commit_msg = f"mailbox: prune {plan.summary()} older than {days}d"
        if not git_add_and_commit(
            self._mailbox_repo_path, removed, commit_msg, self._mailbox_commit_prefixes
        ):
            logger.warning("mailbox prune could not be committed")
        else:
            logger.info(
                "Mailbox pruned %s (protected=%d truncated=%s)",
                plan.summary(), plan.protected, plan.truncated,
            )
        return plan

    def _log_terminal_inbox_message(self, path: Path) -> None:
        """Record why an inbox message is being dropped, before it is gone.

        Written through the same task logger the message used while it was
        being processed, so ``GET /logs/trace/{reference}`` can still
        explain what happened to it once the file no longer exists.
        """
        try:
            rel = str(path.relative_to(self._mailbox_repo_path))
        except ValueError:
            rel = path.name
        row = self.conn.execute(
            "SELECT retry_count, last_error, msg_type FROM mailbox_messages WHERE file_path = ?",
            (str(path),),
        ).fetchone()
        task_log = get_task_logger(
            _transient_mailbox_task_id(rel), log_dir=self.task_log_dir,
        )
        task_log.warning(
            "mailbox",
            "message_discarded",
            "mailbox message removed from the inbox after exhausting its retries",
            source_file=path.name,
            message_type=str(row[2]) if row else "unknown",
            retry_count=int(row[0]) if row and row[0] is not None else MAX_RETRY,
            last_error=str(row[1]) if row and row[1] else "unknown",
        )

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

    def process_inbox(self, files: list[Path] | None = None) -> list[dict[str, Any]]:
        """Process inbox messages, by default every message currently present.

        ``files`` restricts the run to an explicit batch.  The inbox drain loop
        uses it to process only the messages that arrived after an earlier
        pass, so a message deliberately left behind — deferred, awaiting a
        retry, or out of retries — is not re-run within the same invocation.
        """
        if files is None:
            files = self._get_inbox_files()
        results = []
        for f in files:
            results.append(self._process_file(f))
        return results

    def list_inbox_files(self) -> list[Path]:
        """Inbox messages currently on disk, oldest first.

        Public so a caller can compare two scans of the queue without reaching
        into the processor internals.
        """
        return self._get_inbox_files()

    def _get_inbox_files(self) -> list[Path]:
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
        rel = str(file_path.relative_to(self._mailbox_repo_path))
        task_log = get_task_logger(
            _transient_mailbox_task_id(rel),
            log_dir=self.task_log_dir,
        )
        task_log.info(
            "mailbox",
            "message_received",
            "mailbox message discovered",
            source_file=file_path.name,
        )

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

        try:
            msg = MailboxMessage.load(file_path, self.inbox)
        except (ValueError, UnicodeDecodeError) as exc:
            self._record_failure(file_path, str(exc), existing)
            task_log.warning(
                "mailbox",
                "message_rejected",
                f"mailbox message rejected during validation: {exc}",
                source_file=file_path.name,
                validation_error=str(exc),
            )
            return {"file": rel, "status": "failed", "error": str(exc)}

        commit_sha = git_current_commit(self._mailbox_repo_path)
        now = _now_iso()

        if existing:
            if existing["status"] == STATUS_DEFERRED:
                self.conn.execute(
                    "UPDATE mailbox_messages SET status='processing' WHERE id=?",
                    (existing["id"],),
                )
            else:
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

        try:
            if msg.msg_type == "operation_chain":
                chain_outcome = _process_operation_chain(
                    msg,
                    self._code_repo_path,
                    self.conn,
                    db_msg_id,
                    operation_chain_service=self.operation_chain_service,
                )
                return self._handle_chain_outcome(
                    chain_outcome,
                    file_path,
                    rel,
                    msg,
                    db_msg_id,
                )
            processor = _PROCESSORS[msg.msg_type]
            processor_result = processor(msg, self._code_repo_path, self.conn, db_msg_id)
            if processor_result is _DEFERRED_PROCESSING:
                self.conn.execute(
                    "UPDATE mailbox_messages SET status=? WHERE id=?",
                    (STATUS_DEFERRED, db_msg_id),
                )
                self.conn.commit()
                return {"file": rel, "status": "pending", "type": msg.msg_type}
            result_body, idea_id = processor_result
        except Exception as exc:
            logger.exception("Error processing %s", file_path)
            self._record_failure(file_path, str(exc), {"id": db_msg_id})
            task_log.error(
                "mailbox",
                "message_processing_failed",
                "mailbox message processing failed",
                exc=exc,
                source_file=file_path.name,
            )
            return {"file": rel, "status": "failed", "error": str(exc)}

        processed_at = _now_iso()
        outbox_name = _outbox_filename(msg, processed_at)
        outbox_path = self.outbox / outbox_name
        outbox_content = _build_outbox_content(msg, "done", processed_at, result_body, idea_id)

        if not self.dry_run:
            _atomic_write(outbox_path, outbox_content)

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

        commit_msg = f"mailbox: process {msg_type} from {created_by} [{Path(file_path).name}]"

        mailbox_files: list[Path] = [file_path, outbox_path]
        if self._mailbox_repo_path == self._code_repo_path:
            if msg_type == "idea":
                mailbox_files.append(self._code_repo_path / "IDEAS.md")
            if not git_add_and_commit(self._mailbox_repo_path, mailbox_files, commit_msg, self._mailbox_commit_prefixes):
                return False
        else:
            if not git_add_and_commit(self._mailbox_repo_path, mailbox_files, commit_msg, self._mailbox_commit_prefixes):
                return False
            if msg_type == "idea":
                ideas_path = self._code_repo_path / "IDEAS.md"
                if not git_add_and_commit(self._code_repo_path, [ideas_path], commit_msg):
                    logger.warning("Failed to commit IDEAS.md to code repo for mailbox row %s", msg_id)
                    return False
        self.conn.execute(
            "UPDATE mailbox_messages SET status=? WHERE id=?",
            (STATUS_DONE, msg_id),
        )
        self.conn.commit()
        git_push(self._mailbox_repo_path)
        return True

    def _handle_chain_outcome(
        self,
        outcome: Any,
        file_path: Path,
        rel: str,
        msg: MailboxMessage,
        db_msg_id: int,
    ) -> dict[str, Any]:
        processed_at = _now_iso()

        if outcome is _CHAIN_ACCEPTED:
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

        assert isinstance(outcome, _ChainError), f"Unexpected chain outcome: {outcome!r}"
        outbox_name = _outbox_filename(msg, processed_at)
        outbox_path = self.outbox / outbox_name
        outbox_content = _build_outbox_content(msg, "rejected", processed_at, outcome.reason, None)
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

        commit_msg = f"mailbox: process {msg_type} from {created_by} [{file_path.name}]"
        if not git_add_and_commit(self._mailbox_repo_path, [file_path], commit_msg, self._mailbox_commit_prefixes):
            return False

        self.conn.execute(
            "UPDATE mailbox_messages SET status=? WHERE id=?",
            (STATUS_DONE, msg_id),
        )
        self.conn.commit()
        git_push(self._mailbox_repo_path)
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
