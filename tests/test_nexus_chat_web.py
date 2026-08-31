import unittest
from pathlib import Path


class NexusChatWebTests(unittest.TestCase):
    def test_web_chat_sends_auth_and_persists_session(self):
        javascript = "\n".join(p.read_text(encoding="utf-8") for p in sorted(Path("static").glob("*.js")))
        self.assertIn('const CHAT_SESSION_STORAGE_KEY = "nexusChatSessionId";', javascript)
        self.assertIn('headers: authHeaders()', javascript)
        self.assertIn('session_id: state.chatSessionId', javascript)
        self.assertIn('sessionStorage.setItem(CHAT_SESSION_STORAGE_KEY, response.session_id)', javascript)
        self.assertIn('response.experiment_id', javascript)
        self.assertIn('/chat/jobs/${encodeURIComponent(jobId)}', javascript)
        self.assertIn('setTimeout(resolve, 2000)', javascript)


if __name__ == "__main__":
    unittest.main()
