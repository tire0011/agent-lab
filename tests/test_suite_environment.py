"""The suite's own environment, pinned.

`tests/conftest.py` defaulted `AGENT_API_KEY` to `test-api-key` while every
API test module sends `x-api-key: test-key`. CI exports `AGENT_API_KEY:
test-key`, so the `setdefault` never fired there and CI stayed green - while
on a developer machine and on the VM, where nothing exports it, roughly 90
authenticated tests answered 401 instead of 200.

That is the failure mode worth guarding: a suite that passes only under the
one environment that happens to override its own default. These tests fail
wherever the key the modules send and the key the app checks disagree, CI
included.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS = _REPO_ROOT / "tests"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "tests.yml"

# What every API test module sends.
EXPECTED_API_KEY = "test-key"


class ApiKeyAgreementTests(unittest.TestCase):
    def test_the_environment_carries_the_key_the_modules_send(self):
        self.assertEqual(os.environ.get("AGENT_API_KEY"), EXPECTED_API_KEY)

    def test_the_app_checks_that_same_key(self):
        import api

        self.assertEqual(api.API_KEY, EXPECTED_API_KEY)

    def test_no_test_module_sends_a_different_key(self):
        """A module with its own literal would 401 for the same reason."""
        pattern = re.compile(r'os\.environ\.setdefault\(\s*"AGENT_API_KEY"\s*,\s*"([^"]+)"')
        disagreeing = {}
        for path in sorted(_TESTS.glob("*.py")):
            for value in pattern.findall(path.read_text(encoding="utf-8")):
                if value != EXPECTED_API_KEY:
                    disagreeing[path.name] = value
        self.assertEqual(
            disagreeing,
            {},
            "these modules default AGENT_API_KEY to something the API tests do not send",
        )

    def test_ci_exports_the_same_key(self):
        """Otherwise CI and a local run disagree again, in the other direction."""
        if not _WORKFLOW.is_file():
            self.skipTest("no tests workflow in this checkout")
        workflow = _WORKFLOW.read_text(encoding="utf-8")
        exported = set(re.findall(r"AGENT_API_KEY:\s*(\S+)", workflow))
        self.assertTrue(exported, "the workflow no longer exports AGENT_API_KEY")
        self.assertEqual(
            exported,
            {EXPECTED_API_KEY},
            "CI exports a different key than the test modules send",
        )


class RealKeyInEnvironmentTests(unittest.TestCase):
    """The timing check runs pytest with the service environment, real key included."""

    def test_api_tests_pass_with_a_real_key_exported(self):
        import subprocess
        import sys

        env = dict(os.environ, AGENT_API_KEY="the-real-service-key")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "tests/test_suite_environment.py::ApiKeyAgreementTests"],
            cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])


class ProviderCredentialsTests(unittest.TestCase):
    """Tests must never reach a real provider, whatever the service env exports."""

    def test_provider_credentials_are_removed(self):
        import subprocess
        import sys

        env = dict(
            os.environ,
            AZURE_OPENAI_API_KEY="real-looking", AZURE_OPENAI_ENDPOINT="https://x.invalid",
            GEMINI_API_KEY="real-looking", MISTRAL_API_KEY="real-looking",
            CLAUDE_CODE_OAUTH_TOKEN="real-looking", GITHUB_TOKEN="real-looking",
        )
        probe = (
            "import os, sys; sys.path.insert(0, 'tests'); import conftest; "
            "names = ['AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_ENDPOINT', 'GEMINI_API_KEY', "
            "'MISTRAL_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'GITHUB_TOKEN']; "
            "left = [n for n in names if n in os.environ]; "
            "print(left); sys.exit(1 if left or os.environ['AGENT_API_KEY'] != 'test-key' else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-2000:])


class ServicePathsTests(unittest.TestCase):
    """Real service paths exported by the service env must not leak into tests."""

    def test_log_dir_and_mailbox_path_are_temporary_even_when_exported(self):
        import subprocess
        import sys

        env = dict(os.environ, AGENT_LOG_DIR="/real/service/logs",
                   AGENT_MAILBOX_REPO_PATH="/real/service/mailbox")
        probe = (
            "import os, sys; sys.path.insert(0, 'tests'); import conftest; "
            "bad = [n for n in ('AGENT_LOG_DIR', 'AGENT_MAILBOX_REPO_PATH') "
            "if os.environ[n].startswith('/real/service')]; print(bad); sys.exit(1 if bad else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-2000:])


class NexusSwitchesTests(unittest.TestCase):
    def test_service_runtime_switches_do_not_reach_the_tests(self):
        import subprocess
        import sys

        env = dict(os.environ, AGENT_CODING_PROVIDERS="codex,claude", NEXUS_REVERIFY_PARKED="0",
                   NEXUS_TEST_CLOCK_DRIFT_MS="50")
        probe = (
            "import os, sys; sys.path.insert(0, 'tests'); import conftest; "
            "leaked = [n for n in ('AGENT_CODING_PROVIDERS', 'NEXUS_REVERIFY_PARKED') if n in os.environ]; "
            "kept = os.environ.get('NEXUS_TEST_CLOCK_DRIFT_MS') == '50'; "
            "print(leaked, kept); sys.exit(0 if not leaked and kept else 1)"
        )
        result = subprocess.run([sys.executable, "-c", probe], cwd=_REPO_ROOT, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-2000:])


class IsolationTests(unittest.TestCase):
    """conftest's other guarantees, which are about not touching real state."""

    def test_logs_go_somewhere_temporary(self):
        log_dir = os.environ.get("AGENT_LOG_DIR", "")
        self.assertTrue(log_dir, "AGENT_LOG_DIR is unset; the suite may write to /agent")
        self.assertNotIn("/agent/state", log_dir)

    def test_the_mailbox_path_is_not_the_real_checkout(self):
        mailbox = os.environ.get("AGENT_MAILBOX_REPO_PATH", "")
        self.assertTrue(mailbox, "AGENT_MAILBOX_REPO_PATH is unset")
        self.assertNotEqual(Path(mailbox), Path("/agent"))


if __name__ == "__main__":
    unittest.main()
