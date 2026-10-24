"""#332: a timer checks every 2 hours whether the VM needs a reboot.

After a system update Ubuntu writes /var/run/reboot-required; until now someone
had to notice it and ask Nexus to reboot.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import nexus_reboot_check
from modules.operations_registry import MACHINE_REBOOT_COMMAND, OperationsRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"


class RebootCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.marker = self.tmp / "reboot-required"
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="Running timer as unit: run-u1.timer\n", stderr=""))
        self.registry = OperationsRegistry(
            experiment_service=None,
            db_path=str(self.tmp / "agent.db"),
            repo_root=tmp.name,
            subprocess_runner=self.runner,
            github_work_tracking_factory=lambda **_kwargs: SimpleNamespace(),
        )
        self.addCleanup(self.registry.close)

    def _add_experiment(self, experiment_id: str, status: str) -> None:
        self.registry._conn.execute(
            "INSERT INTO experiments (id, goal, status, created_at) VALUES (?, ?, ?, ?)",
            (experiment_id, "goal", status, "2026-10-24T00:00:00Z"),
        )
        self.registry._conn.commit()

    def _check(self) -> str:
        return nexus_reboot_check.check(self.registry, marker_path=str(self.marker))

    def test_marker_and_nothing_running_schedules_the_reboot(self):
        self.marker.write_text("*** System restart required ***\n", encoding="utf-8")
        self._add_experiment("s" * 32, "succeeded")
        self._add_experiment("q" * 32, "queued")

        self.assertEqual(self._check(), "scheduled")

        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0], list(MACHINE_REBOOT_COMMAND))

    def test_no_marker_does_not_reboot(self):
        self.assertEqual(self._check(), "not_required")
        self.runner.assert_not_called()

    def test_active_experiment_does_not_reboot(self):
        self.marker.write_text("", encoding="utf-8")
        for status in ("selecting", "implementing", "verifying"):
            with self.subTest(status=status):
                self._add_experiment(f"{status:0<32}"[:32], status)
                self.assertEqual(self._check(), "busy")
                self.registry._conn.execute("DELETE FROM experiments")
        self.runner.assert_not_called()

    def test_failed_scheduling_is_reported(self):
        self.marker.write_text("", encoding="utf-8")
        self.runner.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="sudo: a password is required\n")
        self.assertEqual(self._check(), "failed")

    def test_main_uses_the_database_and_exit_codes(self):
        db_path = str(self.tmp / "main.db")
        self.assertEqual(nexus_reboot_check.main(["--db-path", db_path, "--marker-path", str(self.marker)]), 0)

    def test_default_marker_is_the_ubuntu_path(self):
        self.assertEqual(nexus_reboot_check.REBOOT_REQUIRED_PATH, "/var/run/reboot-required")


class RebootCheckUnitTests(unittest.TestCase):
    def test_timer_runs_every_two_hours(self):
        timer = (DEPLOY / "nexus-reboot-check.timer").read_text(encoding="utf-8")
        self.assertIn("OnUnitActiveSec=2h", timer)
        self.assertIn("Unit=nexus-reboot-check.service", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_service_runs_the_check_without_a_controller(self):
        service = (DEPLOY / "nexus-reboot-check.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("User=agent", service)
        self.assertIn("ExecStart=/agent/.venv/bin/python /agent/nexus_reboot_check.py", service)
        self.assertIn("Environment=AGENT_DB_PATH=/var/lib/nexus/agent.db", service)

    def test_refresh_installs_and_enables_the_timer(self):
        refresh = (REPO_ROOT / "scripts" / "nexus-refresh.ps1").read_text(encoding="utf-8")
        self.assertIn("setup-reboot-check.sh", refresh)
        setup = (REPO_ROOT / "scripts" / "setup-reboot-check.sh").read_text(encoding="utf-8")
        self.assertIn("systemctl enable --now nexus-reboot-check.timer", setup)
        for unit in ("nexus-reboot-check.service", "nexus-reboot-check.timer"):
            self.assertIn(unit, setup)


if __name__ == "__main__":
    unittest.main()
