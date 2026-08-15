"""Service for executing validated, ordered chains of read-only operations.

State machine: queued → running → completed | completed_with_errors | failed | rejected | interrupted

Persistence: SQLite operation_chains table.
Idempotency: request_id uniqueness enforced at DB level.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from models import (
    OperationChainRequest,
    OperationChainResult,
    OperationChainStep,
    OperationChainStepResult,
)
from modules.outbox_publisher import PUBLISH_PENDING_WRITE, PUBLISH_WRITTEN, RepoLock
from modules.redaction import bounded_lines, redact
from modules.run_store import DEFAULT_DB_PATH, init_db

if TYPE_CHECKING:
    from modules.operations_registry import OperationsRegistry

logger = logging.getLogger(__name__)

_DEFAULT_REPO_ROOT = "/agent"
_DEFAULT_OUTBOX_DIR = "/agent/communication/outbox"

# Statuses
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_COMPLETED_WITH_ERRORS = "completed_with_errors"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_INTERRUPTED = "interrupted"

_TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS, STATUS_FAILED, STATUS_REJECTED, STATUS_INTERRUPTED}

# Allowlist of operations that can run in chains (read-only only)
_CHAIN_OPERATION_ALLOWLIST = {
    "git_status",
    "git_log",
    "git_worktrees",
    "experiment_show",
    "experiment_logs",
    "adoption_show",
    "service_status",
    "mailbox_status",
    "service_logs",
    "disk",
    "memory",
    "problems_list",
    "inbox_status",
    "outbox_status",
}

# Forbidden patterns in arguments (basic injection prevention)
_FORBIDDEN_PATTERNS = {
    "eval",
    "exec",
    "__import__",
    ";",
    "|",
    "&",
    ">",
    "<",
    "$(",
    "`",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, content: str) -> None:
    """Atomically write file via temp + rename."""
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


class OperationChainService:
    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = _DEFAULT_REPO_ROOT,
        outbox_dir: str = _DEFAULT_OUTBOX_DIR,
        operations_registry: OperationsRegistry | None = None,
        mailbox_config: Any = None,
    ) -> None:
        if mailbox_config is not None:
            self.repo_root = mailbox_config.mailbox_repo_path
            self.outbox_dir = mailbox_config.outbox_dir
        else:
            self.repo_root = Path(repo_root)
            self.outbox_dir = Path(outbox_dir)
        self.db_path = db_path
        self.operations_registry = operations_registry
        # RLock (not Lock): several code paths call locked helper methods while
        # already holding the lock (e.g. the exception handler in
        # _run_chain_pipeline calls _get_created_at/_get_started_at/_store_result
        # while holding self._lock). A plain Lock would deadlock there; an RLock
        # allows safe reentrant acquisition from the same thread while still
        # serializing all sqlite3 access across threads (the shared connection
        # is opened with check_same_thread=False and is not safe for
        # unsynchronized concurrent use).
        self._lock = threading.RLock()
        self._repo_lock = RepoLock(self.repo_root)
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._running_threads: dict[str, threading.Thread] = {}

    def close(self) -> None:
        """Join any still-running chain-execution threads before closing the
        shared sqlite3 connection. The connection is opened with
        check_same_thread=False specifically so the background pipeline
        threads can use it, so it must not be closed (or its backing file
        removed) while one of those threads might still be executing a
        query -- doing so corrupts the connection and can segfault the
        interpreter."""
        for thread in list(self._running_threads.values()):
            thread.join(timeout=10)
        self._conn.close()

    def start_chain(self, request: OperationChainRequest) -> OperationChainResult:
        """Start a chain: validate, check idempotency, queue, return status."""
        if request.request_id is None:
            request.request_id = uuid.uuid4().hex

        # Validate chain before ANY execution
        valid, reason = self._validate_chain(request)
        if not valid:
            result = OperationChainResult(
                request_id=request.request_id,
                overall_status=STATUS_REJECTED,
                created_at=_now_iso(),
                rejection_reason=reason,
                steps_successful=0,
                steps_failed=0,
            )
            self._store_result(result)
            self._write_outbox_result(request.request_id, result)
            return result

        # Check idempotency
        with self._lock:
            existing = self._get_chain_by_request_id(request.request_id)
            if existing:
                # Same content → return existing status; different → conflict
                existing_req = json.loads(existing["request_json"])
                if existing_req == json.loads(request.model_dump_json()):
                    return self.get_chain(request.request_id) or result
                else:
                    # Conflict: same request_id, different content
                    conflict_result = OperationChainResult(
                        request_id=request.request_id,
                        overall_status=STATUS_REJECTED,
                        created_at=_now_iso(),
                        rejection_reason="request_id already exists with different content",
                        steps_successful=0,
                        steps_failed=0,
                    )
                    self._store_result(conflict_result)
                    self._write_outbox_result(request.request_id, conflict_result)
                    return conflict_result

            # Create initial queued record
            chain_id = uuid.uuid4().hex
            now = _now_iso()
            self._conn.execute(
                """
                INSERT INTO operation_chains
                    (id, request_id, status, created_at, request_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chain_id, request.request_id, STATUS_QUEUED, now, request.model_dump_json(), now),
            )
            self._conn.commit()

        # Start background thread
        thread = threading.Thread(
            target=self._run_chain_pipeline,
            args=(chain_id, request),
            daemon=True,
        )
        self._running_threads[chain_id] = thread
        thread.start()

        result = OperationChainResult(
            request_id=request.request_id,
            overall_status=STATUS_QUEUED,
            created_at=now,
            steps_successful=0,
            steps_failed=0,
        )
        return result

    def get_chain(self, request_id: str) -> OperationChainResult | None:
        """Retrieve a chain's current status and results."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM operation_chains WHERE request_id = ?",
                (request_id,),
            ).fetchone()

        if not row:
            return None

        return self._row_to_result(row)

    def list_chains(self, limit: int = 20) -> list[OperationChainResult]:
        """List the most recent chains."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM operation_chains ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [self._row_to_result(row) for row in rows]

    def recover_after_restart(self) -> None:
        """Mark running chains as interrupted on startup."""
        with self._lock:
            self._conn.execute(
                "UPDATE operation_chains SET status = ? WHERE status = ?",
                (STATUS_INTERRUPTED, STATUS_RUNNING),
            )
            self._conn.commit()

    def _validate_chain(self, request: OperationChainRequest) -> tuple[bool, str | None]:
        """Comprehensive validation before execution."""
        # Unique step IDs
        step_ids = [s.id for s in request.steps]
        if len(step_ids) != len(set(step_ids)):
            return False, "duplicate step IDs"

        # Validate each step
        for step in request.steps:
            # Action in allowlist?
            if step.action not in _CHAIN_OPERATION_ALLOWLIST:
                return False, f"action '{step.action}' not in allowlist"

            # Arguments safe?
            for key, value in step.arguments.items():
                val_str = str(value)
                for pattern in _FORBIDDEN_PATTERNS:
                    if pattern in val_str:
                        return False, f"forbidden pattern '{pattern}' in argument '{key}'"

            # Arguments match operation spec?
            if self.operations_registry:
                spec = self.operations_registry._specs.get(step.action)
                if spec and spec.arguments:
                    for arg_spec in spec.arguments:
                        if arg_spec.name in step.arguments:
                            arg_val = step.arguments[arg_spec.name]
                            # Basic type check: if int, must be int
                            if arg_spec.type == "integer":
                                try:
                                    arg_val = int(arg_val)
                                except (ValueError, TypeError):
                                    return False, f"argument '{arg_spec.name}' must be int"
                                if arg_spec.minimum and arg_val < arg_spec.minimum:
                                    return False, f"argument '{arg_spec.name}' below minimum {arg_spec.minimum}"
                                if arg_spec.maximum and arg_val > arg_spec.maximum:
                                    return False, f"argument '{arg_spec.name}' exceeds maximum {arg_spec.maximum}"

        return True, None

    def _run_chain_pipeline(self, chain_id: str, request: OperationChainRequest) -> None:
        """Execute the chain sequentially in background thread."""
        try:
            with self._lock:
                now = _now_iso()
                self._conn.execute(
                    "UPDATE operation_chains SET status = ?, started_at = ?, updated_at = ? WHERE id = ?",
                    (STATUS_RUNNING, now, now, chain_id),
                )
                self._conn.commit()

            start_time = time.time()
            step_results: list[OperationChainStepResult] = []
            steps_successful = 0
            steps_failed = 0

            for step in request.steps:
                if request.stop_on_error and steps_failed > 0:
                    # Skip remaining steps
                    step_results.append(
                        OperationChainStepResult(
                            id=step.id,
                            action=step.action,
                            status="skipped",
                            started_at=_now_iso(),
                            duration_ms=0,
                        )
                    )
                    continue

                step_result = self._execute_step(step)
                step_results.append(step_result)
                if step_result.status == "success":
                    steps_successful += 1
                else:
                    steps_failed += 1

            # Determine overall status
            if steps_failed == 0:
                overall_status = STATUS_COMPLETED
            else:
                overall_status = STATUS_COMPLETED_WITH_ERRORS

            duration_ms = int((time.time() - start_time) * 1000)
            ended_at = _now_iso()

            result = OperationChainResult(
                request_id=request.request_id,
                overall_status=overall_status,
                created_at=self._get_created_at(chain_id) or ended_at,
                started_at=self._get_started_at(chain_id),
                ended_at=ended_at,
                duration_ms=duration_ms,
                steps_successful=steps_successful,
                steps_failed=steps_failed,
                steps=step_results,
            )

            # Store result
            self._store_result(result)
            self._write_outbox_result(request.request_id, result)

        except Exception as exc:
            logger.exception(f"Chain {chain_id} failed with exception")
            with self._lock:
                now = _now_iso()
                result = OperationChainResult(
                    request_id=request.request_id,
                    overall_status=STATUS_FAILED,
                    created_at=self._get_created_at(chain_id) or now,
                    started_at=self._get_started_at(chain_id),
                    ended_at=now,
                    duration_ms=0,
                    steps_successful=0,
                    steps_failed=0,
                )
                self._store_result(result)
                self._write_outbox_result(request.request_id, result)

        finally:
            self._running_threads.pop(chain_id, None)

    def _execute_step(self, step: OperationChainStep) -> OperationChainStepResult:
        """Execute one step and return result."""
        started_at = _now_iso()
        start_time = time.time()

        try:
            if not self.operations_registry:
                return OperationChainStepResult(
                    id=step.id,
                    action=step.action,
                    status="failed",
                    started_at=started_at,
                    duration_ms=int((time.time() - start_time) * 1000),
                    error="operations_registry not initialized",
                )

            result = self.operations_registry.execute(step.action, step.arguments)
            output_lines = result.output_lines[:100]  # Limit to 100 lines
            output_lines = [line[:500] if len(line) > 500 else line for line in output_lines]

            return OperationChainStepResult(
                id=step.id,
                action=step.action,
                status="success" if result.success else "failed",
                started_at=started_at,
                duration_ms=int((time.time() - start_time) * 1000),
                output=output_lines,
                error=result.error_summary if not result.success else None,
                truncated=len(result.output_lines) > 100 or any(len(line) > 500 for line in result.output_lines),
            )

        except subprocess.TimeoutExpired:
            duration_ms = int((time.time() - start_time) * 1000)
            return OperationChainStepResult(
                id=step.id,
                action=step.action,
                status="failed",
                started_at=started_at,
                duration_ms=duration_ms,
                error="operation timed out",
            )

        except Exception as exc:
            duration_ms = int((time.time() - start_time) * 1000)
            return OperationChainStepResult(
                id=step.id,
                action=step.action,
                status="failed",
                started_at=started_at,
                duration_ms=duration_ms,
                error=str(exc)[:200],
            )

    def _store_result(self, result: OperationChainResult) -> None:
        """Store/update result in DB."""
        with self._lock:
            now = _now_iso()
            self._conn.execute(
                """
                UPDATE operation_chains
                SET status = ?, result_json = ?, steps_successful = ?, steps_failed = ?,
                    started_at = ?, ended_at = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (
                    result.overall_status,
                    result.model_dump_json(),
                    result.steps_successful,
                    result.steps_failed,
                    result.started_at,
                    result.ended_at,
                    now,
                    result.request_id,
                ),
            )
            self._conn.commit()

    def _write_outbox_result(self, request_id: str, result: OperationChainResult) -> None:
        """Write operation_chain_result to outbox."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        filename = f"{timestamp}-operation-chain-result-{request_id}.md"
        filepath = self.outbox_dir / filename

        front_matter = f"""---
type: operation_chain_result
created_at: {_now_iso()}
request_id: {request_id}
status: {result.overall_status}
---
"""

        body = f"""# Operation Chain Result

**Request ID:** {request_id}  
**Status:** {result.overall_status}  
**Created:** {result.created_at}  
**Started:** {result.started_at or "N/A"}  
**Ended:** {result.ended_at or "N/A"}  
**Duration:** {result.duration_ms or "N/A"} ms  

## Summary

- Steps Successful: {result.steps_successful}
- Steps Failed: {result.steps_failed}
- Total Steps: {len(result.steps)}

"""

        if result.rejection_reason:
            body += f"**Rejection Reason:** {result.rejection_reason}\n\n"

        body += "## Steps\n\n"
        for step in result.steps:
            body += f"### {step.id} ({step.action})\n\n"
            body += f"**Status:** {step.status}  \n"
            body += f"**Duration:** {step.duration_ms} ms  \n"
            if step.error:
                body += f"**Error:** {step.error}  \n"
            if step.truncated:
                body += f"**Output:** (truncated)\n"
            if step.output:
                body += "```\n"
                body += "\n".join(step.output)
                body += "\n```\n"
            body += "\n"

        body += "\n## Raw Result JSON\n\n```json\n"
        body += json.dumps(result.model_dump(), indent=2)
        body += "\n```\n"

        content = front_matter + body
        try:
            # Compute relative path for the publisher.  If the outbox directory
            # is not under repo_root (e.g. in tests with a temp outbox dir) we
            # still write the file but skip the publish-lifecycle DB tracking.
            try:
                rel_path: str | None = str(filepath.relative_to(self.repo_root))
            except ValueError:
                rel_path = None

            with self._repo_lock:
                if rel_path is not None:
                    # Persist publish intent + full content before the atomic write
                    # so that a crash between write and the second DB update is
                    # recoverable via OperationChainOutboxPublisher.content_fn.
                    with self._lock:
                        self._conn.execute(
                            "UPDATE operation_chains SET outbox_file = ?, outbox_content = ?, publish_status = ?, updated_at = ? WHERE request_id = ?",
                            (rel_path, content, PUBLISH_PENDING_WRITE, _now_iso(), result.request_id),
                        )
                        self._conn.commit()

                _atomic_write(filepath, content)

                if rel_path is not None:
                    # Advance to written so the publisher knows the file exists on disk.
                    with self._lock:
                        self._conn.execute(
                            "UPDATE operation_chains SET publish_status = ?, updated_at = ? WHERE request_id = ?",
                            (PUBLISH_WRITTEN, _now_iso(), result.request_id),
                        )
                        self._conn.commit()
                else:
                    # Fallback: store absolute path so callers can still locate the file.
                    with self._lock:
                        self._conn.execute(
                            "UPDATE operation_chains SET outbox_file = ?, updated_at = ? WHERE request_id = ?",
                            (str(filepath), _now_iso(), result.request_id),
                        )
                        self._conn.commit()
        except Exception as exc:
            logger.error(f"Failed to write outbox file {filepath}: {exc}")

    def _get_chain_by_request_id(self, request_id: str) -> dict | None:
        """Get chain row by request_id."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM operation_chains WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def _get_created_at(self, chain_id: str) -> str | None:
        """Get created_at timestamp for chain."""
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM operation_chains WHERE id = ?",
                (chain_id,),
            ).fetchone()
        return row["created_at"] if row else None

    def _get_started_at(self, chain_id: str) -> str | None:
        """Get started_at timestamp for chain."""
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at FROM operation_chains WHERE id = ?",
                (chain_id,),
            ).fetchone()
        return row["started_at"] if row else None

    def _row_to_result(self, row: sqlite3.Row) -> OperationChainResult:
        """Convert DB row to OperationChainResult."""
        if row["result_json"]:
            data = json.loads(row["result_json"])
            return OperationChainResult(**data)
        else:
            return OperationChainResult(
                request_id=row["request_id"],
                overall_status=row["status"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                duration_ms=None,
                steps_successful=row["steps_successful"] or 0,
                steps_failed=row["steps_failed"] or 0,
            )
