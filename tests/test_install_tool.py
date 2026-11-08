"""Nexus installs a missing Ubuntu tool itself, behind a denylist (#589)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from modules.nexus_tool_router import NexusToolRouter
from modules.operations_registry import INSTALL_TOOL_COMMAND, OperationsRegistry, OperationValidationError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "nexus-install-tool.sh"


class InstallToolOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="installed: ripgrep\n", stderr=""))
        self.registry = OperationsRegistry(
            experiment_service=SimpleNamespace(),
            db_path=str(Path(tmp.name) / "agent.db"),
            repo_root=tmp.name,
            subprocess_runner=self.runner,
            github_work_tracking_factory=lambda **_kwargs: SimpleNamespace(),
        )
        self.addCleanup(self.registry.close)

    def test_the_controller_finds_it_for_a_missing_command(self):
        router = NexusToolRouter(operations_registry=self.registry, schema_completion=None, controller=True)
        names = [spec.name for spec in router.candidate_operations("install missing tool rg command not found")]
        self.assertIn("install_tool", names)

    def test_other_roles_do_not_see_it(self):
        router = NexusToolRouter(operations_registry=self.registry, schema_completion=None, controller=False)
        self.assertNotIn("install_tool", {spec.name for spec in router.operation_specs()})

    def test_it_runs_the_root_script_with_the_package(self):
        result = self.registry.execute("install_tool", {"package": "ripgrep"})
        self.assertTrue(result.success, result)
        self.assertEqual(self.runner.call_args.args[0], [*INSTALL_TOOL_COMMAND, "ripgrep"])

    def test_options_and_shell_text_are_rejected(self):
        for package in ("", "-y", "rg; reboot", "a b", "../x"):
            with self.assertRaises(OperationValidationError):
                self.registry.execute("install_tool", {"package": package})
        self.runner.assert_not_called()

    def test_sudoers_grants_the_script(self):
        text = (ROOT / "scripts" / "setup-nexus-permissions.sh").read_text()
        self.assertIn('NOPASSWD: %s\\n\' "$SERVICE_USER" "$INSTALL_TOOL"', text)


class InstallToolScriptTests(unittest.TestCase):
    """The script against a fake apt-get that installs ``plan`` packages."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.denylist = self.dir / "denylist"
        self.denylist.write_text((ROOT / "deploy" / "tool-denylist").read_text())
        self.log = self.dir / "apt.log"
        fake = self.dir / "apt-get"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$*" >> {self.log}\n'
            'if [[ "$1" == -s ]]; then for p in $FAKE_PLAN; do echo "Inst $p (1.0 Ubuntu)"; done; fi\n'
        )
        fake.chmod(0o755)
        (self.dir / "logger").write_text("#!/bin/sh\n")
        (self.dir / "logger").chmod(0o755)

    def _run(self, *args: str, plan: str = "", denylist: Path | None = None) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.dir}:{os.environ['PATH']}",
            "FAKE_PLAN": plan,
            "NEXUS_TOOL_DENYLIST": str(denylist or self.denylist),
        }
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)

    def _installed(self) -> bool:
        return self.log.exists() and any(line.startswith("install") for line in self.log.read_text().splitlines())

    def test_an_allowed_package_is_installed(self):
        result = self._run("ripgrep", plan="ripgrep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._installed())

    def test_a_denied_package_is_refused(self):
        result = self._run("openssh-server", plan="openssh-server")
        self.assertEqual(result.returncode, 3)
        self.assertFalse(self._installed())

    def test_a_denied_dependency_is_refused(self):
        result = self._run("harmless", plan="harmless docker.io")
        self.assertEqual(result.returncode, 3)
        self.assertIn("docker.io", result.stderr)
        self.assertFalse(self._installed())

    def test_no_denylist_means_no_install(self):
        result = self._run("ripgrep", plan="ripgrep", denylist=self.dir / "missing")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self._installed())

    def test_options_or_several_packages_are_refused(self):
        for args in (("-o", "x"), ("--allow-unauthenticated",), ("a", "b"), ()):
            self.assertEqual(self._run(*args).returncode, 2, args)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
