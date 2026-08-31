import hashlib
import hmac
import unittest

from modules.mailbox.github_webhook import has_inbox_changes, is_ping_event, verify_signature


class MailboxGithubWebhookModuleTests(unittest.TestCase):
    def test_verify_signature_accepts_valid_hmac(self):
        body = b'{"commits":[]}'
        secret = "abc123"
        signature = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        self.assertTrue(
            verify_signature(secret=secret, raw_body=body, signature_header=signature)
        )

    def test_verify_signature_rejects_missing_or_wrong_signature(self):
        body = b'{"commits":[]}'
        self.assertFalse(verify_signature(secret="abc", raw_body=body, signature_header=None))
        self.assertFalse(
            verify_signature(secret="abc", raw_body=body, signature_header="sha256=deadbeef")
        )

    def test_is_ping_event_is_case_insensitive(self):
        self.assertTrue(is_ping_event("ping"))
        self.assertTrue(is_ping_event("PING"))
        self.assertFalse(is_ping_event("push"))

    def test_has_inbox_changes_checks_added_and_modified_only(self):
        self.assertTrue(has_inbox_changes({"commits": [{"added": ["inbox/a.md"]}]}))
        self.assertTrue(has_inbox_changes({"commits": [{"modified": ["inbox/a.md"]}]}))
        self.assertFalse(has_inbox_changes({"commits": [{"removed": ["inbox/a.md"]}]}))
        self.assertFalse(has_inbox_changes({"commits": [{"added": ["outbox/a.md"]}]}))
        self.assertFalse(has_inbox_changes({"commits": "not-a-list"}))


if __name__ == "__main__":
    unittest.main()
