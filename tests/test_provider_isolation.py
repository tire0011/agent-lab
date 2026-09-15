"""The suite must not be able to reach a real model provider (#158, #193).

The controller escalates to `codex_readonly` when a prompt exceeds the local
budget. On the VM the codex binary exists, so a test whose injected client was
meant to answer instead spent a real Codex call and failed on the different
answer that came back. Whether that happened depended on how much memory the
host had free, so the failure set varied between runs - 16 locally, 20 in
another run, 4 on a CI runner with no codex binary at all.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path


class ProviderIsolationTests(unittest.TestCase):
    def test_codex_binary_is_not_reachable(self):
        configured = os.environ.get("AGENT_CODEX_BIN")

        self.assertTrue(configured, "AGENT_CODEX_BIN must be set for the suite")
        self.assertFalse(
            Path(configured).exists(),
            f"tests can launch a real codex binary at {configured}",
        )

    def test_claude_binary_is_not_reachable(self):
        configured = os.environ.get("AGENT_CLAUDE_BIN")

        self.assertTrue(configured, "AGENT_CLAUDE_BIN must be set for the suite")
        self.assertFalse(
            Path(configured).exists(),
            f"tests can launch a real claude binary at {configured}",
        )

    def test_no_coding_provider_is_installed_for_the_suite(self):
        # The random provider draw must find nothing runnable, so no test can
        # spend a real call on either agent just because the draw went that way.
        from modules.coding_provider import available_providers

        self.assertEqual(available_providers(), [])

    def test_the_real_deployment_path_is_not_in_use(self):
        # The module default is what the running service actually uses, so
        # this is the path a test would reach if the isolation were skipped
        # or weakened back to a setdefault.
        self.assertNotEqual(
            os.environ.get("AGENT_CODEX_BIN"), "/home/agent/.local/bin/codex"
        )
        self.assertNotEqual(
            os.environ.get("AGENT_CLAUDE_BIN"), "/home/agent/.local/bin/claude"
        )

    def test_logs_do_not_go_to_the_production_directory(self):
        from modules.central_logging import DEFAULT_LOG_DIR

        self.assertNotEqual(DEFAULT_LOG_DIR, "state/experiment-logs")


if __name__ == "__main__":
    unittest.main()
