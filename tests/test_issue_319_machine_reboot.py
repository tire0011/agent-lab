"""#319: the controller can reboot the VM via find_tool.

After the Ubuntu update the VM needed a reboot and Nexus had no way to do it.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from modules.nexus_tool_router import NexusToolRouter
from modules.operations_registry import MACHINE_REBOOT_COMMAND, OperationsRegistry, OperationValidationError

SUDOERS_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup-nexus-permissions.sh"


class MachineRebootTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="Running timer as unit: run-u1.timer\n", stderr=""))
        self.registry = OperationsRegistry(
            experiment_service=SimpleNamespace(),
            db_path=str(Path(tmp.name) / "agent.db"),
            repo_root=tmp.name,
            subprocess_runner=self.runner,
            github_work_tracking_factory=lambda **_kwargs: SimpleNamespace(),
        )
        self.addCleanup(self.registry.close)

    def _router(self, *, controller: bool) -> NexusToolRouter:
        return NexusToolRouter(operations_registry=self.registry, schema_completion=None, controller=controller)

    def _add_experiment(self, experiment_id: str, status: str) -> None:
        self.registry._conn.execute(
            "INSERT INTO experiments (id, goal, status, created_at) VALUES (?, ?, ?, ?)",
            (experiment_id, "goal", status, "2026-10-16T00:00:00Z"),
        )
        self.registry._conn.commit()

    def test_find_tool_offers_machine_reboot_to_the_controller(self):
        names = [spec.name for spec in self._router(controller=True).candidate_operations("reboot the machine after the ubuntu update")]
        self.assertIn("machine_reboot", names)

    def test_other_roles_do_not_see_machine_reboot(self):
        names = {spec.name for spec in self._router(controller=False).operation_specs()}
        self.assertNotIn("machine_reboot", names)

    def test_reboot_is_scheduled_through_systemd_run(self):
        self._add_experiment("a" * 32, "succeeded")
        result = self.registry.execute("machine_reboot", {})
        self.assertTrue(result.success, result)
        self.assertEqual(result.operation, "machine_reboot")
        self.assertEqual(
            self.runner.call_args.args[0],
            ["sudo", "-n", "systemd-run", "--collect", "--on-active=60", "systemctl", "reboot"],
        )

    def test_reboot_is_refused_while_an_experiment_is_active(self):
        for status in ("queued", "selecting", "implementing", "verifying"):
            with self.subTest(status=status):
                experiment_id = f"{status:0<32}"[:32]
                self._add_experiment(experiment_id, status)
                with self.assertRaises(OperationValidationError) as ctx:
                    self.registry.execute("machine_reboot", {})
                self.assertIn(experiment_id[:8], str(ctx.exception))
                self.registry._conn.execute("DELETE FROM experiments")
        self.runner.assert_not_called()

    def test_arguments_are_rejected(self):
        with self.assertRaises(OperationValidationError):
            self.registry.execute("machine_reboot", {"delay": 0})
        self.runner.assert_not_called()

    def test_failed_sudo_is_reported(self):
        self.runner.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="sudo: a password is required\n")
        result = self.registry.execute("machine_reboot", {})
        self.assertFalse(result.success)
        self.assertIn("password", result.error_summary)

    def test_sudoers_grants_exactly_the_reboot_command(self):
        script = SUDOERS_SCRIPT.read_text(encoding="utf-8")
        granted = re.findall(r"%s (--collect --on-active=\d+ systemctl reboot)", script)
        self.assertEqual(granted, [" ".join(MACHINE_REBOOT_COMMAND[3:])])


if __name__ == "__main__":
    unittest.main()
