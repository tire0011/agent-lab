from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from modules.nexus_delegation import NexusDelegationService, _AZURE_ERROR_DETAIL_LIMIT
from scripts.test_azure_openai import _credential_hint, _load_agent_env, _parse_env_lines


class _FakeAsyncClient:
    response: httpx.Response

    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb

    async def post(self, *args, **kwargs):
        del args, kwargs
        return self.response


class AzureOpenAiDiagnosticTests(unittest.TestCase):
    def _service(self) -> NexusDelegationService:
        jobs = SimpleNamespace(llama=None)
        service = NexusDelegationService(llm_job_service=jobs)
        self.addCleanup(service.close)
        return service

    def test_non_200_keeps_bounded_detail_without_secret(self) -> None:
        secret = "super-secret-azure-key-12345"
        long_detail = f'{{"error":{{"message":"bad key {secret} "}}}}' + ("x" * 3000)
        _FakeAsyncClient.response = httpx.Response(401, text=long_detail)

        env = {
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_API_KEY": secret,
            "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "modules.nexus_delegation.httpx.AsyncClient", _FakeAsyncClient
        ):
            result = asyncio.run(self._service().delegate("azure_openai", "diagnose"))

        self.assertFalse(result.success)
        self.assertEqual(result.metadata["http_status"], 401)
        self.assertEqual(result.metadata["error"], "HTTP 401")
        self.assertLessEqual(len(result.metadata["error_detail"]), _AZURE_ERROR_DETAIL_LIMIT)
        self.assertNotIn(secret, str(result.metadata))
        self.assertIn("[REDACTED]", result.metadata["error_detail"])

    def test_credential_hint_never_contains_credential_value(self) -> None:
        secret = "another-super-secret-key"
        with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": secret}, clear=False):
            hint = _credential_hint()

        self.assertIn("configured", hint)
        self.assertIn(f"length={len(secret)}", hint)
        self.assertNotIn(secret, hint)


class LoadAgentEnvTests(unittest.TestCase):
    def test_parse_env_lines_skips_comments_and_blanks(self) -> None:
        text = (
            "\n"
            "# comment\n"
            'AZURE_OPENAI_ENDPOINT="https://example.openai.azure.com"\n'
            "AZURE_OPENAI_API_KEY=some-key\n"
            "MALFORMED_LINE\n"
        )

        parsed = _parse_env_lines(text)

        self.assertEqual(parsed["AZURE_OPENAI_ENDPOINT"], "https://example.openai.azure.com")
        self.assertEqual(parsed["AZURE_OPENAI_API_KEY"], "some-key")
        self.assertNotIn("MALFORMED_LINE", parsed)
        self.assertNotIn("# comment", parsed)

    def test_load_agent_env_fills_missing_values_from_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "agent.env"
            env_path.write_text("AZURE_OPENAI_ENDPOINT=https://from-file.openai.azure.com\n")

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AZURE_OPENAI_ENDPOINT", None)
                _load_agent_env(path=env_path)
                self.assertEqual(
                    os.environ.get("AZURE_OPENAI_ENDPOINT"),
                    "https://from-file.openai.azure.com",
                )

    def test_load_agent_env_does_not_override_existing_env(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "agent.env"
            env_path.write_text("AZURE_OPENAI_ENDPOINT=https://from-file.openai.azure.com\n")

            with patch.dict(
                os.environ, {"AZURE_OPENAI_ENDPOINT": "https://explicit.example.com"}, clear=False
            ):
                _load_agent_env(path=env_path)
                self.assertEqual(
                    os.environ.get("AZURE_OPENAI_ENDPOINT"), "https://explicit.example.com"
                )

    def test_load_agent_env_missing_file_is_a_noop(self) -> None:
        missing = Path("/nonexistent/does-not-exist.env")
        with patch.dict(os.environ, {}, clear=False):
            _load_agent_env(path=missing)  # should not raise


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
