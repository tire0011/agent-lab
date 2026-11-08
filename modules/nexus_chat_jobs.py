from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from modules.central_logging import DEFAULT_LOG_DIR, get_chat_logger
from modules.nexus_chat_service import NexusChatResult, NexusChatService

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
_TERMINAL = {STATUS_SUCCEEDED, STATUS_FAILED}


@dataclass(slots=True)
class NexusChatJob:
    id: str
    status: str
    message: str
    session_id: str | None
    result: NexusChatResult | None = None
    error: str | None = None


class NexusChatJobService:
    """Run complete Nexus chat turns outside the request/response lifetime. Local inference
    can take minutes on the small VM, so keeping the orchestrator loop inside POST /chat makes
    the reverse proxy time out while Nexus is still working; this service keeps one bounded
    worker queue and lets the browser poll a short-lived job instead."""

    def __init__(
        self,
        chat_service: NexusChatService,
        *,
        id_factory: Callable[[], str] | None = None,
        log_dir: str | None = None,
    ) -> None:
        self.chat_service = chat_service
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        experiment_service = getattr(chat_service, "experiment_service", None)
        self.log_dir = log_dir or str(
            getattr(experiment_service, "experiment_log_dir", DEFAULT_LOG_DIR)
        )
        self._lock = threading.RLock()
        self._jobs: dict[str, NexusChatJob] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._closed = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="nexus-chat-job-worker",
            daemon=True,
        )
        self._worker.start()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put(None)
        self._worker.join(timeout=2.0)

    def submit(self, message: str, *, session_id: str | None = None) -> NexusChatJob:
        text = (message or "").strip()
        if not text:
            raise ValueError("message must not be empty")
        job = NexusChatJob(
            id=self.id_factory(),
            status=STATUS_QUEUED,
            message=text,
            session_id=session_id,
        )
        with self._lock:
            self._jobs[job.id] = job
        get_chat_logger(job.id, session_id=session_id, log_dir=self.log_dir).info(
            "chat", "job_submitted", "chat job submitted"
        )
        self._queue.put(job.id)
        return self.get(job.id)  # type: ignore[return-value]

    def get(self, job_id: str) -> NexusChatJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return NexusChatJob(
                id=job.id,
                status=job.status,
                message=job.message,
                session_id=job.session_id,
                result=job.result,
                error=job.error,
            )

    def _worker_loop(self) -> None:
        while not self._closed.is_set():
            job_id = self._queue.get()
            if job_id is None:
                return
            self._run(job_id)

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != STATUS_QUEUED:
                return
            job.status = STATUS_RUNNING
            message = job.message
            session_id = job.session_id

        task_log = get_chat_logger(job_id, session_id=session_id, log_dir=self.log_dir)
        task_log.info("chat", "job_running", "chat job started")
        try:
            result = self.chat_service.handle(message, session_id=session_id, job_id=job_id)
        except Exception as exc:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job.status = STATUS_FAILED
                    job.error = f"{type(exc).__name__}: {exc}"
            task_log.error("chat", "job_failed", "chat job failed", exc=exc)
            return

        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.status = STATUS_SUCCEEDED
                job.result = result
                job.session_id = result.session_id
        task_log.bind_session_id(result.session_id)
        task_log.info(
            "chat",
            "job_succeeded",
            "chat job completed",
            action=result.action,
            experiment_id=result.experiment_id,
        )
