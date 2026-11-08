"""Persistent Codex coding-task lifecycle in the shared SQLite store (``codex_tasks``),
executed through the existing workers, synchronously in the caller's thread - no
queue, threads, or parallelism.

Status lifecycle: queued -> running -> succeeded | failed | timed_out (``blocked``
is reserved; ``CodexResult`` has no reliable signal for it, so such cases map to
``failed`` rather than being guessed - see ``_map_result_to_status``).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from modules.coding_provider import (
    KNOWN_PROVIDERS,
    build_worker,
    choose_provider,
    provider_of,
)
from modules.codex_worker import CodexResult, CodexWorker, SANDBOX_MODES
from modules.codex_worker import PROVIDER as PROVIDER_CODEX
from modules.run_store import DEFAULT_DB_PATH, init_db

# Status constants

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"

_TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_BLOCKED, STATUS_FAILED, STATUS_TIMED_OUT)

_DEFAULT_MAX_PROMPT_BYTES = 16 * 1024
_DEFAULT_MAX_FINAL_MESSAGE_BYTES = 16 * 1024
_DEFAULT_MAX_ERROR_BYTES = 4 * 1024


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_text(text: str | None, max_bytes: int) -> str | None:
    if text is None:
        return None
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n[... truncated]"


class TaskNotFoundError(LookupError):
    """Raised when a task id does not exist in the store."""


class TaskNotRunnableError(RuntimeError):
    """Raised when ``run_task`` is called on a task that is not ``queued``."""


@dataclass
class CodexTask:
    id: str
    prompt: str
    working_dir: str
    sandbox: str
    timeout_seconds: int
    status: str
    created_at: str
    provider: str = PROVIDER_CODEX
    started_at: str | None = None
    ended_at: str | None = None
    thread_id: str | None = None
    exit_code: int | None = None
    runtime_ms: int | None = None
    timed_out: bool | None = None
    usage: dict[str, Any] | None = None
    final_message: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "working_dir": self.working_dir,
            "sandbox": self.sandbox,
            "timeout_seconds": self.timeout_seconds,
            "status": self.status,
            "created_at": self.created_at,
            "provider": self.provider,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "thread_id": self.thread_id,
            "exit_code": self.exit_code,
            "runtime_ms": self.runtime_ms,
            "timed_out": self.timed_out,
            "usage": self.usage,
            "final_message": self.final_message,
            "error": self.error,
        }


def _row_to_task(row: sqlite3.Row) -> CodexTask:
    usage_json = row["usage_json"]
    usage = json.loads(usage_json) if usage_json else None
    timed_out = row["timed_out"]
    # Pre-second-agent rows carry no provider; they were all Codex runs.
    provider = (
        row["provider"] if "provider" in row.keys() and row["provider"] else PROVIDER_CODEX
    )
    return CodexTask(
        id=row["id"],
        prompt=row["prompt"],
        working_dir=row["working_dir"],
        sandbox=row["sandbox"],
        timeout_seconds=row["timeout_seconds"],
        status=row["status"],
        created_at=row["created_at"],
        provider=provider,
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        thread_id=row["thread_id"],
        exit_code=row["exit_code"],
        runtime_ms=row["runtime_ms"],
        timed_out=None if timed_out is None else bool(timed_out),
        usage=usage,
        final_message=row["final_message"],
        error=row["error"],
    )


def _map_result_to_status(result: CodexResult) -> tuple[str, str | None]:
    """Map a ``CodexResult`` to a final ``(status, error)`` pair: timed_out first,
    then worker error or non-zero exit -> ``failed``, exit 0 -> ``succeeded``.
    ``blocked`` is never guessed - ``CodexResult`` has no reliable signal for it.
    """
    if result.timed_out:
        return STATUS_TIMED_OUT, result.error
    if result.error:
        return STATUS_FAILED, result.error
    if result.success:
        return STATUS_SUCCEEDED, None
    return STATUS_FAILED, result.error


class CodexTaskService:
    """Creates, persists, and runs external coding-agent tasks synchronously in
    the caller's thread, reusing the ``run_store`` SQLite infrastructure and the
    existing workers.

    The agent is decided once, at creation time, and stored with the task (caller-
    named, or the durable provider rotation); an injected *worker* pins every
    task to that worker instead (what tests and single-provider setups do).
    """

    def __init__(
        self,
        worker: CodexWorker | None = None,
        db_path: str = DEFAULT_DB_PATH,
        max_prompt_bytes: int = _DEFAULT_MAX_PROMPT_BYTES,
        max_final_message_bytes: int = _DEFAULT_MAX_FINAL_MESSAGE_BYTES,
        max_error_bytes: int = _DEFAULT_MAX_ERROR_BYTES,
        rng: Any | None = None,
    ) -> None:
        """worker pins every task to that worker (``None`` = production default:
        per-task provider rotation). rng is a deprecated compatibility argument;
        provider selection is a deterministic, persistent rotation.
        """
        self.worker = worker
        self.rng = rng
        self.db_path = db_path
        self.max_prompt_bytes = max_prompt_bytes
        self.max_final_message_bytes = max_final_message_bytes
        self.max_error_bytes = max_error_bytes

        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        # One connection for the service lifetime; FastAPI sync handlers arrive on
        # varying threads and sqlite3 connections are not thread-safe, so this lock
        # serializes access (short-held, per statement group).
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    # create_task

    def create_task(
        self,
        prompt: str,
        working_dir: str,
        sandbox: str = "read-only",
        timeout_seconds: int = 120,
        provider: str | None = None,
    ) -> CodexTask:
        """Persist a new task in status ``queued``; does not execute it. *provider*
        names the agent, or omit it for the service's rotation."""
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        if sandbox not in SANDBOX_MODES:
            raise ValueError(f"invalid sandbox {sandbox!r}; must be one of {SANDBOX_MODES}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if provider is not None and provider not in KNOWN_PROVIDERS:
            raise ValueError(f"invalid provider {provider!r}; must be one of {KNOWN_PROVIDERS}")

        resolved_provider = provider or self._provider_for_new_task()
        task_id = uuid.uuid4().hex
        created_at = _utcnow_iso()
        stored_prompt = _truncate_text(prompt, self.max_prompt_bytes) or ""

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO codex_tasks (
                    id, prompt, working_dir, sandbox, timeout_seconds,
                    status, created_at, provider, started_at, ended_at, thread_id,
                    exit_code, runtime_ms, timed_out, usage_json,
                    final_message, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL,
                          NULL, NULL, NULL, NULL, NULL, NULL)
                """,
                (
                    task_id,
                    stored_prompt,
                    working_dir,
                    sandbox,
                    timeout_seconds,
                    STATUS_QUEUED,
                    created_at,
                    resolved_provider,
                ),
            )

        return self.get_task(task_id)

    def _provider_for_new_task(self) -> str:
        """Provider for a task whose caller did not name one."""
        if self.worker is not None:
            # A test double may lack a usable provider name; anything unknown is
            # recorded as Codex, the only provider such callers were written for.
            return provider_of(self.worker, PROVIDER_CODEX) or PROVIDER_CODEX
        return choose_provider(self.rng, db_path=self.db_path)

    def _worker_for(self, task: CodexTask) -> Any:
        """The worker that runs *task*: the injected one, or the task's own."""
        if self.worker is not None:
            return self.worker
        return build_worker(task.provider)

    # get_task / list_tasks

    def get_task(self, task_id: str) -> CodexTask:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM codex_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"no task with id {task_id!r}")
        return _row_to_task(row)

    def list_tasks(self, limit: int = 20) -> list[CodexTask]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM codex_tasks ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    # run_task

    def run_task(self, task_id: str) -> CodexTask:
        """Execute *task_id* via the worker for its recorded provider: load (raises
        ``TaskNotFoundError``), atomically transition queued -> running (rejects a
        second start), run, persist the result; any exception persists ``failed``
        instead of leaving the task ``running``."""
        task = self.get_task(task_id)  # raises TaskNotFoundError if missing

        started_at = _utcnow_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE codex_tasks SET status = ?, started_at = ? "
                "WHERE id = ? AND status = ?",
                (STATUS_RUNNING, started_at, task_id, STATUS_QUEUED),
            )
        if cursor.rowcount != 1:
            current = self.get_task(task_id)
            raise TaskNotRunnableError(
                f"task {task_id!r} cannot be started from status "
                f"{current.status!r} (only {STATUS_QUEUED!r} tasks can run)"
            )

        try:
            result = self._worker_for(task).run(
                task=task.prompt,
                working_dir=task.working_dir,
                sandbox=task.sandbox,
                timeout_seconds=task.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - must never leave task 'running'
            self._finalize(
                task_id,
                status=STATUS_FAILED,
                thread_id=None,
                exit_code=None,
                runtime_ms=None,
                timed_out=False,
                usage=None,
                final_message=None,
                error=f"unhandled exception while running task: {exc}",
            )
            return self.get_task(task_id)

        status, error = _map_result_to_status(result)
        self._finalize(
            task_id,
            status=status,
            thread_id=result.thread_id,
            exit_code=result.exit_code,
            runtime_ms=result.runtime_ms,
            timed_out=result.timed_out,
            usage=result.usage,
            final_message=result.final_message,
            error=error,
        )
        return self.get_task(task_id)

    def _finalize(
        self,
        task_id: str,
        *,
        status: str,
        thread_id: str | None,
        exit_code: int | None,
        runtime_ms: int | None,
        timed_out: bool | None,
        usage: dict[str, Any] | None,
        final_message: str | None,
        error: str | None,
    ) -> None:
        ended_at = _utcnow_iso()
        usage_json = json.dumps(usage) if usage is not None else None
        final_message_capped = _truncate_text(final_message, self.max_final_message_bytes)
        error_capped = _truncate_text(error, self.max_error_bytes)

        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE codex_tasks
                SET status = ?, ended_at = ?, thread_id = ?, exit_code = ?,
                    runtime_ms = ?, timed_out = ?, usage_json = ?,
                    final_message = ?, error = ?
                WHERE id = ?
                """,
                (
                    status,
                    ended_at,
                    thread_id,
                    exit_code,
                    runtime_ms,
                    None if timed_out is None else (1 if timed_out else 0),
                    usage_json,
                    final_message_capped,
                    error_capped,
                    task_id,
                ),
            )

    # Interrupted-task recovery (no auto-restart)

    def mark_interrupted_tasks_failed(self) -> list[str]:
        """Mark orphaned ``running`` tasks as ``failed`` (startup bookkeeping only,
        never restarts); returns the marked task ids."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM codex_tasks WHERE status = ?", (STATUS_RUNNING,)
            ).fetchall()
            ids = [row["id"] for row in rows]

            ended_at = _utcnow_iso()
            with self._conn:
                self._conn.executemany(
                    """
                    UPDATE codex_tasks
                    SET status = ?, ended_at = ?, error = ?
                    WHERE id = ? AND status = ?
                    """,
                    [
                        (
                            STATUS_FAILED,
                            ended_at,
                            "interrupted: task was still 'running' at service "
                            "startup (process restart or crash); no automatic "
                            "recovery is implemented",
                            task_id,
                            STATUS_RUNNING,
                        )
                        for task_id in ids
                    ],
                )
        return ids


