import os
import unittest
from unittest.mock import patch

os.environ.setdefault("AGENT_API_KEY", "test-key")

import api
from modules.mailbox_poll_runner import MailboxPollRunResult


class MailboxWebhookSyncFailureRegressionTests(unittest.TestCase):
    def test_webhook_poll_retries_once_after_sync_failed(self):
        with patch(
            "api.run_mailbox_poll_once",
            side_effect=[
                MailboxPollRunResult(status="sync_failed"),
                MailboxPollRunResult(status="processed"),
            ],
        ) as runner:
            api._run_mailbox_poll_once_for_webhook()

        self.assertEqual(runner.call_count, 2)


if __name__ == "__main__":
    unittest.main()
