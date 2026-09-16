"""Manual Azure OpenAI diagnostic using Nexus's real delegation path (#208)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable

from modules.nexus_delegation import (
    DelegationResult,
    NexusDelegationService,
    sanitize_azure_error_detail,
)


def diagnostic_lines(
    result: DelegationResult,
    *,
    endpoint: str,
    deployment: str,
    credential: str,
) -> Iterable[str]:
    """Build safe human-readable diagnostic output without credential values."""
    yield f"azure_endpoint={endpoint or '<not configured>'}"
    yield f"azure_deployment={deployment}"
    yield f"credential_configured={bool(credential)}"
    yield f"credential_length={len(credential) if credential else 0}"
    yield "credential_type=api-key"
    yield "delegation_path=NexusDelegationService.delegate('azure_openai', ...)"
    yield f"success={result.success}"

    status = result.metadata.get("http_status")
    if status is not None:
        yield f"http_status={status}"

    error = result.metadata.get("error")
    if error:
        yield f"error={sanitize_azure_error_detail(str(error), secrets=(credential,))}"

    detail = result.metadata.get("error_detail")
    if detail:
        yield f"error_detail={sanitize_azure_error_detail(str(detail), secrets=(credential,))}"

    if result.success:
        yield f"response={sanitize_azure_error_detail(result.content, secrets=(credential,))}"


async def main() -> int:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    credential = os.environ.get("AZURE_OPENAI_API_KEY", "")

    service = NexusDelegationService()
    try:
        result = await service.delegate(
            "azure_openai",
            "Reply with exactly OK.",
            context="manual diagnostic",
        )
    finally:
        service.close()

    for line in diagnostic_lines(
        result,
        endpoint=endpoint,
        deployment=deployment,
        credential=credential,
    ):
        print(line)
    return 0 if result.success else 1


if __name__ == "__main__":  # pragma: no cover - manual diagnostic entry point
    raise SystemExit(asyncio.run(main()))
