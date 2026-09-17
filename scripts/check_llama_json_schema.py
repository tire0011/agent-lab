#!/usr/bin/env python3
"""Is the resolver's JSON schema actually enforced by llama-server?

`TOOL_RESOLVER_RESPONSE_FORMAT` declares `status` as an enum of four values,
and the #218 manual run came back with a status outside it while naming a
valid operation. That is only possible if the schema is not being enforced -
so this asks the server directly instead of reasoning about it.

The method is a schema the answer cannot satisfy by accident: `status` may be
the single word "found", and the prompt asks for "banana". Under a real
grammar constraint the model *cannot* emit banana, however it was asked.
Whatever comes back is the server's answer about that request shape.

Three shapes are compared:

1. ``{"type": "json_schema", "schema": {...}}``      - what Nexus sends today
2. ``{"type": "json_schema", "json_schema": {...}}`` - the OpenAI spec shape
3. ``{"type": "json_object", "schema": {...}}``      - llama.cpp's own shape

A shape that returns "banana", or that the server rejects, is not enforcing
the schema. One that returns "found" is.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx

PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["found"]}},
    "required": ["status"],
    "additionalProperties": False,
}
PROMPT = (
    'Return a JSON object whose "status" field is exactly the word banana. '
    'Do not write "found". Output only the JSON object.'
)

SHAPES: list[tuple[str, dict[str, Any]]] = [
    (
        "json_schema + schema      (what Nexus sends today)",
        {"type": "json_schema", "schema": PROBE_SCHEMA},
    ),
    (
        "json_schema + json_schema (OpenAI spec shape)",
        {
            "type": "json_schema",
            "json_schema": {"name": "probe", "schema": PROBE_SCHEMA, "strict": True},
        },
    ),
    (
        "json_object + schema      (llama.cpp native shape)",
        {"type": "json_object", "schema": PROBE_SCHEMA},
    ),
    ("no response_format        (control)", None),
]


async def _ask(base_url: str, model: str, response_format: Any, timeout: float) -> tuple[str, str]:
    """Returns (verdict, detail) for one request shape."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "temperature": 0.0,
        "max_tokens": 40,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        return "ERROR", f"request failed: {exc}"
    if resp.status_code != 200:
        return "REJECTED", f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        content = str(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError) as exc:
        return "ERROR", f"unexpected response structure: {exc}"
    return classify(content)


def classify(content: str) -> tuple[str, str]:
    """Whether one answer could only have come from a constrained decoder.

    The schema allows exactly ``found`` and the prompt asked for ``banana``,
    so any other answer - including one that is not JSON at all - is proof the
    constraint was not applied.
    """
    detail = content.strip().replace("\n", " ")[:160]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return "NOT ENFORCED", f"not even valid JSON: {detail}"
    if not isinstance(parsed, dict):
        return "NOT ENFORCED", f"not a JSON object: {detail}"
    if str(parsed.get("status", "")).strip() == "found":
        return "ENFORCED", detail
    return "NOT ENFORCED", detail


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--base-url",
        default=os.getenv("LOCAL_MODEL_URL") or os.getenv("LLAMA_CPP_BASE_URL") or "http://127.0.0.1:8080/v1",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LOCAL_MODEL_NAME") or os.getenv("LLAMA_CPP_MODEL") or "qwen2.5-coder-7b-instruct",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    print("llama-server JSON schema enforcement probe")
    print(f"server: {base_url}")
    print(f"model:  {args.model}")
    print('schema: status must be exactly "found"')
    print('prompt: asks for "banana"')
    print()

    results: dict[str, str] = {}
    for label, shape in SHAPES:
        verdict, detail = await _ask(base_url, args.model, shape, args.timeout)
        results[label] = verdict
        print(f"{label}")
        print(f"  -> {verdict}: {detail}")
        print()

    today = next(label for label in results if "today" in label)
    print("verdict")
    if results[today] == "ENFORCED":
        print("  The shape Nexus sends today IS enforced. The unusable status in")
        print("  #218 came from somewhere else - the schema is not the cause.")
        return 0

    working = [label for label, verdict in results.items()
               if verdict == "ENFORCED" and "control" not in label]
    print(f"  The shape Nexus sends today is {results[today]}: llama-server is not")
    print("  constraining the answer, which is how a status outside the enum got out.")
    if working:
        print("  A shape that IS enforced on this server:")
        for label in working:
            print(f"    - {label}")
        print("  Switching TOOL_RESOLVER_RESPONSE_FORMAT to it fixes this at the root.")
    else:
        print("  No shape was enforced on this server, so the schema cannot be relied")
        print("  on here at all and the parsing has to stay tolerant.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
