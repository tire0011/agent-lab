"""Tests for automatic API dependency synchronization (issue #31)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-api-deps.sh"


def _run(coro):
    return asyncio.run(coro)


class TestDependencySyncScript(unittest.TestCase):
    def _run_sync(self, pip_exit_code: int = 0):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_pip = root / "pip"
            requirements = root / "requirements-api.txt"
            args_log = root / "pip-args.txt"
            requirements.write_text("example-package==1.0\n", encoding="utf-8")
            fake_pip.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$SYNC_ARGS_LOG\"\nexit \"${SYNC_EXIT_CODE:-0}\"\n",
                encoding="utf-8",
            )
            fake_pip.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "AGENT_REPO_ROOT": str(root),
                    "AGENT_VENV_PIP": str(fake_pip),
                    "AGENT_REQUIREMENTS_FILE": str(requirements),
                    "SYNC_ARGS_LOG": str(args_log),
                    "SYNC_EXIT_CODE": str(pip_exit_code),
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(SYNC_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            args = args_log.read_text(encoding="utf-8").splitlines()
            return result, args, str(requirements)

    def test_runs_project_pip_against_api_requirements(self):
        result, args, requirements = self._run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            args,
            ["install", "--disable-pip-version-check", "-r", requirements],
        )

    def test_propagates_pip_failure(self):
        result, _, _ = self._run_sync(pip_exit_code=23)
        self.assertEqual(result.returncode, 23)
        self.assertNotIn("API dependency sync complete", result.stdout)

    def test_service_syncs_before_uvicorn(self):
        unit = (REPO_ROOT / "deploy" / "agent-api.service").read_text(encoding="utf-8")
        sync_pos = unit.index("ExecStartPre=/bin/bash /agent/scripts/sync-api-deps.sh")
        api_pos = unit.index("ExecStart=/agent/.venv/bin/python -m uvicorn")
        self.assertLess(sync_pos, api_pos)

    def test_refresh_pull_uses_same_sync_helper(self):
        pull_script = (REPO_ROOT / "scripts" / "nexus-pull.ps1").read_text(encoding="utf-8")
        self.assertIn("scripts/sync-api-deps.sh", pull_script)


class TestOperationsGitPullDependencySync(unittest.TestCase):
    def test_successful_pull_runs_dependency_sync(self):
        import modules.git_operations as git_module

        with tempfile.TemporaryDirectory() as tmpdir:
            git = git_module.GitOperations(tmpdir)
            run = AsyncMock(
                side_effect=[
                    (0, "Already up to date.\n", ""),
                    (0, "API dependency sync complete\n", ""),
                ]
            )
            with patch("modules.git_operations._run", new=run):
                result = _run(git.pull())

        self.assertTrue(result["success"])
        self.assertEqual(run.await_count, 2)
        sync_args = run.await_args_list[1].args[0]
        self.assertEqual(sync_args[0], "/bin/bash")
        self.assertTrue(sync_args[1].endswith("scripts/sync-api-deps.sh"))

    def test_dependency_sync_failure_fails_operation(self):
        import modules.git_operations as git_module

        with tempfile.TemporaryDirectory() as tmpdir:
            git = git_module.GitOperations(tmpdir)
            run = AsyncMock(
                side_effect=[
                    (0, "Already up to date.\n", ""),
                    (17, "", "pip install failed"),
                ]
            )
            with patch("modules.git_operations._run", new=run):
                result = _run(git.pull())

        self.assertFalse(result["success"])
        self.assertIn("dependency synchronization failed", result["message"])


if __name__ == "__main__":
    unittest.main()
