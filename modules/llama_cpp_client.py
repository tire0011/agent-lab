"""Wrapper for OpenAI-compatible local model REST APIs (llama.cpp / llama-server),
so the local model is treated identically to the Azure OpenAI client. Configured
entirely by env vars (``LOCAL_MODEL_URL``, default ``http://127.0.0.1:8080/v1``;
``LOCAL_MODEL_NAME``, default ``qwen2.5-coder-7b-instruct``; legacy aliases
``LLAMA_CPP_BASE_URL``/``LLAMA_CPP_MODEL`` still accepted), so the model can be
swapped without touching orchestration code.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx


_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
_DEFAULT_MODEL_NAME = "qwen2.5-coder-7b-instruct"
_FALSE_VALUES = {"0", "false", "no", "off"}


def json_schema_response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The wire shape a JSON schema must be sent in to actually be applied: both
    llama-server and Azure OpenAI read ``response_format.json_schema.schema``.
    Sending it one level higher (``{"type": "json_schema", "schema": ...}``) is
    silently ignored by both, so every enum became a suggestion (#218)."""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": schema, "strict": True},
    }


class LlamaCppClientError(Exception):
    """Raised when the llama-cpp server returns an unexpected response."""


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from *text* (tolerant of surrounding prose)."""
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
    """Minimal async client for a local OpenAI-compatible model server
    (``/v1/chat/completions``). base_url defaults to ``LOCAL_MODEL_URL`` (legacy
    ``LLAMA_CPP_BASE_URL`` accepted); timeout defaults high (300 s) because CPU
    inference is slow; model defaults to ``LOCAL_MODEL_NAME`` (legacy
    ``LLAMA_CPP_MODEL`` accepted) and is mainly used for logging.
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
        self.last_timings: dict[str, float | int] | None = None
        self.cache_prompt = os.getenv("LLAMA_CPP_CACHE_PROMPT", "1").strip().lower() not in _FALSE_VALUES
        try:
            self.id_slot = int(os.getenv("LLAMA_CPP_SLOT", "0"))
        except ValueError:
            self.id_slot = 0

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat-completion request and return the raw response dict; extra
        kwargs are forwarded into the request body."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "cache_prompt": self.cache_prompt,
            "id_slot": self.id_slot,
            **kwargs,
        }

        self.last_timings = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.TimeoutException as exc:
            prompt_tokens = sum(
                max(1, len(str(message.get("content", "")).encode("utf-8")) // 3)
                for message in messages
            )
            limit = (f"{self.timeout:g}" if isinstance(self.timeout, (int, float))
                     else str(self.timeout))
            raise LlamaCppClientError(
                f"local model call timed out after {limit}s "
                f"(prompt ~{prompt_tokens} tokens, max_tokens {max_tokens})"
            ) from exc

        if resp.status_code != 200:
            raise LlamaCppClientError(
                f"llama-cpp server returned HTTP {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        timings = data.get("timings") if isinstance(data, dict) else None
        if isinstance(timings, dict):
            names = ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")
            self.last_timings = {
                name: timings[name] for name in names
                if isinstance(timings.get(name), (int, float))
            }
            # llama-server versions have used several names and locations for
            # the reused prefix. Normalize them for callers and structured logs.
            for source in (data, timings):
                if not isinstance(source, dict):
                    continue
                for name in ("tokens_cached", "n_past", "cache_n"):
                    value = source.get(name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        self.last_timings["tokens_cached"] = value
                        break
                if "tokens_cached" in self.last_timings:
                    break
        return data  # type: ignore[no-any-return]

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

    @property
    def server_root(self) -> str:
        """The server URL without the OpenAI ``/v1`` suffix (llama-server's own routes)."""
        root = self.base_url
        return root[: -len("/v1")] if root.endswith("/v1") else root

    async def context_size(self) -> int:
        """The context size (``n_ctx``) per request from ``GET /props`` (newer servers
        under ``default_generation_settings``, older at top level); raises
        :class:`LlamaCppClientError` when the server does not say."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.server_root}/props")
        if resp.status_code != 200:
            raise LlamaCppClientError(f"llama-cpp /props returned HTTP {resp.status_code}")
        data = resp.json()
        settings = data.get("default_generation_settings") if isinstance(data, dict) else None
        for source in (settings, data):
            if isinstance(source, dict):
                value = source.get("n_ctx")
                if isinstance(value, int) and value > 0:
                    return value
        raise LlamaCppClientError("llama-cpp /props did not report n_ctx")

    async def count_tokens(self, text: str) -> int:
        """Token count of *text* by the server's own tokenizer (``POST /tokenize``)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{self.server_root}/tokenize", json={"content": text})
        if resp.status_code != 200:
            raise LlamaCppClientError(f"llama-cpp /tokenize returned HTTP {resp.status_code}")
        tokens = resp.json().get("tokens")
        if not isinstance(tokens, list):
            raise LlamaCppClientError("llama-cpp /tokenize returned no token list")
        return len(tokens)

    async def is_available(self) -> bool:
        """Return True if the llama-cpp server is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/models")
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    async def busy_slots(self) -> int | None:
        """How many server slots are processing a request (``GET /slots``).

        ``None`` when the server does not expose ``/slots`` (it is off unless
        llama-server runs with ``--slots``/``--metrics`` on some versions), so a
        caller can tell "idle" from "unknown". Connection errors propagate.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.server_root}/slots")
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, list):
            return None
        return sum(
            1 for slot in data
            if isinstance(slot, dict) and (slot.get("is_processing") or slot.get("state") == 1)
        )

    # Instance method kept for backward compatibility; delegates to the module-level
    # function, which needs no client state.
    def extract_json(self, text: str) -> dict[str, Any]:
        """Extract the first JSON object from *text* (delegates to module-level function)."""
        return extract_json(text)
