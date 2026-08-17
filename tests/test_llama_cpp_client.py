"""Tests for modules/llama_cpp_client.py."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError


def make_client(base_url: str = "http://localhost:8080/v1") -> LlamaCppClient:
    return LlamaCppClient(base_url=base_url, model="test-model")


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
