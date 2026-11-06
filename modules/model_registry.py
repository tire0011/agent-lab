"""One interface for chat models from any OpenAI-compatible provider (#552).

The models live in ``config/models.json`` (or the file named by
``NEXUS_MODELS_FILE``): adding a provider is a new entry, not new code.
Every provider is reached through the same ``/chat/completions`` call.
A model is available when its API key is set in the environment; keys are
read from the environment only and never logged. An optional per-entry
``headers`` object adds provider-required headers. A 2xx answer that is not
JSON is reported as ``non_json_response``, not as a success.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODELS_FILE = Path(__file__).resolve().parent.parent / "config" / "models.json"
COST_ORDER = ("local", "free", "paid")
_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class ModelSpec:
    id: str
    base_url: str
    model: str
    cost: str
    api_key_env: str = ""
    roles: tuple[str, ...] = ()
    description: str = ""
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def available(self) -> bool:
        return not self.api_key_env or bool(os.environ.get(self.api_key_env, "").strip())


@dataclass
class ChatResult:
    model_id: str
    cost: str
    success: bool
    content: str = ""
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    rate_limited: bool = False


def load_models(path: str | Path | None = None) -> list[ModelSpec]:
    """The configured models; an unreadable file means none."""
    source = Path(path or os.environ.get("NEXUS_MODELS_FILE") or DEFAULT_MODELS_FILE)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("model registry: cannot read %s: %s", source, exc)
        return []
    models = []
    for raw in data.get("models") or []:
        try:
            cost = str(raw.get("cost") or "paid").strip().lower()
            models.append(ModelSpec(
                id=str(raw["id"]).strip(),
                base_url=str(raw["base_url"]).rstrip("/"),
                model=str(raw["model"]).strip(),
                cost=cost if cost in COST_ORDER else "paid",
                api_key_env=str(raw.get("api_key_env") or "").strip(),
                roles=tuple(str(r).strip().lower() for r in raw.get("roles") or ()),
                description=str(raw.get("description") or ""),
                headers=tuple(
                    (str(k).strip(), str(v))
                    for k, v in (raw.get("headers") or {}).items()
                    if str(k).strip()
                ),
            ))
        except (KeyError, TypeError, AttributeError):
            logger.warning("model registry: skipping a malformed entry")
    return models


def candidates(models: list[ModelSpec], *, role: str | None = None, max_cost: str = "paid") -> list[ModelSpec]:
    """Available models for *role*, cheapest first, never above *max_cost*."""
    limit = COST_ORDER.index(max_cost) if max_cost in COST_ORDER else len(COST_ORDER) - 1
    chosen = [
        m for m in models
        if m.available and COST_ORDER.index(m.cost) <= limit and (role is None or not m.roles or role in m.roles)
    ]
    return sorted(chosen, key=lambda m: COST_ORDER.index(m.cost))


Transport = Callable[[str, dict[str, str], dict[str, Any]], httpx.Response]


def _post(url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
    return httpx.post(url, headers=headers, json=body, timeout=_TIMEOUT_SECONDS)


def chat(spec: ModelSpec, prompt: str, *, max_tokens: int = 1500, transport: Transport | None = None) -> ChatResult:
    """One chat completion against *spec*."""
    headers = dict(spec.headers)
    headers["Content-Type"] = "application/json"
    if spec.api_key_env:
        headers["Authorization"] = f"Bearer {os.environ.get(spec.api_key_env, '').strip()}"
    body = {"model": spec.model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
    try:
        response = (transport or _post)(f"{spec.base_url}/chat/completions", headers, body)
    except httpx.HTTPError as exc:
        return ChatResult(spec.id, spec.cost, False, error=type(exc).__name__)
    if response.status_code == 429:
        return ChatResult(spec.id, spec.cost, False, error="rate_limited", rate_limited=True)
    if response.status_code >= 400:
        return ChatResult(spec.id, spec.cost, False, error=f"http_{response.status_code}")
    try:
        data = response.json()
    except ValueError:
        logger.warning(
            "model registry: %s answered %s with a non-JSON body (content-type %s)",
            spec.id, response.status_code, response.headers.get("content-type", "unknown"),
        )
        return ChatResult(spec.id, spec.cost, False, error="non_json_response")
    try:
        content = str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ChatResult(spec.id, spec.cost, False, error="bad_response")
    return ChatResult(spec.id, spec.cost, bool(content.strip()), content=content,
                      error="" if content.strip() else "empty", usage=dict(data.get("usage") or {}))


def chat_first(
    prompt: str,
    *,
    role: str | None = None,
    max_cost: str = "paid",
    max_tokens: int = 1500,
    models: list[ModelSpec] | None = None,
    transport: Transport | None = None,
) -> ChatResult:
    """Ask the cheapest available model for *role*; on failure the next one."""
    tried: list[str] = []
    for spec in candidates(models if models is not None else load_models(), role=role, max_cost=max_cost):
        result = chat(spec, prompt, max_tokens=max_tokens, transport=transport)
        if result.success:
            return result
        tried.append(f"{spec.id}:{result.error}")
        logger.info("model registry: %s failed (%s), trying the next model", spec.id, result.error)
    return ChatResult("", "", False, error="not_available" + (f" ({', '.join(tried)})" if tried else ""))


__all__ = ["ChatResult", "ModelSpec", "candidates", "chat", "chat_first", "load_models"]
