"""Configuration for the standalone Nexus MCP bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_TRANSPORT = "stdio"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8001
SUPPORTED_TRANSPORTS = frozenset({"stdio", "streamable-http"})


@dataclass(frozen=True)
class BridgeConfig:
    nexus_mcp_url: str
    api_key: str
    timeout_seconds: float = 30.0
    transport: str = DEFAULT_TRANSPORT
    http_host: str = DEFAULT_HTTP_HOST
    http_port: int = DEFAULT_HTTP_PORT
    http_cors_origins: tuple[str, ...] = ("*",)

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        api_key = os.environ.get("AGENT_API_KEY", "").strip()
        if not api_key:
            raise ValueError("AGENT_API_KEY is required")
        url = os.environ.get("NEXUS_MCP_URL", "").strip()
        if not url:
            raise ValueError("NEXUS_MCP_URL is required (e.g. https://<vm-host>/mcp/)")
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
        transport = os.environ.get("NEXUS_MCP_TRANSPORT", DEFAULT_TRANSPORT).strip().lower()
        if transport not in SUPPORTED_TRANSPORTS:
            supported = ", ".join(sorted(SUPPORTED_TRANSPORTS))
            raise ValueError(f"NEXUS_MCP_TRANSPORT must be one of: {supported}")
        http_host = os.environ.get("NEXUS_MCP_HTTP_HOST", DEFAULT_HTTP_HOST).strip()
        if not http_host:
            raise ValueError("NEXUS_MCP_HTTP_HOST must not be empty")
        try:
            http_port = int(os.environ.get("NEXUS_MCP_HTTP_PORT", str(DEFAULT_HTTP_PORT)))
        except ValueError as exc:
            raise ValueError("NEXUS_MCP_HTTP_PORT must be an integer") from exc
        if not 1 <= http_port <= 65535:
            raise ValueError("NEXUS_MCP_HTTP_PORT must be between 1 and 65535")
        cors_raw = os.environ.get("NEXUS_MCP_HTTP_CORS_ORIGINS", "*").strip()
        http_cors_origins = tuple(origin.strip() for origin in cors_raw.split(",") if origin.strip())
        if not http_cors_origins:
            raise ValueError("NEXUS_MCP_HTTP_CORS_ORIGINS must not be empty")
        return cls(
            nexus_mcp_url=url,
            api_key=api_key,
            timeout_seconds=timeout,
            transport=transport,
            http_host=http_host,
            http_port=http_port,
            http_cors_origins=http_cors_origins,
        )
