"""Wrapper for OpenAI-compatible local model REST APIs.

The llama-server (llama.cpp) exposes an OpenAI-compatible
``/v1/chat/completions`` endpoint.  This client wraps it with minimal
dependencies so the rest of the codebase can treat it identically to the
Azure OpenAI client.

Configuration is driven entirely by environment variables so the local model
can be swapped (different GGUF, different quantisation, different server) without
changing any orchestration code:

* ``LOCAL_MODEL_URL``  — server base URL (default: ``http://127.0.0.1:8080/v1``)
* ``LOCAL_MODEL_NAME`` — model name sent in requests, for logging
                         (default: ``qwen2.5-coder-7b-instruct``)

Deprecated aliases (still accepted for backward compatibility):
* ``LLAMA_CPP_BASE_URL`` — maps to ``LOCAL_MODEL_URL``
* ``LLAMA_CPP_MODEL``    — maps to ``LOCAL_MODEL_NAME``
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx


_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
_DEFAULT_MODEL_NAME = "qwen2.5-coder-7b-instruct"


def json_schema_response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The wire shape a JSON schema has to be sent in to actually be applied.

    llama-server reads the schema from ``response_format.json_schema.schema``
    and Azure OpenAI expects the same OpenAI-standard envelope. Nexus used to
    send ``{"type": "json_schema", "schema": {...}}`` - the schema one level
    too high - which neither server finds, so every schema was silently
    ignored and every declared enum was a suggestion. That is how a tool
    resolver constrained to four ``status`` values answered with a fifth,
    fenced in Markdown, and had its otherwise correct selection thrown away
    (#218).

    Silently is the operative word: an ignored schema looks exactly like a
    model that happens to comply, right up until it does not.
    """
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": schema, "strict": True},
    }


class LlamaCppClientError(Exception):
    """Raised when the llama-cpp server returns an unexpected response."""


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from *text*.

    llama-cpp models sometimes add prose around the JSON block; this
    function is tolerant of that.
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


class LlamaCppClient:
    """Minimal async client for a local OpenAI-compatible model server.

    Works with llama-server (llama.cpp) or any server that exposes the
    ``/v1/chat/completions`` endpoint.

    Parameters
    ----------
    base_url:
        Base URL of the server (default: ``LOCAL_MODEL_URL`` env var,
        falling back to ``http://127.0.0.1:8080/v1``).
        The legacy ``LLAMA_CPP_BASE_URL`` env var is also accepted.
    timeout:
        HTTP request timeout in seconds. Inference on CPU can be slow, so the
        default is intentionally high (300 s).
    model:
        Model name sent in the request body (default: ``LOCAL_MODEL_NAME`` env
        var, falling back to ``qwen2.5-coder-7b-instruct``).  The server
        typically ignores this field but it is useful for logging and tracing.
        The legacy ``LLAMA_CPP_MODEL`` env var is also accepted.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 300.0,
        model: str | None = None,
    ) -> None:
        # Prefer the generic LOCAL_MODEL_URL; fall back to legacy LLAMA_CPP_BASE_URL.
        self.base_url = (
            base_url
            or os.getenv("LOCAL_MODEL_URL")
            or os.getenv("LLAMA_CPP_BASE_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout
        # Prefer the generic LOCAL_MODEL_NAME; fall back to legacy LLAMA_CPP_MODEL.
        self.model = (
            model
            or os.getenv("LOCAL_MODEL_NAME")
            or os.getenv("LLAMA_CPP_MODEL", _DEFAULT_MODEL_NAME)
        )

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
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Convenience wrapper that returns the assistant's reply as a string."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        completion_options: dict[str, Any] = {}
        if response_format is not None:
            completion_options["response_format"] = response_format
        data = await self.chat_completion(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **completion_options,
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

    # Keep as an instance method for backward compatibility; delegates to the
    # module-level function which does not require any client state.
    def extract_json(self, text: str) -> dict[str, Any]:
        """Extract the first JSON object from *text* (delegates to module-level function)."""
        return extract_json(text)
