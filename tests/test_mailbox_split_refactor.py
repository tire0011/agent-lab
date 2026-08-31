import importlib.util
import unittest

from modules import mailbox


class MailboxSplitRefactorTests(unittest.TestCase):
    def test_mailbox_public_api_is_preserved_and_split_module_exists(self):
        self.assertTrue(hasattr(mailbox, "MailboxMessage"))
        self.assertTrue(hasattr(mailbox, "MailboxProcessor"))
        self.assertTrue(hasattr(mailbox, "MailboxLock"))
        self.assertTrue(hasattr(mailbox, "parse_front_matter"))
        self.assertTrue(hasattr(mailbox, "git_is_clean"))
        self.assertTrue(hasattr(mailbox, "_git"))
        self.assertIsNotNone(importlib.util.find_spec("modules.mailbox.core"))
        self.assertIsNotNone(importlib.util.find_spec("modules.mailbox.processing"))


if __name__ == "__main__":
    unittest.main()
