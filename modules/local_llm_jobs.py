from __future__ import annotations

import asyncio
import os
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from modules.llama_cpp_client import LlamaCppClient
from modules.run_store import DEFAULT_DB_PATH

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
_TERMINAL = {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalLlmJobError(RuntimeError):
    pass


@dataclass(slots=True)
class LocalLlmJob:
    id: str
    status: str
    session_id: str | None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: str | None = None
    error: str | None = None


class LocalLlmJobService:
    """Persist and serialize slow local-LLM inference behind a single worker.

    The first implementation intentionally stays small: one daemon worker,
    SQLite persistence and polling.  llama-server remains the inference
    backend.  The controller no longer owns the long HTTP request itself.
    """

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        llama_client: LlamaCppClient | None = None,
        poll_interval: float = 0.25,
    ) -> None:
        self.db_path = db_path
        watchdog = float(os.environ.get("NEXUS_LOCAL_LLM_JOB_WATCHDOG_SECONDS", "3600"))
        watchdog = max(300.0, min(watchdog, 7200.0))
        self.llama = llama_client or LlamaCppClient(timeout=watchdog)
        self.poll_interval = max(0.05, poll_interval)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_llm_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                prompt TEXT NOT NULL,
                temperature REAL NOT NULL,
                max_tokens INTEGER NOT NULL,
                session_id TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                result TEXT,
                error TEXT
            )
            """
        )
        # A process restart cannot keep an in-flight HTTP request alive. Make
        # that state explicit rather than leaving an immortal `running` row.
        self._conn.execute(
            """
            UPDATE local_llm_jobs
            SET status = ?, finished_at = ?, error = ?
            WHERE status = ?
            """,
            (STATUS_FAILED, _now(), "local LLM worker interrupted by process restart", STATUS_RUNNING),
        )
        self._conn.commit()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._closed = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, name="local-llm-job-worker", daemon=True)
        self._worker.start()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put(None)
        self._worker.join(timeout=2.0)
        with self._lock:
            self._conn.close()

    def submit(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        session_id: str | None = None,
    ) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO local_llm_jobs
                    (id, status, prompt, temperature, max_tokens, session_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, STATUS_QUEUED, prompt, float(temperature), int(max_tokens), session_id, _now()),
            )
            self._conn.commit()
        self._queue.put(job_id)
        return job_id

    def get(self, job_id: str) -> LocalLlmJob | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,status,session_id,created_at,started_at,finished_at,result,error FROM local_llm_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return LocalLlmJob(**dict(row))

    async def wait(self, job_id: str) -> str:
        while True:
            job = self.get(job_id)
            if job is None:
                raise LocalLlmJobError(f"unknown local LLM job {job_id}")
            if job.status == STATUS_SUCCEEDED:
                return job.result or ""
            if job.status in {STATUS_FAILED, STATUS_CANCELLED}:
                raise LocalLlmJobError(job.error or f"local LLM job {job.status}")
            await asyncio.sleep(self.poll_interval)

    def _worker_loop(self) -> None:
        while not self._closed.is_set():
            job_id = self._queue.get()
            if job_id is None:
                return
            self._run(job_id)

    def _run(self, job_id: str) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT prompt,temperature,max_tokens,status FROM local_llm_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None or row["status"] != STATUS_QUEUED:
                return
            self._conn.execute(
                "UPDATE local_llm_jobs SET status = ?, started_at = ? WHERE id = ?",
                (STATUS_RUNNING, _now(), job_id),
            )
            self._conn.commit()
        started = time.perf_counter()
        try:
            result = asyncio.run(
                self.llama.complete(
                    prompt=str(row["prompt"]),
                    temperature=float(row["temperature"]),
                    max_tokens=int(row["max_tokens"]),
                )
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._conn.execute(
                    "UPDATE local_llm_jobs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                    (STATUS_FAILED, _now(), f"{type(exc).__name__}: {str(exc)[:1000]}", job_id),
                )
                self._conn.commit()
            return
        with self._lock:
            self._conn.execute(
                "UPDATE local_llm_jobs SET status = ?, finished_at = ?, result = ?, error = NULL WHERE id = ?",
                (STATUS_SUCCEEDED, _now(), result, job_id),
            )
            self._conn.commit()
