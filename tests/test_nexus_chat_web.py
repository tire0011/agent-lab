import unittest
from pathlib import Path


class NexusChatWebTests(unittest.TestCase):
    def test_web_chat_sends_auth_and_persists_session(self):
        javascript = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn('const CHAT_SESSION_STORAGE_KEY = "nexusChatSessionId";', javascript)
        self.assertIn('headers: authHeaders()', javascript)
        self.assertIn('session_id: state.chatSessionId', javascript)
        self.assertIn('sessionStorage.setItem(CHAT_SESSION_STORAGE_KEY, response.session_id)', javascript)
        self.assertIn('response.experiment_id', javascript)


if __name__ == "__main__":
    unittest.main()
