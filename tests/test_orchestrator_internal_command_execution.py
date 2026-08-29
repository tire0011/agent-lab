from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.mailbox_development import process_command


class InternalOrchestratorCommandExecutionTests(unittest.TestCase):
    def test_internal_command_executes_synchronously_without_deferred_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            result = process_command(
                SimpleNamespace(body="printf 'orchestrator-ok'"),
                repo,
                None,
                0,
            )

        self.assertIsInstance(result, tuple)
        body, idea_id = result
        self.assertIsNone(idea_id)
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("orchestrator-ok", body)


if __name__ == "__main__":
    unittest.main()
