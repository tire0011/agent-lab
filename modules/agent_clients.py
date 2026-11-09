"""Construction of the Azure OpenAI client and the MCP filesystem server, kept
separate from agent.py so the external-service wiring can be read, tested, and
swapped independently of the run/log orchestration logic.
"""

import os
import shlex

from openai import AsyncOpenAI

from agents.mcp import MCPServerStdio

from modules.env import get_required_env
from modules.github_mcp import SERVER_NAME as GITHUB_SERVER_NAME, GitHubMcpConfig


def build_openai_client() -> AsyncOpenAI:
    endpoint = get_required_env("AZURE_OPENAI_ENDPOINT").rstrip("/")
    api_key = get_required_env("AZURE_OPENAI_API_KEY")
    return AsyncOpenAI(
        api_key=api_key,
        base_url=f"{endpoint}/openai/v1/",
    )


def build_filesystem_server() -> MCPServerStdio:
    command = os.getenv("MCP_FILESYSTEM_SERVER_COMMAND", "npx")
    args_env = os.getenv("MCP_FILESYSTEM_SERVER_ARGS")
    if args_env:
        args = shlex.split(args_env)
    else:
        args = ["-y", "@modelcontextprotocol/server-filesystem", "/"]

    return MCPServerStdio(
        name="filesystem",
        params={"command": command, "args": args},
        cache_tools_list=True,
    )


def build_github_server(config: GitHubMcpConfig | None = None) -> MCPServerStdio | None:
    """The official GitHub MCP server (#41), or ``None`` when it is not configured.

    ``None`` keeps the agent working without GitHub instead of failing to start.
    """
    config = config or GitHubMcpConfig.from_env()
    if not config.configured:
        return None
    return MCPServerStdio(
        name=GITHUB_SERVER_NAME,
        params=config.stdio_params(),
        cache_tools_list=True,
        client_session_timeout_seconds=config.timeout_seconds,
    )
