import os
import sys
import unittest
from types import ModuleType
from unittest.mock import patch, sentinel


openai_module = ModuleType("openai")
openai_module.AsyncOpenAI = object
agents_module = ModuleType("agents")
agents_module.__path__ = []
agents_mcp_module = ModuleType("agents.mcp")
agents_mcp_module.MCPServerStdio = object

with patch.dict(
    sys.modules,
    {
        "openai": openai_module,
        "agents": agents_module,
        "agents.mcp": agents_mcp_module,
    },
):
    from modules import agent_clients

build_filesystem_server = agent_clients.build_filesystem_server


class BuildFilesystemServerTests(unittest.TestCase):
    @patch.object(agent_clients, "MCPServerStdio")
    def test_uses_default_command_and_filesystem_root(self, mock_mcp_server_stdio):
        mock_mcp_server_stdio.return_value = sentinel.server
        with patch.dict(os.environ, {}, clear=True):
            server = build_filesystem_server()
        mock_mcp_server_stdio.assert_called_once_with(
            name="filesystem",
            params={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/"]},
            cache_tools_list=True,
        )
        self.assertIs(server, sentinel.server)

    @patch.object(agent_clients, "MCPServerStdio")
    def test_agent_fs_root_no_longer_restricts_default_server(self, mock_mcp_server_stdio):
        with patch.dict(os.environ, {"AGENT_FS_ROOT": "/workspace root"}, clear=True):
            build_filesystem_server()
        mock_mcp_server_stdio.assert_called_once_with(
            name="filesystem",
            params={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/"]},
            cache_tools_list=True,
        )

    @patch.object(agent_clients, "MCPServerStdio")
    def test_parses_quoted_argument_override_with_shlex(self, mock_mcp_server_stdio):
        environment = {
            "MCP_FILESYSTEM_SERVER_COMMAND": "custom-mcp",
            "MCP_FILESYSTEM_SERVER_ARGS": '--flag "value with spaces" \'single quoted\'',
        }
        with patch.dict(os.environ, environment, clear=True):
            build_filesystem_server()
        mock_mcp_server_stdio.assert_called_once_with(
            name="filesystem",
            params={"command": "custom-mcp", "args": ["--flag", "value with spaces", "single quoted"]},
            cache_tools_list=True,
        )

    @patch.object(agent_clients, "MCPServerStdio")
    def test_propagates_malformed_argument_error(self, mock_mcp_server_stdio):
        with patch.dict(os.environ, {"MCP_FILESYSTEM_SERVER_ARGS": '--flag "unterminated'}, clear=True):
            with self.assertRaises(ValueError):
                build_filesystem_server()
        mock_mcp_server_stdio.assert_not_called()

    @patch.object(agent_clients, "MCPServerStdio")
    def test_propagates_mcp_server_constructor_error(self, mock_mcp_server_stdio):
        constructor_error = RuntimeError("constructor failed")
        mock_mcp_server_stdio.side_effect = constructor_error
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as raised:
                build_filesystem_server()
        self.assertIs(raised.exception, constructor_error)


if __name__ == "__main__":
    unittest.main()
