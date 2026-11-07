"""Tests for the read-only VM maintenance and resource report (#350)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.operations_registry import OperationValidationError, OperationsRegistry


class VmMaintenanceReportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.outputs = {
            ("apt", "list", "--upgradable"): "Listing... Done\nopenssl/jammy-security 3.0 amd64 [upgradable]\ncurl/jammy-updates 8.0 amd64 [upgradable]\n",
            ("systemctl", "--failed", "--no-legend", "--plain", "--no-pager"): "broken.service loaded failed failed Broken service\n",
            ("systemctl", "--failed", "--type=timer", "--no-legend", "--plain", "--no-pager"): "stale.timer loaded failed failed Stale timer\n",
            ("df", "-h", "--output=source,size,used,avail,pcent,target", "/"): "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 29G 12G 17G 42% /\n",
            ("free", "-h"): "Mem: 969Mi 700Mi 269Mi\nSwap: 1.0Gi 64Mi 960Mi\n",
            ("cat", "/proc/pressure/memory"): "some avg10=0.10 avg60=0.05 avg300=0.01 total=12\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
            ("ps", "-eo", "pid,pmem,rss,comm", "--sort=-rss"): "PID %MEM RSS COMMAND\n10 20.0 200000 python\n11 10.0 100000 nginx\n",
            ("find", "-L", "/etc/letsencrypt/live", "-name", "fullchain.pem", "-exec", "openssl", "x509", "-in", "{}", "-noout", "-subject", "-enddate", ";"): "subject=CN = nexus.example\nnotAfter=Dec 31 23:59:59 2026 GMT\n",
        }
        self.commands: list[tuple[str, ...]] = []

        def runner(command, **_kwargs):
            key = tuple(command)
            self.commands.append(key)
            return subprocess.CompletedProcess(command, 0, stdout=self.outputs[key], stderr="")

        self.runner = runner
        self.registry = OperationsRegistry(
            experiment_service=None,
            db_path=str(self.tmp / "agent.db"),
            repo_root=str(self.tmp),
            subprocess_runner=runner,
            github_work_tracking_factory=lambda **_kwargs: SimpleNamespace(),
        )
        self.addCleanup(self.registry.close)

    def test_fake_command_outputs_produce_expected_report(self):
        result = self.registry.execute("vm_maintenance_report")

        self.assertTrue(result.success)
        report = "\n".join(result.output_lines)
        for expected in (
            "updates: 2 upgradable; 1 security",
            "openssl, curl",
            "failed units: 1",
            "broken.service",
            "failed timers: 1",
            "stale.timer",
            "[PATH] 29G 12G 17G 42% /",
            "Swap: 1.0Gi 64Mi 960Mi",
            "some avg10=0.10",
            "10 20.0 200000 python",
            "notAfter=Dec 31 23:59:59 2026 GMT",
        ):
            self.assertIn(expected, report)

        forbidden = {"upgrade", "remove", "install", "restart", "stop", "start", "enable", "disable"}
        self.assertFalse(forbidden.intersection(word for command in self.commands for word in command))
        spec = next(spec for spec in self.registry.catalog() if spec.name == "vm_maintenance_report")
        self.assertFalse(spec.mutates_state)

    def test_failed_command_is_unknown_and_does_not_fail_report(self):
        def failing_runner(command, **kwargs):
            if command[0] == "free":
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="unavailable")
            return self.runner(command, **kwargs)

        self.registry.subprocess_runner = failing_runner
        result = self.registry.execute("vm_maintenance_report")

        self.assertTrue(result.success)
        self.assertIn("memory/swap: unknown", result.output_lines)
        self.assertIsNone(result.error_summary)

    def test_arguments_are_rejected(self):
        with self.assertRaises(OperationValidationError):
            self.registry.execute("vm_maintenance_report", {"upgrade": True})


if __name__ == "__main__":
    unittest.main()