# CLI entry point for manual smoke-testing

def _build_cli_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m modules.codex_task_service",
        description=(
            "Create a Codex task, run it synchronously via CodexWorker, "
            "and print its final persisted state."
        ),
    )
    parser.add_argument("prompt", help="Natural-language coding task")
    parser.add_argument(
        "--working-dir", "-C", default="/agent",
        help="Absolute working directory (default: /agent)",
    )
    parser.add_argument(
        "--sandbox", "-s", default="read-only", choices=list(SANDBOX_MODES),
        help="Sandbox mode (default: read-only)",
    )
    parser.add_argument(
        "--timeout", "-t", type=int, default=120,
        help="Timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH,
        help=f"SQLite db path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--provider", default=None, choices=list(KNOWN_PROVIDERS),
        help="Coding agent to use (default: next in the persistent installed-provider rotation)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    import json as _json
    from modules.logging_config import configure_logging

    configure_logging()

    parser = _build_cli_parser()
    args = parser.parse_args(argv)

    service = CodexTaskService(db_path=args.db)
    service.mark_interrupted_tasks_failed()

    task = service.create_task(
        prompt=args.prompt,
        working_dir=args.working_dir,
        sandbox=args.sandbox,
        timeout_seconds=args.timeout,
        provider=args.provider,
    )
    print(f"created task {task.id} (status={task.status}, provider={task.provider})")

    result_task = service.run_task(task.id)
    print(_json.dumps(result_task.to_dict(), ensure_ascii=False, indent=2))

    service.close()
    return 0 if result_task.status == STATUS_SUCCEEDED else 1


if __name__ == "__main__":
    raise SystemExit(main())
