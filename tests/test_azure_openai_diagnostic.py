"""Azure OpenAI delegation diagnostics must be useful without leaking secrets (#208)."""

from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules.nexus_delegation import DelegationResult, NexusDelegationService
from scripts.test_azure_openai import diagnostic_lines


class _FakeResponse:
    status_code = 401

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAsyncClient:
    response_text = ""

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    async def post(self, *args, **kwargs):
        del args, kwargs
        return _FakeResponse(self.response_text)


class AzureOpenAiDiagnosticTests(unittest.TestCase):
    def _service(self) -> NexusDelegationService:
        jobs = SimpleNamespace(llama=None)
        return NexusDelegationService(llm_job_service=jobs)

    def test_non_200_keeps_bounded_detail_but_redacts_api_key(self) -> None:
        secret = "super-secret-api-key"
        _FakeAsyncClient.response_text = (
            '{"error":{"message":"invalid credential super-secret-api-key"}}' + "x" * 2000
        )
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
        self.assertIn("invalid credential", result.metadata["error_detail"])
        self.assertIn("[REDACTED]", result.metadata["error_detail"])
        self.assertNotIn(secret, repr(result.metadata))
        self.assertLessEqual(len(result.metadata["error_detail"]), 1000)

    def test_diagnostic_output_never_prints_credential_value(self) -> None:
        secret = "do-not-print-this-key"
        result = DelegationResult(
            "azure_openai",
            False,
            "",
            {
                "error": f"HTTP 401 {secret}",
                "http_status": 401,
                "error_detail": f"Azure echoed {secret}",
            },
        )

        output = "\n".join(
            diagnostic_lines(
                result,
                endpoint="https://example.openai.azure.com",
                deployment="test-deployment",
                credential=secret,
            )
        )

        self.assertNotIn(secret, output)
        self.assertIn("credential_configured=True", output)
        self.assertIn(f"credential_length={len(secret)}", output)
        self.assertIn("http_status=401", output)
        self.assertIn("[REDACTED]", output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
