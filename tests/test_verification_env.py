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
    SAFE_RUNTIME_PASSTHROUGH,
    VERIFICATION_TEST_API_KEY,
    build_verification_test_env,
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


class VerificationTestApiKeyTests(unittest.TestCase):
    def test_agent_api_key_forced_to_test_key_regardless_of_parent(self):
        """Regression test: even with a fake *production* AGENT_API_KEY set
        in the parent process environment, the built verification test
        environment must always contain the fixed test key."""
        parent_env = {"AGENT_API_KEY": "fake-production-secret-key-12345", "PATH": "/usr/bin"}
        built = build_verification_test_env(parent_env)
        self.assertEqual(built["AGENT_API_KEY"], "test-key")
        self.assertEqual(built["AGENT_API_KEY"], VERIFICATION_TEST_API_KEY)
        self.assertNotIn("fake-production-secret-key-12345", built.values())

    def test_agent_api_key_set_even_when_parent_lacks_it(self):
        built = build_verification_test_env({"PATH": "/usr/bin"})
        self.assertEqual(built["AGENT_API_KEY"], "test-key")

    def test_default_base_env_is_real_os_environ(self):
        # Calling with no argument must not raise and must still force the
        # test key (uses the real process environment as the source).
        built = build_verification_test_env()
        self.assertEqual(built["AGENT_API_KEY"], "test-key")


class VerificationEnvSecretExclusionTests(unittest.TestCase):
    def test_representative_secrets_are_absent(self):
        """None of these secret-shaped variables, even if present in the
        parent process, may appear in the built verification environment
        (AGENT_API_KEY is present but only ever with the forced test
        value, never the parent's value)."""
        parent_env = dict(_REPRESENTATIVE_SECRETS)
        parent_env["PATH"] = "/usr/bin"
        parent_env["HOME"] = "/home/nexus"
        built = build_verification_test_env(parent_env)

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

    def test_only_allowlisted_keys_or_agent_api_key_are_present(self):
        parent_env = dict(_REPRESENTATIVE_SECRETS)
        parent_env.update({"PATH": "/usr/bin", "HOME": "/home/nexus", "RANDOM_UNKNOWN_VAR": "whatever"})
        built = build_verification_test_env(parent_env)
        allowed = SAFE_RUNTIME_PASSTHROUGH | {"AGENT_API_KEY"}
        for key in built:
            self.assertIn(key, allowed, msg=f"unexpected key {key} leaked into verification env")

    def test_empty_parent_env_yields_only_agent_api_key(self):
        built = build_verification_test_env({})
        self.assertEqual(built, {"AGENT_API_KEY": "test-key"})


class VerificationEnvSafeRuntimeTests(unittest.TestCase):
    def test_safe_runtime_variables_pass_through_when_present(self):
        parent_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/nexus",
            "LANG": "en_US.UTF-8",
            "USER": "nexus",
        }
        built = build_verification_test_env(parent_env)
        self.assertEqual(built["PATH"], "/usr/bin:/bin")
        self.assertEqual(built["HOME"], "/home/nexus")
        self.assertEqual(built["LANG"], "en_US.UTF-8")
        self.assertEqual(built["USER"], "nexus")

    def test_missing_safe_runtime_variables_are_simply_omitted(self):
        # Building must not raise or synthesize values for allowlisted
        # variables that simply are not set in the parent environment.
        built = build_verification_test_env({"HOME": "/home/nexus"})
        self.assertIn("HOME", built)
        self.assertNotIn("PATH", built)

    def test_starts_from_empty_not_full_parent_copy(self):
        # A large parent env with many unrelated variables must not leak
        # anything beyond the allowlist + forced AGENT_API_KEY.
        parent_env = {f"UNRELATED_VAR_{i}": str(i) for i in range(50)}
        parent_env["PATH"] = "/usr/bin"
        built = build_verification_test_env(parent_env)
        self.assertEqual(set(built.keys()), {"PATH", "AGENT_API_KEY"})


class VerificationTestEnvContextTests(unittest.TestCase):
    def test_sets_database_path_and_preserves_environment_rules(self):
        parent_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/nexus",
            "AGENT_API_KEY": "production-secret",
            "AGENT_DB_PATH": "/production/agent.db",
            "OPENAI_API_KEY": "provider-secret",
        }

        with verification_test_env(parent_env) as env:
            db_path = Path(env["AGENT_DB_PATH"])
            self.assertTrue(db_path.parent.is_dir())
            self.assertEqual(db_path.name, "agent.db")
            self.assertEqual(env["AGENT_API_KEY"], VERIFICATION_TEST_API_KEY)
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["HOME"], "/home/nexus")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotEqual(env["AGENT_DB_PATH"], "/production/agent.db")
            self.assertEqual(
                set(env),
                {"PATH", "HOME", "AGENT_API_KEY", "AGENT_DB_PATH"},
            )

    def test_separate_contexts_use_different_database_paths(self):
        with verification_test_env({}) as first_env:
            first_path = Path(first_env["AGENT_DB_PATH"])
        with verification_test_env({}) as second_env:
            second_path = Path(second_env["AGENT_DB_PATH"])

        self.assertNotEqual(first_path, second_path)
        self.assertNotEqual(first_path.parent, second_path.parent)

    def test_removes_temporary_directory_after_normal_exit(self):
        with verification_test_env({}) as env:
            temp_dir = Path(env["AGENT_DB_PATH"]).parent
            self.assertTrue(temp_dir.is_dir())

        self.assertFalse(temp_dir.exists())

    def test_removes_temporary_directory_after_exception(self):
        temp_dir = None
        with self.assertRaisesRegex(RuntimeError, "expected failure"):
            with verification_test_env({}) as env:
                temp_dir = Path(env["AGENT_DB_PATH"]).parent
                self.assertTrue(temp_dir.is_dir())
                raise RuntimeError("expected failure")

        self.assertIsNotNone(temp_dir)
        self.assertFalse(temp_dir.exists())


if __name__ == "__main__":
    unittest.main()
