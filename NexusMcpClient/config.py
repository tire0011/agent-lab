"""Configuration for the standalone Nexus MCP stdio client."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_NEXUS_MCP_URL = "https://agent-lab-tire0011.germanywestcentral.cloudapp.azure.com/mcp/"


@dataclass(frozen=True)
class BridgeConfig:
    nexus_mcp_url: str
    api_key: str
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        api_key = os.environ.get("AGENT_API_KEY", "").strip()
        if not api_key:
            raise ValueError("AGENT_API_KEY is required")
        url = os.environ.get("NEXUS_MCP_URL", DEFAULT_NEXUS_MCP_URL).strip()
        if not url.startswith(("https://", "http://")):
            raise ValueError("NEXUS_MCP_URL must use http:// or https://")
        if not url.endswith("/"):
            url += "/"
        try:
            timeout = float(os.environ.get("NEXUS_MCP_TIMEOUT_SECONDS", "30"))
        except ValueError as exc:
            raise ValueError("NEXUS_MCP_TIMEOUT_SECONDS must be numeric") from exc
        if timeout <= 0:
            raise ValueError("NEXUS_MCP_TIMEOUT_SECONDS must be positive")
        return cls(nexus_mcp_url=url, api_key=api_key, timeout_seconds=timeout)
