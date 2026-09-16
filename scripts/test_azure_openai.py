#!/usr/bin/env python3
"""Manual Azure OpenAI diagnostic using the real Nexus delegation path."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

# Support direct execution via /agent/scripts/test_azure_openai.py as well as
# `python -m scripts.test_azure_openai` from the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modules.nexus_delegation import NexusDelegationService

# Canonical runtime env file for Nexus services (see OPERATIONS.md). Values
# already present in the process environment always win.
_AGENT_ENV_FILE = Path("/etc/agent-lab/agent.env")


def _parse_env_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _load_agent_env(path: Path = _AGENT_ENV_FILE) -> None:
    """Populate missing AZURE_OPENAI_* vars from the Nexus env file.

    The file is only readable as root, so a direct read is attempted first
    (works when already root) and falls back to a non-interactive
    ``sudo`` read. Any failure is ignored; callers fall back to whatever is
    already in the process environment.
    """
    if not path.exists():
        return

    text: str | None = None
    try:
        text = path.read_text()
    except PermissionError:
        try:
            proc = subprocess.run(
                ["sudo", "-n", "cat", str(path)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                text = proc.stdout
        except (OSError, subprocess.SubprocessError):
            text = None
    except OSError:
        text = None

    if not text:
        return

    for key, value in _parse_env_lines(text).items():
        os.environ.setdefault(key, value)


def _credential_hint() -> str:
    key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    if not key:
        return "not configured"
    return f"configured (type=str, length={len(key)})"


async def _main() -> int:
    _load_agent_env()

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
