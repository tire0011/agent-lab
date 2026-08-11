"""Construction of the Azure OpenAI client and the MCP filesystem server.

Kept separate from agent.py so the wiring of external services (Azure
OpenAI SDK, MCP filesystem server) can be read, tested, and swapped
independently of the run/log orchestration logic.
"""

import os
import shlex

from openai import AsyncOpenAI

from agents.mcp import MCPServerStdio, create_static_tool_filter

from modules.env import get_required_env


def build_openai_client() -> AsyncOpenAI:
    endpoint = get_required_env("AZURE_OPENAI_ENDPOINT").rstrip("/")
    api_key = get_required_env("AZURE_OPENAI_API_KEY")
    return AsyncOpenAI(
        api_key=api_key,
        base_url=f"{endpoint}/openai/v1/",
    )


def build_filesystem_server() -> MCPServerStdio:
    allowed_root = os.getenv("AGENT_FS_ROOT", "/agent")
    command = os.getenv("MCP_FILESYSTEM_SERVER_COMMAND", "npx")
    args_env = os.getenv("MCP_FILESYSTEM_SERVER_ARGS")
    if args_env:
        args = shlex.split(args_env)
    else:
        args = ["-y", "@modelcontextprotocol/server-filesystem", allowed_root]

    return MCPServerStdio(
        name="filesystem",
        params={"command": command, "args": args},
        cache_tools_list=True,
        tool_filter=create_static_tool_filter(
            allowed_tool_names=["list_directory", "read_file"]
        ),
    )
