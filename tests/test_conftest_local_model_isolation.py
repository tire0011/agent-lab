"""Regression tests for test-suite isolation from the VM's llama-server."""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest

from modules.llama_cpp_client import LlamaCppClient
from modules.llama_router import LlamaRouter
from modules.local_llm_jobs import LocalLlmJobService
from modules.operations_executor import OperationsExecutor


UNREACHABLE_LOCAL_MODEL_URL = "http://127.0.0.1:9"


def test_conftest_assigns_local_model_urls_unconditionally() -> None:
    """conftest has replaced any values inherited by the pytest process."""
    assert os.environ["LOCAL_MODEL_URL"] == UNREACHABLE_LOCAL_MODEL_URL
    assert os.environ["LLAMA_CPP_BASE_URL"] == UNREACHABLE_LOCAL_MODEL_URL
    assert os.environ["LLAMA_API_URL"] == UNREACHABLE_LOCAL_MODEL_URL
    assert LlamaCppClient().base_url == UNREACHABLE_LOCAL_MODEL_URL


def test_default_client_refuses_connection_within_five_seconds() -> None:
    client = LlamaCppClient()
    started = time.monotonic()

    with pytest.raises(httpx.ConnectError):
        asyncio.run(
            asyncio.wait_for(
                client.chat_completion([{"role": "user", "content": "test"}]),
                timeout=5.0,
            )
        )

    assert time.monotonic() - started < 5.0


def test_all_local_model_callers_use_isolated_url(monkeypatch) -> None:
    router = LlamaRouter()
    jobs = LocalLlmJobService(db_path=":memory:")

    assert router._client.base_url == UNREACHABLE_LOCAL_MODEL_URL
    assert jobs.llama.base_url == UNREACHABLE_LOCAL_MODEL_URL

    requested_urls: list[str] = []

    class FakeResponse:
        status_code = 503

    class FakeAsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            pass

        async def post(self, url: str, **_kwargs):
            requested_urls.append(url)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    asyncio.run(OperationsExecutor(db_path=":memory:")._test_llama("test"))

    assert requested_urls == [UNREACHABLE_LOCAL_MODEL_URL]
