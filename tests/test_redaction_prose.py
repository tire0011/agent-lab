"""Redaction must scrub assignments, not prose.

Regression for #175. ``_SECRET_PATTERN`` used to accept a bare keyword
followed by whitespace, so any ordinary sentence containing "token",
"secret" or "password" lost its next word - and in serialized JSON the
greedy value pattern swallowed the closing quote and bracket, leaving the
persisted analysis unparseable. The live FailureAnalysis ``878e8008``
reported "The GitHub token=[REDACTED] not configured" because of it.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from modules.redaction import redact, redact_secrets


class RedactionProseTests(unittest.TestCase):
    def test_prose_mentioning_a_credential_keyword_is_untouched(self):
        for sentence in (
            "The GitHub token is not configured",
            "Configure GITHUB_TOKEN/GH_TOKEN before starting or resuming implementation.",
            "Check the GitHub token settings in the Nexus environment.",
            "This change does not access credentials at any point.",
            "The password was never the problem here.",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(redact_secrets(sentence), sentence)

    def test_keyword_assignments_are_still_redacted(self):
        for text, secret in (
            ("fatal: remote: token=ghp_abcXYZ123456789abcdef access denied", "ghp_abcXYZ123456789abcdef"),
            ("Authorization: Bearer ghp_AbCdEfGhIjKlMnOpQrStUvWxYz12 rejected", "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz12"),
            ("push rejected: sk-abcdefghijklmnopqrstuvwxyz1234 is invalid", "sk-abcdefghijklmnopqrstuvwxyz1234"),
            ("AZURE_OPENAI_API_KEY=sk-verysecretvalue1234", "sk-verysecretvalue1234"),
            ("api_key = hunter2hunter2 was rejected", "hunter2hunter2"),
            ("password:swordfish99", "swordfish99"),
            ("cookie=session_abcdef123456; Path=/", "session_abcdef123456"),
            ("url https://x-access-token:ghs_SomeSecret12345@github.com/o/r.git", "ghs_SomeSecret12345"),
        ):
            with self.subTest(text=text):
                cleaned = redact_secrets(text)
                self.assertNotIn(secret, cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_configured_secret_values_are_still_scrubbed(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_configuredvalue12345"}):
            cleaned = redact_secrets("the remote refused ghp_configuredvalue12345 outright")
        self.assertNotIn("ghp_configuredvalue12345", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_quoted_json_keys_are_redacted(self):
        """A serialized object writes the keyword quoted: ``{"password": ...}``.

        The character after the keyword is then ``"``, not ``:``. Every value
        here is deliberately shapeless - not ``ghp_``/``sk-`` and not a
        configured env value - so only the keyword/assignment pattern can
        catch it, and the test fails if that path regresses.
        """
        for key in ("password", "api_key", "token", "secret", "credential", "authorization"):
            with self.subTest(key=key):
                cleaned = redact_secrets(json.dumps({key: "hunter2hunter2"}))
                self.assertNotIn("hunter2hunter2", cleaned)
                self.assertEqual(json.loads(cleaned), {key: "[REDACTED]"})

    def test_redacting_serialized_json_keeps_it_parseable(self):
        document = {
            "missing_information": ["GitHub token status"],
            "summary": "The GitHub token is not configured",
            "model": "qwen2.5-coder-7b-instruct",
            # Shapeless on purpose: no shape pattern rescues this one.
            "config": {"token": "plainsecretvalue", "retries": 2},
            "tokens": 5,
        }
        cleaned = redact_secrets(json.dumps(document))

        self.assertNotIn("plainsecretvalue", cleaned)
        restored = json.loads(cleaned)
        self.assertEqual(restored["missing_information"], ["GitHub token status"])
        self.assertEqual(restored["summary"], "The GitHub token is not configured")
        self.assertEqual(restored["model"], "qwen2.5-coder-7b-instruct")
        self.assertEqual(restored["config"]["token"], "[REDACTED]")
        self.assertEqual(restored["config"]["retries"], 2)
        # A count that merely contains a keyword is not an assignment.
        self.assertEqual(restored["tokens"], 5)

    def test_shapeless_values_are_redacted_without_any_shape_pattern(self):
        for text, secret in (
            ('{"password": "hunter2hunter2"}', "hunter2hunter2"),
            ('{"api_key": "hunter2hunter2"}', "hunter2hunter2"),
            ("password=hunter2hunter2", "hunter2hunter2"),
            ("api_key: hunter2hunter2", "hunter2hunter2"),
            ("'secret' => 'hunter2hunter2'", "hunter2hunter2"),
        ):
            with self.subTest(text=text):
                cleaned = redact_secrets(text)
                self.assertNotIn(secret, cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_an_already_redacted_value_is_not_redacted_again(self):
        self.assertEqual(
            redact_secrets("fatal: remote: token=ghp_abcXYZ123456789abcdef denied"),
            "fatal: remote: token=[REDACTED] denied",
        )

    def test_redact_still_replaces_absolute_paths(self):
        cleaned = redact("failed reading /var/lib/nexus/agent.db")
        self.assertNotIn("/var/lib/nexus/agent.db", cleaned)
        self.assertIn("[PATH]", cleaned)


if __name__ == "__main__":
    unittest.main()
