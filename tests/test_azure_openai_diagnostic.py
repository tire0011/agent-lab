from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from modules.nexus_delegation import NexusDelegationService, _AZURE_ERROR_DETAIL_LIMIT
from scripts.test_azure_openai import _credential_hint


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
