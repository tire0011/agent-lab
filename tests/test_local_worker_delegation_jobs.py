"""`local_worker` delegation runs on the shared local-LLM job path (#167 review).

`NexusDelegationService._local()` used to call `LlamaCppClient.complete()`
directly, which left a third independent request path to llama-server: a local
delegation could run outside `LocalLlmJobService` and race a queued controller
or failure-analysis job. D-019 says every local-model caller goes through the
one mechanism, so local delegation is now another role on the same queue.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from modules.local_llm_jobs import (
    ROLE_LOCAL_WORKER,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    LocalLlmJobService,
)
from modules.nexus_delegation import NexusDelegationService


class _RecordingClient:
    model = "test-local-model"

    def __init__(self, response: str | Exception = "worker findings"):
        self._response = response
        self.threads: list[str] = []

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None, system=None):
        del prompt, temperature, max_tokens, response_format, system
        self.threads.append(threading.current_thread().name)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class LocalWorkerDelegationJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = str(self.tmp / "agent.db")

    def _service(self, client) -> tuple[NexusDelegationService, LocalLlmJobService]:
        jobs = LocalLlmJobService(db_path=self.db_path, llama_client=client, poll_interval=0.01)
        self.addCleanup(jobs.close)
        delegation = NexusDelegationService(
            llama_client=client, db_path=self.db_path, llm_job_service=jobs
        )
        self.addCleanup(delegation.close)
        return delegation, jobs

    def _rows(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT id,role,status FROM local_llm_jobs").fetchall()
        finally:
            conn.close()

    def test_local_delegation_is_queued_as_a_local_worker_job(self) -> None:
        client = _RecordingClient()
        delegation, _jobs = self._service(client)

        result = asyncio.run(delegation.delegate("local_worker", "analyse this", context="ctx"))

        self.assertTrue(result.success)
        self.assertEqual(result.content, "worker findings")

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], ROLE_LOCAL_WORKER)
        self.assertEqual(rows[0]["status"], STATUS_SUCCEEDED)
        self.assertEqual(result.metadata["job_id"], rows[0]["id"])

        # The request ran on the shared job worker, not inline on the caller's
        # thread as a direct client call would.
        self.assertEqual(len(client.threads), 1)
        self.assertTrue(client.threads[0].startswith("local-llm-job-worker"))

    def test_a_failed_delegation_job_degrades_without_raising(self) -> None:
        client = _RecordingClient(RuntimeError("llama-server connection refused"))
        delegation, _jobs = self._service(client)

        result = asyncio.run(delegation.delegate("local_worker", "analyse this"))

        self.assertFalse(result.success)
        self.assertEqual(result.metadata["error"], "LocalLlmJobError")
        self.assertEqual(self._rows()[0]["status"], STATUS_FAILED)

    def test_the_orchestrator_hands_delegation_its_own_job_service(self) -> None:
        from types import SimpleNamespace

        from modules.task_orchestrator import NexusTaskOrchestrator

        client = _RecordingClient()
        orchestrator = NexusTaskOrchestrator(
            operations_registry=SimpleNamespace(db_path=self.db_path, repo_root=str(self.tmp)),
            experiment_service=SimpleNamespace(),
            llama_client=client,
        )
        self.addCleanup(orchestrator.close)

        # Controller inference and local delegation are the same queue, so a
        # delegate cannot open a second concurrent request to llama-server.
        self.assertIs(orchestrator.delegation.llm_jobs, orchestrator.llm_jobs)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
