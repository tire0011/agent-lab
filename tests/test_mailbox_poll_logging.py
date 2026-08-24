from __future__ import annotations

import unittest

from modules.mailbox_poll_runner import _safe_result_summary


class MailboxPollLoggingTests(unittest.TestCase):
    def test_result_summary_logs_only_status_and_bounded_basename(self) -> None:
        results = [
            {
                "status": "failed",
                "file": "/var/lib/nexus/mailbox-repo/inbox/example.md",
                "error": "secret message content that must never be logged",
                "body": "private mailbox body",
            },
            {
                "status": "done",
                "file": "inbox/second.md",
                "outbox": "outbox/response-containing-private-data.md",
            },
        ]

        summary = _safe_result_summary(results)

        self.assertEqual(summary, "failed:example.md,done:second.md")
        self.assertNotIn("secret message content", summary)
        self.assertNotIn("private mailbox body", summary)
        self.assertNotIn("response-containing-private-data", summary)

    def test_result_summary_is_bounded(self) -> None:
        results = [
            {"status": "done", "file": f"inbox/{index}.md"}
            for index in range(25)
        ]

        summary = _safe_result_summary(results, limit=2)

        self.assertEqual(summary, "done:0.md,done:1.md,+23_more")


if __name__ == "__main__":
    unittest.main()
