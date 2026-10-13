"""#315: the controller can restart the mailbox and the local model via find_tool.

After the Ubuntu update (2026-10-10) Nexus could not restart a service: find_tool
only knew read-only operations, and the run ended asking for a service name.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from modules.nexus_tool_router import NexusToolRouter
from modules.operations_registry import _RESTARTABLE_SERVICES, OperationsRegistry, OperationValidationError
from modules.service_operations import _systemctl

SUDOERS_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup-nexus-permissions.sh"


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
        ready = Mock(success=True)
        with patch("modules.service_operations._systemctl", new=AsyncMock(return_value=(0, ""))) as mocked, patch.object(
            router.operations_registry, "_op_local_model_status", return_value=ready
        ):
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


class RestartPrivilegeTests(unittest.TestCase):
    """Review on #316: the sudo rules must cover exactly the restarts it runs."""

    def test_restart_runs_through_sudo(self):
        process = AsyncMock()
        process.communicate.return_value = (b"", b"")
        process.returncode = 0
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            asyncio.run(_systemctl(["restart", "llama-server"]))
            asyncio.run(_systemctl(["is-active", "llama-server"]))
        self.assertEqual(spawn.await_args_list[0].args, ("sudo", "-n", "systemctl", "restart", "llama-server"))
        self.assertEqual(spawn.await_args_list[1].args, ("systemctl", "is-active", "llama-server"))

    def test_sudoers_grants_exactly_the_allowed_restarts(self):
        script = SUDOERS_SCRIPT.read_text(encoding="utf-8")
        granted = set(re.findall(r"%s restart ([\w-]+)", script))
        self.assertEqual(granted, set(_RESTARTABLE_SERVICES))


if __name__ == "__main__":
    unittest.main()
