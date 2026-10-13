"""#317: restarting the local LLM waits for its HTTP endpoint to recover."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from modules.operations_registry import OperationsRegistry


class ServiceRestartReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = OperationsRegistry(
            experiment_service=SimpleNamespace(),
            db_path=str(Path(tmp.name) / "agent.db"),
            repo_root=tmp.name,
            github_work_tracking_factory=lambda **_kwargs: SimpleNamespace(),
        )
        self.addCleanup(self.registry.close)

    @patch("modules.service_operations._systemctl", new_callable=AsyncMock, return_value=(0, ""))
    def test_llm_restart_waits_until_local_model_status_succeeds(self, _systemctl):
        unavailable = Mock(success=False)
        ready = Mock(success=True)
        with patch.object(
            self.registry,
            "_op_local_model_status",
            side_effect=[unavailable, ready],
        ) as status, patch("modules.operations_registry.time.sleep") as sleep:
            result = self.registry.execute("service_restart", {"service": "llm"})

        self.assertTrue(result.success, result)
        self.assertIn("local model: ready", result.output_lines)
        self.assertEqual(status.call_args_list, [call({}), call({})])
        sleep.assert_called_once_with(2.0)

    @patch("modules.service_operations._systemctl", new_callable=AsyncMock, return_value=(0, ""))
    def test_llm_restart_fails_when_model_never_becomes_ready(self, _systemctl):
        unavailable = Mock(success=False)
        with patch.object(
            self.registry,
            "_op_local_model_status",
            return_value=unavailable,
        ) as status, patch(
            "modules.operations_registry._LOCAL_MODEL_RESTART_TIMEOUT_SECONDS", -1.0
        ), patch("modules.operations_registry.time.sleep") as sleep:
            result = self.registry.execute("service_restart", {"service": "llm"})

        self.assertFalse(result.success, result)
        self.assertIn("did not come back", result.error_summary)
        status.assert_called_once_with({})
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
