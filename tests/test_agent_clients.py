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
agents_mcp_module.create_static_tool_filter = object

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
    @patch.object(agent_clients, "create_static_tool_filter")
    def test_uses_default_command_and_root_arguments(
        self, mock_create_static_tool_filter, mock_mcp_server_stdio
    ):
        mock_create_static_tool_filter.return_value = sentinel.tool_filter
        mock_mcp_server_stdio.return_value = sentinel.server

        with patch.dict(os.environ, {}, clear=True):
            server = build_filesystem_server()

        mock_create_static_tool_filter.assert_called_once_with(
            allowed_tool_names=["list_directory", "read_file"]
        )
        mock_mcp_server_stdio.assert_called_once_with(
            name="filesystem",
            params={
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/agent"],
            },
            cache_tools_list=True,
            tool_filter=sentinel.tool_filter,
        )
        self.assertIs(server, sentinel.server)

    @patch.object(agent_clients, "MCPServerStdio")
    @patch.object(agent_clients, "create_static_tool_filter")
    def test_uses_overridden_root_in_default_arguments(
        self, mock_create_static_tool_filter, mock_mcp_server_stdio
    ):
        mock_create_static_tool_filter.return_value = sentinel.tool_filter

        with patch.dict(os.environ, {"AGENT_FS_ROOT": "/workspace root"}, clear=True):
            build_filesystem_server()

        mock_mcp_server_stdio.assert_called_once_with(
            name="filesystem",
            params={
                "command": "npx",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    "/workspace root",
                ],
            },
            cache_tools_list=True,
            tool_filter=sentinel.tool_filter,
        )

    @patch.object(agent_clients, "MCPServerStdio")
    @patch.object(agent_clients, "create_static_tool_filter")
    def test_parses_quoted_argument_override_with_shlex(
        self, mock_create_static_tool_filter, mock_mcp_server_stdio
    ):
        mock_create_static_tool_filter.return_value = sentinel.tool_filter
        environment = {
            "MCP_FILESYSTEM_SERVER_COMMAND": "custom-mcp",
            "MCP_FILESYSTEM_SERVER_ARGS": '--flag "value with spaces" \'single quoted\'',
        }

        with patch.dict(os.environ, environment, clear=True):
            build_filesystem_server()

        mock_mcp_server_stdio.assert_called_once_with(
            name="filesystem",
            params={
                "command": "custom-mcp",
                "args": ["--flag", "value with spaces", "single quoted"],
            },
            cache_tools_list=True,
            tool_filter=sentinel.tool_filter,
        )

    @patch.object(agent_clients, "MCPServerStdio")
    @patch.object(agent_clients, "create_static_tool_filter")
    def test_propagates_malformed_argument_error(
        self, mock_create_static_tool_filter, mock_mcp_server_stdio
    ):
        with patch.dict(
            os.environ,
            {"MCP_FILESYSTEM_SERVER_ARGS": '--flag "unterminated'},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                build_filesystem_server()

        mock_create_static_tool_filter.assert_not_called()
        mock_mcp_server_stdio.assert_not_called()

    @patch.object(agent_clients, "MCPServerStdio")
    @patch.object(agent_clients, "create_static_tool_filter")
    def test_propagates_mcp_server_constructor_error(
        self, mock_create_static_tool_filter, mock_mcp_server_stdio
    ):
        mock_create_static_tool_filter.return_value = sentinel.tool_filter
        constructor_error = RuntimeError("constructor failed")
        mock_mcp_server_stdio.side_effect = constructor_error

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as raised:
                build_filesystem_server()

        self.assertIs(raised.exception, constructor_error)
        mock_create_static_tool_filter.assert_called_once_with(
            allowed_tool_names=["list_directory", "read_file"]
        )
        mock_mcp_server_stdio.assert_called_once_with(
            name="filesystem",
            params={
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/agent"],
            },
            cache_tools_list=True,
            tool_filter=sentinel.tool_filter,
        )


if __name__ == "__main__":
    unittest.main()
