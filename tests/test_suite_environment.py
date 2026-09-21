"""The suite's own environment, pinned.

`tests/conftest.py` defaulted `AGENT_API_KEY` to `test-api-key` while every
API test module sends `x-api-key: test-key`. CI exports `AGENT_API_KEY:
test-key`, so the `setdefault` never fired there and CI stayed green - while
on a developer machine and on the VM, where nothing exports it, roughly 90
authenticated tests answered 401 instead of 200.

That is the failure mode worth guarding: a suite that passes only under the
one environment that happens to override its own default. These tests fail
wherever the key the modules send and the key the app checks disagree, CI
included.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS = _REPO_ROOT / "tests"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "tests.yml"

# What every API test module sends.
EXPECTED_API_KEY = "test-key"


class ApiKeyAgreementTests(unittest.TestCase):
    def test_the_environment_carries_the_key_the_modules_send(self):
        self.assertEqual(os.environ.get("AGENT_API_KEY"), EXPECTED_API_KEY)

    def test_the_app_checks_that_same_key(self):
        import api

        self.assertEqual(api.API_KEY, EXPECTED_API_KEY)

    def test_no_test_module_sends_a_different_key(self):
        """A module with its own literal would 401 for the same reason."""
        pattern = re.compile(r'os\.environ\.setdefault\(\s*"AGENT_API_KEY"\s*,\s*"([^"]+)"')
        disagreeing = {}
        for path in sorted(_TESTS.glob("*.py")):
            for value in pattern.findall(path.read_text(encoding="utf-8")):
                if value != EXPECTED_API_KEY:
                    disagreeing[path.name] = value
        self.assertEqual(
            disagreeing,
            {},
            "these modules default AGENT_API_KEY to something the API tests do not send",
        )

    def test_ci_exports_the_same_key(self):
        """Otherwise CI and a local run disagree again, in the other direction."""
        if not _WORKFLOW.is_file():
            self.skipTest("no tests workflow in this checkout")
        workflow = _WORKFLOW.read_text(encoding="utf-8")
        exported = set(re.findall(r"AGENT_API_KEY:\s*(\S+)", workflow))
        self.assertTrue(exported, "the workflow no longer exports AGENT_API_KEY")
        self.assertEqual(
            exported,
            {EXPECTED_API_KEY},
            "CI exports a different key than the test modules send",
        )


class IsolationTests(unittest.TestCase):
    """conftest's other guarantees, which are about not touching real state."""

    def test_logs_go_somewhere_temporary(self):
        log_dir = os.environ.get("AGENT_LOG_DIR", "")
        self.assertTrue(log_dir, "AGENT_LOG_DIR is unset; the suite may write to /agent")
        self.assertNotIn("/agent/state", log_dir)

    def test_the_mailbox_path_is_not_the_real_checkout(self):
        mailbox = os.environ.get("AGENT_MAILBOX_REPO_PATH", "")
        self.assertTrue(mailbox, "AGENT_MAILBOX_REPO_PATH is unset")
        self.assertNotEqual(Path(mailbox), Path("/agent"))


if __name__ == "__main__":
    unittest.main()
