"""Offline tests for the standalone Nexus MCP stdio bridge."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .config import BridgeConfig
from .stdio_proxy import RemoteNexus, _connect_and_register, create_stdio_server


class StdioBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_config_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                BridgeConfig.from_env()

    def test_config_uses_deployed_default_and_normalizes_url(self):
        with patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}, clear=True):
            config = BridgeConfig.from_env()
        self.assertEqual(
            config.nexus_mcp_url,
            "https://agent-lab-tire0011.germanywestcentral.cloudapp.azure.com/mcp/",
        )

    async def test_server_factory_creates_stdio_server(self):
        with patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}, clear=True):
            server = create_stdio_server()
        self.assertEqual(server.name, "nexus-stdio-bridge")

    async def test_startup_discovers_and_registers_remote_tool(self):
        server = create_stdio_server(BridgeConfig("http://example.test/mcp/", "test-key"))
        remote = RemoteNexus(BridgeConfig("http://example.test/mcp/", "test-key"))
        remote.connect = AsyncMock(return_value=[SimpleNamespace(name="health", description="Health")])
        remote.close = AsyncMock()

        async for _ in _connect_and_register(server, remote):
            tools = server._tool_manager.list_tools()
            self.assertIn("health", {tool.name for tool in tools})

        remote.connect.assert_awaited_once()
        remote.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
