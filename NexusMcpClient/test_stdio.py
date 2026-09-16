"""Offline tests for the standalone Nexus MCP stdio bridge."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .config import BridgeConfig
from .stdio_proxy import RemoteNexus, _connect_and_register, create_stdio_server, run_http_server


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
        self.assertEqual(config.transport, "stdio")
        self.assertEqual(config.http_host, "127.0.0.1")
        self.assertEqual(config.http_port, 8001)
        self.assertEqual(config.http_cors_origins, ("*",))

    def test_config_reads_http_transport_settings(self):
        values = {
            "AGENT_API_KEY": "test-key",
            "NEXUS_MCP_TRANSPORT": "streamable-http",
            "NEXUS_MCP_HTTP_HOST": "0.0.0.0",
            "NEXUS_MCP_HTTP_PORT": "9100",
        }
        with patch.dict(os.environ, values, clear=True):
            config = BridgeConfig.from_env()
        self.assertEqual(config.transport, "streamable-http")
        self.assertEqual(config.http_host, "0.0.0.0")
        self.assertEqual(config.http_port, 9100)

    def test_config_rejects_invalid_http_settings(self):
        for name, value in (
            ("NEXUS_MCP_TRANSPORT", "http"),
            ("NEXUS_MCP_HTTP_PORT", "not-a-port"),
            ("NEXUS_MCP_HTTP_PORT", "65536"),
            ("NEXUS_MCP_HTTP_CORS_ORIGINS", "   "),
        ):
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, {"AGENT_API_KEY": "test-key", name: value}, clear=True):
                    with self.assertRaises(ValueError):
                        BridgeConfig.from_env()

    def test_config_reads_cors_origins_list(self):
        values = {
            "AGENT_API_KEY": "test-key",
            "NEXUS_MCP_HTTP_CORS_ORIGINS": "http://127.0.0.1:8080, http://localhost:8080",
        }
        with patch.dict(os.environ, values, clear=True):
            config = BridgeConfig.from_env()
        self.assertEqual(config.http_cors_origins, ("http://127.0.0.1:8080", "http://localhost:8080"))

    async def test_server_factory_creates_stdio_server(self):
        with patch.dict(os.environ, {"AGENT_API_KEY": "test-key"}, clear=True):
            server = create_stdio_server()
        self.assertEqual(server.name, "nexus-stdio-bridge")
        self.assertEqual(server.settings.host, "127.0.0.1")
        self.assertEqual(server.settings.port, 8001)

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

    async def test_run_http_server_enables_cors_for_configured_origins(self):
        config = BridgeConfig(
            "http://example.test/mcp/",
            "test-key",
            http_cors_origins=("http://127.0.0.1:8080",),
        )
        server = create_stdio_server(config)
        with patch("uvicorn.run") as mock_run:
            run_http_server(server, config)

        mock_run.assert_called_once()
        app = mock_run.call_args.args[0]
        cors_entries = [
            entry for entry in app.user_middleware if entry.cls.__name__ == "CORSMiddleware"
        ]
        self.assertEqual(len(cors_entries), 1)
        self.assertEqual(cors_entries[0].kwargs["allow_origins"], ["http://127.0.0.1:8080"])


if __name__ == "__main__":
    unittest.main()
