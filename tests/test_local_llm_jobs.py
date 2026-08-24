from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from modules.local_llm_jobs import LocalLlmJobService, STATUS_SUCCEEDED


class _SlowClient:
    async def complete(self, *, prompt, temperature, max_tokens):
        del temperature, max_tokens
        time.sleep(0.05)
        return "reply:" + prompt


class LocalLlmJobServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_is_persisted_and_completed_by_single_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = LocalLlmJobService(
                db_path=str(Path(tmp) / "jobs.db"),
                llama_client=_SlowClient(),
                poll_interval=0.01,
            )
            try:
                job_id = service.submit("hello", session_id="session-1", max_tokens=7)
                queued = service.get(job_id)
                self.assertIsNotNone(queued)
                result = await service.wait(job_id)
                job = service.get(job_id)
                self.assertEqual(result, "reply:hello")
                self.assertEqual(job.status, STATUS_SUCCEEDED)
                self.assertEqual(job.session_id, "session-1")
                self.assertIsNotNone(job.started_at)
                self.assertIsNotNone(job.finished_at)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
