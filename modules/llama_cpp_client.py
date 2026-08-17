"""Async HTTP client for the llama-cpp-python OpenAI-compatible REST API.

The llama-cpp-python server exposes an OpenAI-compatible ``/v1/chat/completions``
endpoint. This client wraps it with minimal dependencies so the rest of the
codebase can treat it identically to the Azure OpenAI client.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx


_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"


class LlamaCppClientError(Exception):
    """Raised when the llama-cpp server returns an unexpected response."""


class LlamaCppClient:
    """Minimal async client for the llama-cpp-python HTTP server.

    Parameters
    ----------
    base_url:
        Base URL of the llama-cpp-python server (default: ``LLAMA_CPP_BASE_URL``
        env var, falling back to ``http://127.0.0.1:8080/v1``).
    timeout:
        HTTP request timeout in seconds. Inference on CPU can be slow, so the
        default is intentionally high (300 s).
    model:
        Model name sent in the request body. The llama-cpp server ignores this
        but it is useful for logging.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 300.0,
        model: str = "llama-2-13b-chat",
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("LLAMA_CPP_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout
        self.model = model

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat-completion request and return the raw response dict.

        Parameters
        ----------
        messages:
            List of ``{"role": ..., "content": ...}`` dicts.
        temperature:
            Sampling temperature (0.0 = deterministic).
        max_tokens:
            Maximum tokens to generate.
        **kwargs:
            Extra fields forwarded to the request body.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        if resp.status_code != 200:
            raise LlamaCppClientError(
                f"llama-cpp server returned HTTP {resp.status_code}: {resp.text[:500]}"
            )

        return resp.json()  # type: ignore[no-any-return]

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> str:
        """Convenience wrapper that returns the assistant's reply as a string."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        data = await self.chat_completion(
            messages, temperature=temperature, max_tokens=max_tokens
        )

        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError) as exc:
            raise LlamaCppClientError(
                f"Unexpected response structure from llama-cpp server: {exc}"
            ) from exc

    async def is_available(self) -> bool:
        """Return True if the llama-cpp server is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/models")
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    def extract_json(self, text: str) -> dict[str, Any]:
        """Extract the first JSON object from *text*.

        llama-cpp models sometimes add prose around the JSON block; this
        method is tolerant of that.
        """
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            raise LlamaCppClientError(
                f"No JSON object found in model output: {text[:300]!r}"
            )
        raw = text[start:end]
        try:
            return json.loads(raw)  # type: ignore[no-any-return]
        except json.JSONDecodeError as exc:
            raise LlamaCppClientError(
                f"Failed to parse JSON from model output: {exc}\nRaw: {raw[:300]!r}"
            ) from exc
