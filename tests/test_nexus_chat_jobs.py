import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.nexus_chat_jobs import NexusChatJobService, STATUS_SUCCEEDED


class FakeChatService:
    experiment_service = SimpleNamespace(experiment_log_dir="state/experiment-logs")

    def handle(self, message, *, session_id=None, job_id=None):
        return SimpleNamespace(reply="done", session_id=session_id or "s1", action="answer", experiment_id=None, status="completed")


class NexusChatJobServiceTests(unittest.TestCase):
    def test_submit_returns_immediately_and_worker_completes(self):
        service = NexusChatJobService(FakeChatService(), id_factory=lambda: "job1")
        self.addCleanup(service.close)
        job = service.submit("hello", session_id="s1")
        self.assertEqual(job.id, "job1")
        deadline = time.time() + 2
        while time.time() < deadline:
            current = service.get("job1")
            if current and current.status == STATUS_SUCCEEDED:
                break
            time.sleep(0.01)
        current = service.get("job1")
        self.assertEqual(current.status, STATUS_SUCCEEDED)
        self.assertEqual(current.result.reply, "done")

    def test_job_lifecycle_is_logged(self):
        service = NexusChatJobService(FakeChatService(), id_factory=lambda: "job-log")
        self.addCleanup(service.close)
        with self.assertLogs("modules.nexus_chat_jobs", level="INFO") as logs:
            service.submit("hello", session_id="s-log")
            deadline = time.time() + 2
            while time.time() < deadline:
                current = service.get("job-log")
                if current and current.status == STATUS_SUCCEEDED:
                    break
                time.sleep(0.01)

        combined = "\n".join(logs.output)
        self.assertIn("nexus_chat_job submitted job_id=job-log", combined)
        self.assertIn("nexus_chat_job running job_id=job-log", combined)
        self.assertIn("nexus_chat_job succeeded job_id=job-log", combined)

    def test_failure_before_experiment_creation_is_structured(self):
        class FailingChatService(FakeChatService):
            def handle(self, message, *, session_id=None, job_id=None):
                raise RuntimeError("token=abcdef0123456789abcdef0123456789")

        with tempfile.TemporaryDirectory() as tmp:
            service = NexusChatJobService(
                FailingChatService(),
                id_factory=lambda: "job-failed",
                log_dir=tmp,
            )
            self.addCleanup(service.close)
            service.submit("hello", session_id="session-failed")
            deadline = time.time() + 2
            while time.time() < deadline:
                current = service.get("job-failed")
                if current and current.status == "failed":
                    break
                time.sleep(0.01)

            lines = [
                json.loads(line)
                for line in (Path(tmp) / "chat-job-failed.jsonl").read_text().splitlines()
            ]
            self.assertIn("job_failed", [entry["event"] for entry in lines])
            self.assertTrue(all(entry["job_id"] == "job-failed" for entry in lines))
            self.assertTrue(all(entry.get("experiment_id") is None for entry in lines))
            self.assertNotIn(
                "abcdef0123456789abcdef0123456789",
                json.dumps(lines),
            )


if __name__ == "__main__":
    unittest.main()
