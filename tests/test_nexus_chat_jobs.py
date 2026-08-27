import time
import unittest
from types import SimpleNamespace

from modules.nexus_chat_jobs import NexusChatJobService, STATUS_SUCCEEDED


class FakeChatService:
    def handle(self, message, *, session_id=None):
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


if __name__ == "__main__":
    unittest.main()
