import unittest
from unittest.mock import patch

from Client import nexus_chat


class NexusChatClientTests(unittest.TestCase):
    def test_send_turn_submits_polls_and_preserves_session(self):
        responses = [
            {"job_id": "job-1", "status": "queued"},
            {"job_id": "job-1", "status": "running"},
            {
                "job_id": "job-1",
                "status": "succeeded",
                "reply": "Hallo",
                "session_id": "session-1",
                "action": "answer",
            },
        ]
        with patch.object(nexus_chat, "_request_json", side_effect=responses) as call, patch.object(
            nexus_chat.time, "sleep"
        ):
            payload, session_id = nexus_chat.send_turn(
                "http://127.0.0.1:8000",
                "secret",
                "Hallo Nexus",
                session_id="session-old",
            )

        self.assertEqual(payload["reply"], "Hallo")
        self.assertEqual(session_id, "session-1")
        self.assertEqual(call.call_args_list[0].args[:2], ("POST", "http://127.0.0.1:8000/chat"))
        self.assertEqual(call.call_args_list[0].kwargs["body"]["session_id"], "session-old")
        self.assertEqual(call.call_args_list[-1].args[:2], ("GET", "http://127.0.0.1:8000/chat/jobs/job-1"))

    def test_send_turn_surfaces_failed_job(self):
        responses = [
            {"job_id": "job-1", "status": "queued"},
            {"job_id": "job-1", "status": "failed", "error": "boom"},
        ]
        with patch.object(nexus_chat, "_request_json", side_effect=responses):
            with self.assertRaisesRegex(nexus_chat.ChatClientError, "boom"):
                nexus_chat.send_turn("http://x", "secret", "Hallo")

    def test_main_requires_api_key(self):
        with patch.dict(nexus_chat.os.environ, {}, clear=True):
            self.assertEqual(nexus_chat.main([]), 2)


if __name__ == "__main__":
    unittest.main()
