"""Focused tests for modules.verification_env.build_verification_test_env.

Regression coverage for the bug where the verification test subprocess
inherited the full production ``agent-api`` environment (including the
real ``AGENT_API_KEY``), causing spurious 401 failures in the test suite
which expects ``AGENT_API_KEY=test-key``.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from modules.verification_env import (
    LOCAL_MODEL_URL_ENVS,
    UNREACHABLE_LOCAL_MODEL_URL,
    VERIFICATION_TEST_API_KEY,
    verification_test_env,
)

# Representative secret-shaped environment variables that must never reach
# the verification test subprocess, regardless of what the parent process
# (agent-api) has set.
_REPRESENTATIVE_SECRETS = {
    "AGENT_API_KEY": "prod-super-secret-key-do-not-leak",
    "AZURE_OPENAI_API_KEY": "azure-secret-abcdef",
    "OPENAI_API_KEY": "sk-openai-secret",
    "AZURE_OPENAI_ENDPOINT": "https://prod.openai.azure.com/",
    "GITHUB_TOKEN": "ghp_super_secret_token",
    "GH_TOKEN": "ghp_another_secret_token",
    "COPILOT_TOKEN": "copilot-secret-token",
    "AUTHORIZATION": "Bearer super-secret",
    "CODEX_HOME": "/home/prod/.codex",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "AGENT_MAILBOX_LOCK_PATH": "/prod/lock",
}


class VerificationEnvTestCase(unittest.TestCase):
    def build_env(self, source=None):
        env_context = verification_test_env(source)
        env = env_context.__enter__()
        self.addCleanup(env_context.__exit__, None, None, None)
        return env


class VerificationTestApiKeyTests(VerificationEnvTestCase):
    def test_agent_api_key_forced_to_test_key_regardless_of_parent(self):
        """Regression test: even with a fake *production* AGENT_API_KEY set
        in the parent process environment, the built verification test
        environment must always contain the fixed test key."""
        parent_env = {"AGENT_API_KEY": "fake-production-secret-key-12345", "PATH": "/usr/bin"}
        built = self.build_env(parent_env)
        self.assertEqual(built["AGENT_API_KEY"], "test-key")
        self.assertEqual(built["AGENT_API_KEY"], VERIFICATION_TEST_API_KEY)
        self.assertNotIn("fake-production-secret-key-12345", built.values())

    def test_agent_api_key_set_even_when_parent_lacks_it(self):
        built = self.build_env({"PATH": "/usr/bin"})
        self.assertEqual(built["AGENT_API_KEY"], "test-key")

    def test_default_base_env_is_real_os_environ(self):
        # Calling with no argument must not raise and must still force the
        # test key (uses the real process environment as the source).
        built = self.build_env()
        self.assertEqual(built["AGENT_API_KEY"], "test-key")


class VerificationEnvSecretExclusionTests(VerificationEnvTestCase):
    def test_representative_secrets_are_absent(self):
        """None of these secret-shaped variables, even if present in the
        parent process, may appear in the built verification environment
        (AGENT_API_KEY is present but only ever with the forced test
        value, never the parent's value)."""
        parent_env = dict(_REPRESENTATIVE_SECRETS)
        parent_env["PATH"] = "/usr/bin"
        parent_env["HOME"] = "/home/nexus"
        built = self.build_env(parent_env)

        for key, secret_value in _REPRESENTATIVE_SECRETS.items():
            if key == "AGENT_API_KEY":
                # Present, but must be the forced test value, not the
                # parent's secret value.
                self.assertIn(key, built)
                self.assertNotEqual(built[key], secret_value)
                self.assertEqual(built[key], "test-key")
            else:
                self.assertNotIn(
                    key, built,
                    msg=f"{key} must never be passed through to the verification subprocess",
                )
            self.assertNotIn(secret_value, built.values())

    def test_service_settings_and_startup_overrides_are_excluded(self):
        overrides = {
            "HTTPS_PROXY": "http://proxy.test",
            "PYTHONPATH": "/service/modules",
            "PYTEST_ADDOPTS": "--lf",
            "NEXUS_REVERIFY_PARKED": "1",
            "NEXUS_TEST_CLOCK_DRIFT_MS": "5000",
            "GIT_CONFIG_GLOBAL": "/service/gitconfig",
            "XDG_CONFIG_HOME": "/service/config",
            "RANDOM_UNKNOWN_VAR": "whatever",
        }
        built = self.build_env({**overrides, "PATH": "/usr/bin", "HOME": "/service"})
        self.assertFalse(overrides.keys() & built.keys())
        self.assertEqual(built["PATH"], "/usr/bin")
        self.assertEqual(Path(built["HOME"]).parent, Path(built["TMPDIR"]))
        for name in ("AGENT_LOG_DIR", "AGENT_MAILBOX_REPO_PATH", "AGENT_CODE_REPO_PATH",
                     "AGENT_CODEX_BIN", "AGENT_CLAUDE_BIN", "AGENT_VIBE_BIN",
                     "LOCAL_MODEL_RATES_PATH"):
            self.assertEqual(Path(built[name]).parent, Path(built["TMPDIR"]))


class VerificationEnvDatabaseIsolationTests(VerificationEnvTestCase):
    def test_production_db_path_is_replaced_with_a_temporary_one(self):
        # #766: the tick exports AGENT_DB_PATH pointing at the production
        # database. The verification suite also runs via unittest discovery,
        # where tests/conftest.py does not execute, so the built environment
        # itself must keep the subprocess away from that database - otherwise
        # a default-path service opens and locks the production DB while the
        # API service writes it, and the suite hangs inside the tick.
        parent_env = {"PATH": "/usr/bin", "AGENT_DB_PATH": "/var/lib/nexus/agent.db"}
        built = self.build_env(parent_env)
        self.assertIn("AGENT_DB_PATH", built)
        self.assertNotEqual(built["AGENT_DB_PATH"], "/var/lib/nexus/agent.db")
        self.assertNotIn("/var/lib/nexus/agent.db", built.values())
        self.assertTrue(built["AGENT_DB_PATH"].endswith("agent.db"))

    def test_db_is_private_even_without_a_parent_setting(self):
        first = self.build_env({})
        second = self.build_env({})
        self.assertEqual(Path(first["AGENT_DB_PATH"]).parent, Path(first["TMPDIR"]))
        self.assertNotEqual(first["AGENT_DB_PATH"], second["AGENT_DB_PATH"])
        self.assertEqual(first["NEXUS_DB_DURABILITY"], "relaxed")
        self.assertEqual(first["NEXUS_DB_MIGRATE_LEGACY"], "0")

    def test_default_base_env_keeps_no_production_db_path(self):
        # Whatever the real process environment holds, the built environment
        # must never carry the production database path through.
        built = self.build_env()
        self.assertNotEqual(built.get("AGENT_DB_PATH"), "/var/lib/nexus/agent.db")


class VerificationEnvLocalModelIsolationTests(VerificationEnvTestCase):
    def test_real_local_model_urls_are_replaced(self):
        # #766: under unittest discovery conftest.py does not redirect the
        # local model, so a test's background failure analysis reached the
        # real llama-server and the suite hung inside the tick.
        parent_env = {
            "LOCAL_MODEL_URL": "http://127.0.0.1:8080/v1",
            "LLAMA_CPP_BASE_URL": "http://127.0.0.1:8080/v1",
            "LLAMA_API_URL": "http://127.0.0.1:8080",
        }
        built = self.build_env(parent_env)
        for name in LOCAL_MODEL_URL_ENVS:
            self.assertEqual(built[name], UNREACHABLE_LOCAL_MODEL_URL)

    def test_unset_urls_are_still_assigned(self):
        # The client falls back to 127.0.0.1:8080 when nothing is set, so an
        # absent variable is no isolation.
        built = self.build_env({"PATH": "/usr/bin"})
        self.assertEqual(built["LOCAL_MODEL_URL"], UNREACHABLE_LOCAL_MODEL_URL)

    def test_unittest_child_with_this_env_never_reaches_a_model_server(self):
        # End to end, the way the tick's verifier runs: a unittest child (no
        # conftest) builds a default client and must be refused at once.
        import subprocess
        import sys
        import time

        env = self.build_env()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-c",
             "import asyncio, httpx\n"
             "from modules.llama_cpp_client import LlamaCppClient\n"
             "client = LlamaCppClient()\n"
             "print(client.base_url)\n"
             "try:\n"
             "    asyncio.run(asyncio.wait_for(client.chat_completion("
             "[{'role': 'user', 'content': 'x'}]), 10))\n"
             "except httpx.ConnectError:\n"
             "    print('refused')\n"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [UNREACHABLE_LOCAL_MODEL_URL, "refused"])
        self.assertLess(time.monotonic() - started, 30)


class VerificationEnvSafeRuntimeTests(VerificationEnvTestCase):
    def test_safe_runtime_variables_pass_through_when_present(self):
        runtime = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
        built = self.build_env(runtime)
        self.assertEqual({name: built[name] for name in runtime}, runtime)

    def test_missing_runtime_variables_are_omitted(self):
        self.assertNotIn("PATH", self.build_env({}))


if __name__ == "__main__":
    unittest.main()
