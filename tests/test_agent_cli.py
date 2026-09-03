"""Regression tests for the Nexus agent CLI."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class TestAgentCli(unittest.TestCase):
    def test_version_exits_without_runtime_initialization(self):
        env = os.environ.copy()
        env.pop("AZURE_OPENAI_ENDPOINT", None)
        env.pop("AZURE_OPENAI_API_KEY", None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "agent.py"), "--version"],
                cwd=tmpdir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            created_paths = list(Path(tmpdir).iterdir())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0.2.0\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(created_paths, [])


if __name__ == "__main__":
    unittest.main()
