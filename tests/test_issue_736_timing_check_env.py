"""#736: the timing check's pytest child runs in the verification environment."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.timing_check import run_timing_check
from modules.verification_env import VERIFICATION_TEST_API_KEY, verification_test_env

# Dumps the environment variables of interest next to itself, then passes.
ENV_DUMP_TEST = (
    "import json\n"
    "import os\n"
    "from pathlib import Path\n"
    "NAMES = ('AGENT_API_KEY', 'GITHUB_TOKEN', 'AZURE_OPENAI_API_KEY',\n"
    "         'NEXUS_TEST_CLOCK_DRIFT_MS', 'PYTHONUNBUFFERED', 'NEXUS_736_MARKER')\n"
    "def test_dump():\n"
    "    seen = {name: os.environ.get(name) for name in NAMES}\n"
    "    (Path(__file__).parent / 'env.json').write_text(json.dumps(seen))\n"
)

# Fake values only: the test checks the names never reach the child.
PARENT_ENV = {
    "AGENT_API_KEY": "parent-production-key",
    "GITHUB_TOKEN": "fake-github-token",
    "AZURE_OPENAI_API_KEY": "fake-azure-key",
    "NEXUS_TEST_CLOCK_DRIFT_MS": "5000",
    "NEXUS_736_MARKER": "kept",
}


class TimingCheckEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "test_env_dump.py").write_text(ENV_DUMP_TEST, encoding="utf-8")
        for args in (
            ("init", "-q"),
            ("config", "user.email", "test@example.com"),
            ("config", "user.name", "Test"),
            ("add", "."),
            ("commit", "-q", "-m", "base"),
        ):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def _child_env(self, drift_ms: int) -> dict:
        with patch.dict("os.environ", PARENT_ENV):
            result = run_timing_check(self.repo, ["tests/test_env_dump.py"], drift_ms)
        self.assertEqual(result, "ok")
        return json.loads((self.repo / "tests" / "env.json").read_text(encoding="utf-8"))

    def test_child_gets_the_test_api_key(self) -> None:
        self.assertEqual(self._child_env(50)["AGENT_API_KEY"], VERIFICATION_TEST_API_KEY)

    def test_parent_credentials_do_not_reach_the_child(self) -> None:
        seen = self._child_env(50)
        self.assertIsNone(seen["GITHUB_TOKEN"])
        self.assertIsNone(seen["AZURE_OPENAI_API_KEY"])
        self.assertIsNone(seen["NEXUS_736_MARKER"])
        self.assertEqual(seen["PYTHONUNBUFFERED"], "1")

    def test_parent_drift_does_not_reach_a_run_with_drift_0(self) -> None:
        self.assertEqual(self._child_env(0)["NEXUS_TEST_CLOCK_DRIFT_MS"], "0")

    def test_requested_drift_overrides_the_parent_drift(self) -> None:
        self.assertEqual(self._child_env(50)["NEXUS_TEST_CLOCK_DRIFT_MS"], "50")

    def test_nested_verification_env_does_not_inherit_drift(self) -> None:
        # A verification run started inside a timing-check child (as in
        # tests/test_issue_689_*) must run its normal tests without drift.
        with verification_test_env({"NEXUS_TEST_CLOCK_DRIFT_MS": "50", "PATH": "/bin"}) as env:
            self.assertNotIn("NEXUS_TEST_CLOCK_DRIFT_MS", env)
            self.assertEqual(env["PATH"], "/bin")


if __name__ == "__main__":
    unittest.main()
