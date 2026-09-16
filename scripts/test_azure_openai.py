"""Manual Azure OpenAI diagnostic using the real Nexus delegation path."""

from __future__ import annotations

import asyncio
import os

from modules.nexus_delegation import NexusDelegationService


def _credential_hint() -> str:
    key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    if not key:
        return "not configured"
    return f"configured (type=str, length={len(key)})"


async def _main() -> int:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/") or "<not configured>"
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

    print("Azure OpenAI diagnostic")
    print(f"endpoint: {endpoint}")
    print(f"deployment: {deployment}")
    print(f"credential: {_credential_hint()}")
    print("delegation path: NexusDelegationService.delegate('azure_openai', ...)")

    service = NexusDelegationService()
    try:
        result = await service.delegate(
            "azure_openai",
            "Reply with exactly OK.",
            context="manual diagnostic",
        )
    finally:
        service.close()

    print(f"success: {result.success}")
    if result.success:
        print(f"response: {result.content}")
        return 0

    status = result.metadata.get("http_status", "n/a")
    print(f"HTTP status: {status}")
    print(f"error: {result.metadata.get('error', 'unknown')}")
    detail = result.metadata.get("error_detail")
    if detail:
        print(f"Azure detail: {detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
