import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.mailbox_poll_runner import _sync_mailbox_repo


class MailboxSyncFailureRegressionTests(unittest.TestCase):
    def test_sync_retries_after_transient_fetch_failure(self):
        mailbox = MagicMock()
        mailbox.git_is_clean.return_value = True
        mailbox._git.side_effect = [
            SimpleNamespace(returncode=1),  # first fetch fails transiently
            SimpleNamespace(returncode=0),  # second fetch succeeds
            SimpleNamespace(returncode=0),  # rebase succeeds
        ]
        mailbox.git_push.return_value = True

        result = _sync_mailbox_repo(mailbox, Path("/tmp/mailbox"), max_attempts=3)

        self.assertTrue(result)
        self.assertEqual(mailbox._git.call_count, 3)
        self.assertEqual(mailbox._git.call_args_list[0].args[0], ["fetch"])
        self.assertEqual(mailbox._git.call_args_list[1].args[0], ["fetch"])
        self.assertEqual(mailbox._git.call_args_list[2].args[0], ["rebase", "@{u}"])
        mailbox.git_push.assert_called_once()


if __name__ == "__main__":
    unittest.main()
