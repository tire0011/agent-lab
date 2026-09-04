import subprocess
import sys
import unittest
from pathlib import Path

from nexus_version import VERSION


REPO_ROOT = Path(__file__).resolve().parents[1]


class AgentCliTests(unittest.TestCase):
    def test_version_exits_without_loading_runtime_dependencies(self):
        result = subprocess.run(
            [sys.executable, "agent.py", "--version"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.stdout.strip(), VERSION)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
