import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.nexus_chat_service import _ChatOrchestrator


class ChatContextTests(unittest.TestCase):
    def _orchestrator(self, allow_mutation=False):
        registry = MagicMock()
        registry.catalog.return_value = []
        registry.db_path = ":memory:"
        registry.repo_root = "/tmp"
        return _ChatOrchestrator(
            operations_registry=registry,
            experiment_service=MagicMock(),
            allow_mutation=allow_mutation,
            llama_client=MagicMock(),
        )

    def test_chat_prompt_is_compact_and_keeps_recent_context(self):
        orchestrator = self._orchestrator(False)
        try:
            history = [
                {"role": "user", "kind": "input", "content": f"old-{i}-" + ("x" * 500)}
                for i in range(12)
            ]
            prompt = orchestrator._prompt(SimpleNamespace(), "Hallo Nexus", history, [])
            self.assertLess(len(prompt), 5000)
            self.assertIn("Hallo Nexus", prompt)
            self.assertIn("old-11-", prompt)
            self.assertNotIn("old-0-", prompt)
            self.assertIn("Discussion only", prompt)
        finally:
            orchestrator.close()

    def test_execution_prompt_keeps_mutating_actions(self):
        orchestrator = self._orchestrator(True)
        try:
            prompt = orchestrator._prompt(SimpleNamespace(), "Mach das", [], [])
            self.assertIn("new_experiment", prompt)
            self.assertIn("resume_experiment", prompt)
            self.assertIn("command", prompt)
            self.assertIn("Execution explicitly requested", prompt)
        finally:
            orchestrator.close()


if __name__ == "__main__":
    unittest.main()
