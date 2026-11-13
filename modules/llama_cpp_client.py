"""Wrapper for OpenAI-compatible local model REST APIs (llama.cpp / llama-server),
so the local model is treated identically to the Azure OpenAI client. Configured
entirely by env vars (``LOCAL_MODEL_URL``, default ``http://127.0.0.1:8080/v1``;
``LOCAL_MODEL_NAME``, default ``qwen2.5-coder-7b-instruct``; legacy aliases
``LLAMA_CPP_BASE_URL``/``LLAMA_CPP_MODEL`` still accepted), so the model can be
swapped without touching orchestration code.

Streaming (#667, ``LOCAL_MODEL_STREAM``, default on): chat completions are
read as server-sent events. The client measures the time to the first token
(prompt ingestion) and the live generation rate, and stops a call early
instead of letting it run into the timeout: when no first token arrives within
``LOCAL_MODEL_TTFT_FRACTION`` (default 0.6) of the timeout
(``prompt_too_slow``), or when after ``LOCAL_MODEL_STREAM_MIN_TOKENS`` (default
16) tokens the projected finish (elapsed + remaining ``max_tokens`` at the live
rate) is beyond the timeout (``generation_too_slow``). The raised
:class:`LlamaCppClientError` carries the reason, the measured partial timings and
rates, and ``busy_slots()`` read at the moment of the stop. A finished stream is
assembled into the same response dict and ``last_timings`` as a non-streamed
call; ``last_stream`` holds the stream measurements.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Any, NoReturn

import httpx


_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
_DEFAULT_MODEL_NAME = "qwen2.5-coder-7b-instruct"
_FALSE_VALUES = {"0", "false", "no", "off"}
ENV_STREAM = "LOCAL_MODEL_STREAM"
ENV_TTFT_FRACTION = "LOCAL_MODEL_TTFT_FRACTION"
ENV_STREAM_MIN_TOKENS = "LOCAL_MODEL_STREAM_MIN_TOKENS"
# TTFT_LIMIT: no first token within this share of the timeout -> prompt_too_slow.
DEFAULT_TTFT_FRACTION = 0.6
# The generation projection is only trusted after this many streamed tokens.
DEFAULT_STREAM_MIN_TOKENS = 16
REASON_PROMPT_TOO_SLOW = "prompt_too_slow"
REASON_GENERATION_TOO_SLOW = "generation_too_slow"
REASON_TIMEOUT = "timeout"
EARLY_STOP_REASONS = (REASON_PROMPT_TOO_SLOW, REASON_GENERATION_TOO_SLOW)
_TIMING_NAMES = ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalize_timings(timings: Any) -> dict[str, float | int] | None:
    """The ``last_timings`` shape from llama-server's ``timings`` object."""
    if not isinstance(timings, dict):
        return None
    result: dict[str, float | int] = {
        name: timings[name] for name in _TIMING_NAMES if _number(timings.get(name))
    }
    # llama-server versions have used several names and locations for
    # the reused prefix. Normalize them for callers and structured logs.
    # Only timing-scoped fields describe work reused for this request.
    # Top-level ``tokens_cached``/``n_past`` describe the slot state on
    # llama-server versions in the field and can include newly evaluated
    # prompt and generated tokens.  Treating those as hits produces cache
    # ratios greater than one and makes the #645 measurement meaningless.
    for name in ("cache_n", "tokens_cached", "n_past"):
        value = timings.get(name)
        if _number(value):
            result["tokens_cached"] = value
            break
    return result


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
    """Raised when the llama-cpp server returns an unexpected response.

    A streamed call that was stopped early or timed out also carries
    ``reason`` (``prompt_too_slow`` / ``generation_too_slow`` / ``timeout``),
    ``timings`` (measured partial llama-server timings, ``None`` when not one
    token arrived), ``rates`` (measured tokens/s and seconds) and
    ``busy_slots`` (``GET /slots`` at the moment of the stop; it counts this
    request too, so more than one means another request was competing).
    """

    def __init__(self, message: str, *, reason: str | None = None,
                 timings: dict[str, float | int] | None = None,
                 rates: dict[str, Any] | None = None,
                 busy_slots: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.timings = timings
        self.rates = dict(rates or {})
        self.busy_slots = busy_slots


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
        *,
        stream: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
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
        # Measurements of the last streamed call (#667), ``None`` otherwise.
        self.last_stream: dict[str, Any] | None = None
        self.stream = (stream if stream is not None else
                       os.getenv(ENV_STREAM, "1").strip().lower() not in _FALSE_VALUES)
        self.ttft_fraction = min(1.0, _env_float(ENV_TTFT_FRACTION, DEFAULT_TTFT_FRACTION))
        self.stream_min_tokens = _env_int(ENV_STREAM_MIN_TOKENS, DEFAULT_STREAM_MIN_TOKENS)
        # Injected for tests (httpx.MockTransport); ``None`` is the real network.
        self.transport = transport
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
        self.last_stream = None
        if self.stream:
            return await self._stream_completion(payload, messages, max_tokens)
        try:
            async with self._http(self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise LlamaCppClientError(self._timeout_message(messages, max_tokens)) from exc

        if resp.status_code != 200:
            raise LlamaCppClientError(
                f"llama-cpp server returned HTTP {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        self.last_timings = normalize_timings(data.get("timings") if isinstance(data, dict) else None)
        return data  # type: ignore[no-any-return]

    def _http(self, timeout: Any) -> httpx.AsyncClient:
        if self.transport is not None:
            return httpx.AsyncClient(timeout=timeout, transport=self.transport)
        return httpx.AsyncClient(timeout=timeout)

    def _timeout_seconds(self) -> float | None:
        timeout = self.timeout
        if isinstance(timeout, httpx.Timeout):
            timeout = timeout.read
        if _number(timeout) and math.isfinite(timeout) and timeout > 0:
            return float(timeout)
        return None

    def _timeout_message(self, messages: list[dict[str, str]], max_tokens: int) -> str:
        prompt_tokens = sum(
            max(1, len(str(message.get("content", "")).encode("utf-8")) // 3)
            for message in messages
        )
        limit = (f"{self.timeout:g}" if isinstance(self.timeout, (int, float))
                 else str(self.timeout))
        return (f"local model call timed out after {limit}s "
                f"(prompt ~{prompt_tokens} tokens, max_tokens {max_tokens})")

    async def _busy_slots_now(self) -> int | None:
        try:
            return await self.busy_slots()
        except Exception:  # the probe must never hide the real stop reason
            return None

    async def _stream_completion(self, payload: dict[str, Any], messages: list[dict[str, str]],
                                 max_tokens: int) -> dict[str, Any]:
        """The ``stream=True`` path: SSE chunks assembled into a completion dict."""
        payload = {**payload, "stream": True, "timings_per_token": True,
                   "stream_options": {"include_usage": True}}
        timeout = self._timeout_seconds()
        ttft_limit = timeout * self.ttft_fraction if timeout else None
        started = time.monotonic()
        first_token_at: float | None = None
        last_token_at: float | None = None
        tokens = 0
        parts: list[str] = []
        role = "assistant"
        finish_reason: str | None = None
        server_timings: dict[str, Any] | None = None
        usage: dict[str, Any] | None = None
        meta: dict[str, Any] = {}

        def measured(now: float) -> tuple[dict[str, float | int] | None, dict[str, Any]]:
            """Partial timings (server per-token timings, else client clock) and rates."""
            ttft = (first_token_at - started) if first_token_at is not None else None
            gen_seconds = (last_token_at - first_token_at
                           if first_token_at is not None and last_token_at is not None else 0.0)
            timings = normalize_timings(server_timings) or {}
            if not _number(timings.get("predicted_n")) and tokens > 1 and gen_seconds > 0:
                # Tokens after the first one, over the time since the first one.
                timings["predicted_n"] = tokens - 1
                timings["predicted_ms"] = gen_seconds * 1000.0
            if not _number(timings.get("prompt_ms")) and ttft is not None:
                prompt_n = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                if _number(prompt_n) and prompt_n > 0:
                    timings["prompt_n"] = prompt_n
                    timings["prompt_ms"] = ttft * 1000.0
            rates: dict[str, Any] = {
                "elapsed_seconds": round(now - started, 3),
                "ttft_seconds": round(ttft, 3) if ttft is not None else None,
                "tokens_streamed": tokens,
                "prompt_tokens_per_second": None,
                "generation_tokens_per_second": None,
            }
            for key, n_key, ms_key in (("prompt_tokens_per_second", "prompt_n", "prompt_ms"),
                                       ("generation_tokens_per_second", "predicted_n", "predicted_ms")):
                n, ms = timings.get(n_key), timings.get(ms_key)
                if _number(n) and _number(ms) and n > 0 and ms > 0:
                    rates[key] = round(float(n) * 1000.0 / float(ms), 3)
            return (timings or None), rates

        async def stop(reason: str, detail: str, cause: BaseException | None = None) -> NoReturn:
            now = time.monotonic()
            busy = await self._busy_slots_now()
            timings, rates = measured(now)
            self.last_timings = timings
            self.last_stream = {**rates, "reason": reason, "busy_slots": busy}
            prompt_rate, gen_rate = rates["prompt_tokens_per_second"], rates["generation_tokens_per_second"]
            if reason == REASON_TIMEOUT:
                head = self._timeout_message(messages, max_tokens)
            else:
                head = f"local model call stopped early ({reason}) after {now - started:.1f}s"
            message = (
                f"{head}: {detail}; ttft "
                + (f"{rates['ttft_seconds']:.1f}s" if rates["ttft_seconds"] is not None else "none")
                + f", {tokens} token(s) streamed, prompt "
                + (f"{prompt_rate:.2f} tok/s" if prompt_rate else "unmeasured")
                + ", generation "
                + (f"{gen_rate:.2f} tok/s" if gen_rate else "unmeasured")
                + f", busy slots {busy if busy is not None else 'unknown'}"
            )
            error = LlamaCppClientError(message, reason=reason, timings=timings, rates=rates,
                                        busy_slots=busy)
            if cause is not None:
                raise error from cause
            raise error

        url = f"{self.base_url}/chat/completions"
        async with self._http(self.timeout) as client:
            try:
                async with client.stream("POST", url, json=payload,
                                         headers={"Content-Type": "application/json",
                                                  "Accept": "text/event-stream"}) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise LlamaCppClientError(
                            f"llama-cpp server returned HTTP {resp.status_code}: {body[:500]}"
                        )
                    lines = resp.aiter_lines().__aiter__()
                    while True:
                        elapsed = time.monotonic() - started
                        waiting_for_first = first_token_at is None
                        if waiting_for_first and ttft_limit is not None:
                            wait: float | None = ttft_limit - elapsed
                        else:
                            wait = timeout - elapsed if timeout is not None else None
                        try:
                            if wait is not None and wait <= 0:
                                raise asyncio.TimeoutError
                            line = await asyncio.wait_for(lines.__anext__(), wait)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            # The pending read was cancelled; this call ends here.
                            if waiting_for_first and ttft_limit is not None:
                                await stop(REASON_PROMPT_TOO_SLOW,
                                           f"no first token within {ttft_limit:.1f}s "
                                           f"({self.ttft_fraction:.0%} of the {timeout:g}s timeout)")
                            await stop(REASON_TIMEOUT, "total time limit reached")
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        raw = line[len("data:"):].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        if "error" in chunk:
                            raise LlamaCppClientError(f"llama-cpp stream error: {str(chunk['error'])[:500]}")
                        for key in ("id", "created", "model", "system_fingerprint"):
                            if key in chunk and key not in meta:
                                meta[key] = chunk[key]
                        if isinstance(chunk.get("timings"), dict):
                            server_timings = chunk["timings"]
                        if isinstance(chunk.get("usage"), dict):
                            usage = chunk["usage"]
                        choices = chunk.get("choices")
                        choice = choices[0] if isinstance(choices, list) and choices else None
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta") or {}
                        if isinstance(delta, dict):
                            if delta.get("role"):
                                role = str(delta["role"])
                            content = delta.get("content")
                            if content:
                                now = time.monotonic()
                                parts.append(str(content))
                                tokens += 1
                                if first_token_at is None:
                                    first_token_at = now
                                last_token_at = now
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                            continue  # the generation is over; no projection
                        if (timeout is not None and first_token_at is not None
                                and tokens >= self.stream_min_tokens
                                and last_token_at is not None and last_token_at > first_token_at):
                            rate = (tokens - 1) / (last_token_at - first_token_at)
                            projected = (last_token_at - started) + max(0, max_tokens - tokens) / rate
                            if projected > timeout:
                                await stop(REASON_GENERATION_TOO_SLOW,
                                           f"projected finish {projected:.0f}s for max_tokens "
                                           f"{max_tokens} at {rate:.2f} tok/s is beyond the "
                                           f"{timeout:g}s timeout")
            except httpx.TimeoutException as exc:
                await stop(REASON_TIMEOUT, f"{type(exc).__name__} while streaming", exc)

        now = time.monotonic()
        content = "".join(parts)
        data: dict[str, Any] = {
            "id": meta.get("id", ""),
            "object": "chat.completion",
            "created": meta.get("created", int(time.time())),
            "model": meta.get("model", self.model),
            "choices": [{
                "index": 0,
                "message": {"role": role, "content": content},
                "finish_reason": finish_reason,
            }],
        }
        if usage is not None:
            data["usage"] = usage
        if server_timings is not None:
            data["timings"] = server_timings
        client_timings, rates = measured(now)
        # The server's own timings when it sends them (identical to a
        # non-streamed call); otherwise what the client clock measured.
        self.last_timings = (normalize_timings(server_timings) if server_timings is not None
                             else client_timings)
        self.last_stream = rates
        return data

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, Any] | None = None,
        seed: int | None = None,
    ) -> str:
        """Convenience wrapper that returns the assistant's reply as a string.

        *seed* is sent as llama-server's ``seed`` sampling option (#639) only
        when given, so default requests are unchanged.
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        completion_options: dict[str, Any] = {}
        if response_format is not None:
            completion_options["response_format"] = response_format
        if seed is not None:
            completion_options["seed"] = int(seed)
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
        async with self._http(10.0) as client:
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
        async with self._http(30.0) as client:
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
            async with self._http(5.0) as client:
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
        async with self._http(5.0) as client:
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
