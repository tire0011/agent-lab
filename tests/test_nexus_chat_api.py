import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient
import api


class NexusChatApiTests(unittest.TestCase):
    def setUp(self):
        self.service = MagicMock()
        self.service.submit.return_value = SimpleNamespace(id="job-1", status="queued", session_id="chat-1")
        api.app.dependency_overrides[api.get_nexus_chat_job_service] = lambda: self.service
        self.client = TestClient(api.app)
        self.addCleanup(api.app.dependency_overrides.clear)

    def test_chat_requires_api_key(self):
        response = self.client.post("/chat", json={"message": "Hallo Nexus"})
        self.assertEqual(response.status_code, 401)

    def test_chat_submit_returns_202_without_waiting_for_result(self):
        response = self.client.post("/chat", headers={"x-api-key": "test-key"}, json={"message": "Hallo", "session_id": "chat-1"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job_id"], "job-1")
        self.assertEqual(response.json()["status"], "queued")
        self.service.submit.assert_called_once_with("Hallo", session_id="chat-1")

    def test_poll_returns_completed_chat_result(self):
        result = SimpleNamespace(reply="Nexus reply", session_id="chat-1", action="answer", experiment_id=None, status="completed")
        self.service.get.return_value = SimpleNamespace(id="job-1", status="succeeded", session_id="chat-1", result=result, error=None)
        response = self.client.get("/chat/jobs/job-1", headers={"x-api-key": "test-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "Nexus reply")
        self.assertEqual(response.json()["session_id"], "chat-1")


if __name__ == "__main__":
    unittest.main()
