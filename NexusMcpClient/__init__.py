"""Standalone stdio MCP client for a remote Nexus instance."""

from .config import BridgeConfig
from .stdio_proxy import create_stdio_server

__all__ = ["BridgeConfig", "create_stdio_server"]
