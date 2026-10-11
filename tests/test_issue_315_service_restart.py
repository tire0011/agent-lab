"""#315: the controller can restart the mailbox and the local model via find_tool.

After the Ubuntu update (2026-10-10) Nexus could not restart a service: find_tool
only knew read-only operations, and the run ended asking for a service name.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.nexus_tool_router import NexusToolRouter
from modules.operations_registry import OperationsRegistry, OperationValidationError


def _router(tmp: str, *, controller: bool) -> NexusToolRouter:
    registry = OperationsRegistry(
        experiment_service=SimpleNamespace(),
        db_path=str(Path(tmp) / "agent.db"),
        repo_root=tmp,
        github_work_tracking_factory=lambda **_kwargs: SimpleNamespace(),
    )
    return NexusToolRouter(
        operations_registry=registry,
        schema_completion=None,
        controller=controller,
    )


class ServiceRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def test_find_tool_offers_service_restart_to_the_controller(self):
        router = _router(self.tmp, controller=True)
        names = [spec.name for spec in router.candidate_operations("restart the llm service")]
        self.assertIn("service_restart", names)

    def test_other_mutating_operations_stay_invisible(self):
        router = _router(self.tmp, controller=True)
        names = {spec.name for spec in router.operation_specs()}
        self.assertNotIn("inbox_refresh", names)

    def test_other_roles_do_not_see_service_restart(self):
        router = _router(self.tmp, controller=False)
        names = {spec.name for spec in router.operation_specs()}
        self.assertNotIn("service_restart", names)

    def test_mailbox_and_llm_are_restarted_through_service_operations(self):
        router = _router(self.tmp, controller=True)
        with patch("modules.service_operations._systemctl", new=AsyncMock(return_value=(0, ""))) as mocked:
            for name, unit in (("mailbox", "nexus-mailbox"), ("llm", "llama-server")):
                result = router.operations_registry.execute("service_restart", {"service": name})
                self.assertTrue(result.success, result)
                self.assertEqual(mocked.await_args.args[0], ["restart", unit])

    def test_api_and_unknown_services_are_rejected(self):
        router = _router(self.tmp, controller=True)
        with patch("modules.service_operations._systemctl", new=AsyncMock(return_value=(0, ""))) as mocked:
            for name in ("api", "agent-api", "ssh", None):
                with self.assertRaises(OperationValidationError):
                    router.operations_registry.execute("service_restart", {"service": name})
            mocked.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
