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
        self.service.handle.return_value = SimpleNamespace(
            reply="Nexus reply",
            session_id="chat-session-1",
            action="answer",
            experiment_id=None,
            status="completed",
        )
        api.app.dependency_overrides[api.get_nexus_chat_service] = lambda: self.service
        self.client = TestClient(api.app)
        self.addCleanup(api.app.dependency_overrides.clear)

    def test_chat_requires_api_key(self):
        response = self.client.post("/chat", json={"message": "Hallo Nexus"})
        self.assertEqual(response.status_code, 401)
        self.service.handle.assert_not_called()

    def test_chat_returns_session_and_action(self):
        response = self.client.post(
            "/chat",
            headers={"x-api-key": "test-key"},
            json={"message": "Hallo Nexus", "session_id": "chat-session-1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], "chat-session-1")
        self.assertEqual(response.json()["action"], "answer")
        self.assertEqual(response.json()["status"], "completed")
        self.service.handle.assert_called_once_with("Hallo Nexus", session_id="chat-session-1")


if __name__ == "__main__":
    unittest.main()
