"""Tests for modules/llama_cpp_client.py."""

import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from modules.llama_cpp_client import (
    LlamaCppClient,
    LlamaCppClientError,
    _DEFAULT_BASE_URL,
)


def make_client(base_url: str = "http://localhost:8080/v1") -> LlamaCppClient:
    return LlamaCppClient(base_url=base_url, model="test-model")


# ── Default configuration ─────────────────────────────────────────────────────

def test_default_base_url(monkeypatch):
    monkeypatch.delenv("LOCAL_MODEL_URL")
    monkeypatch.delenv("LLAMA_CPP_BASE_URL")
    client = LlamaCppClient()
    assert client.base_url == _DEFAULT_BASE_URL.rstrip("/")


def test_default_model_name_is_qwen():
    """Default model should be Qwen2.5-Coder-7B-Instruct, not Llama 2."""
    client = LlamaCppClient()
    assert "qwen" in client.model.lower()
    assert "llama" not in client.model.lower()
    assert "2-13b" not in client.model.lower()


def test_local_model_url_env_var(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_URL", "http://127.0.0.1:9090/v1")
    client = LlamaCppClient()
    assert client.base_url == "http://127.0.0.1:9090/v1"


def test_local_model_name_env_var(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_NAME", "my-custom-model")
    client = LlamaCppClient()
    assert client.model == "my-custom-model"


def test_legacy_llama_cpp_base_url_still_accepted(monkeypatch):
    """LLAMA_CPP_BASE_URL is a backward-compat alias for LOCAL_MODEL_URL."""
    monkeypatch.delenv("LOCAL_MODEL_URL", raising=False)
    monkeypatch.setenv("LLAMA_CPP_BASE_URL", "http://127.0.0.1:7777/v1")
    client = LlamaCppClient()
    assert client.base_url == "http://127.0.0.1:7777/v1"


def test_legacy_llama_cpp_model_still_accepted(monkeypatch):
    """LLAMA_CPP_MODEL is a backward-compat alias for LOCAL_MODEL_NAME."""
    monkeypatch.delenv("LOCAL_MODEL_NAME", raising=False)
    monkeypatch.setenv("LLAMA_CPP_MODEL", "legacy-model")
    client = LlamaCppClient()
    assert client.model == "legacy-model"


def test_local_model_url_takes_priority_over_legacy(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_URL", "http://primary/v1")
    monkeypatch.setenv("LLAMA_CPP_BASE_URL", "http://legacy/v1")
    client = LlamaCppClient()
    assert client.base_url == "http://primary/v1"


def test_local_model_name_takes_priority_over_legacy(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_NAME", "primary-model")
    monkeypatch.setenv("LLAMA_CPP_MODEL", "legacy-model")
    client = LlamaCppClient()
    assert client.model == "primary-model"


def test_no_llama2_in_default_url():
    """Default URL must not embed Llama 2 assumptions."""
    assert "llama-2" not in _DEFAULT_BASE_URL.lower()
    assert "13b" not in _DEFAULT_BASE_URL.lower()


# ── extract_json ──────────────────────────────────────────────────────────────

def test_extract_json_plain():
    client = make_client()
    result = client.extract_json('{"decision": "SELF"}')
    assert result == {"decision": "SELF"}


def test_extract_json_with_prose():
    client = make_client()
    text = 'Here is my answer:\n{"decision": "AZURE_OPENAI", "confidence": 0.9}\nEnd.'
    result = client.extract_json(text)
    assert result["decision"] == "AZURE_OPENAI"


def test_extract_json_no_json_raises():
    client = make_client()
    with pytest.raises(LlamaCppClientError, match="No JSON object"):
        client.extract_json("No JSON here at all.")


def test_extract_json_invalid_json_raises():
    client = make_client()
    with pytest.raises(LlamaCppClientError, match="Failed to parse JSON"):
        client.extract_json("{bad json}")


# ── chat_completion ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_completion_success():
    response_data = {
        "choices": [{"message": {"content": "Hello!"}}]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = response_data

    with patch("modules.llama_cpp_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        client = make_client()
        result = await client.chat_completion([{"role": "user", "content": "Hi"}])

    assert result == response_data


@pytest.mark.asyncio
async def test_chat_completion_http_error_raises():
    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.text = "Service Unavailable"

    with patch("modules.llama_cpp_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        client = make_client()
        with pytest.raises(LlamaCppClientError, match="HTTP 503"):
            await client.chat_completion([{"role": "user", "content": "Hi"}])


@pytest.mark.asyncio
async def test_complete_returns_string():
    response_data = {
        "choices": [{"message": {"content": "Answer text"}}]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = response_data

    with patch("modules.llama_cpp_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        client = make_client()
        result = await client.complete("Tell me something")

    assert result == "Answer text"



@pytest.mark.asyncio
async def test_complete_forwards_structured_response_format():
    response_data = {"choices": [{"message": {"content": "{\"type\":\"answer\"}"}}]}
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = response_data
    response_format = {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {"type": {"type": "string"}},
            "required": ["type"],
        },
    }

    with patch("modules.llama_cpp_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await make_client().complete(
            "Return a decision",
            response_format=response_format,
        )

    assert result == '{"type":"answer"}'
    payload = mock_client.post.await_args.kwargs["json"]
    assert payload["response_format"] == response_format


# ── is_available ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_is_available_true():
    mock_response = MagicMock()
    mock_response.status_code = 200

    with patch("modules.llama_cpp_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        client = make_client()
        assert await client.is_available() is True


@pytest.mark.asyncio
async def test_is_available_false_on_connection_error():
    import httpx

    with patch("modules.llama_cpp_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        mock_client_cls.return_value = mock_client

        client = make_client()
        assert await client.is_available() is False
