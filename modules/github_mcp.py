"""Connection to the official GitHub MCP server (#41).

Nexus talks to GitHub through ``github/github-mcp-server`` instead of a
growing set of hand-written REST calls. The server runs as a stdio
subprocess; every connection spawns a fresh one, so nothing has to be kept
alive and a Nexus restart needs no extra step - the configuration is only
environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| ``GITHUB_PERSONAL_ACCESS_TOKEN`` | ``GITHUB_TOKEN``, then ``GH_TOKEN`` | token handed to the server |
| ``GITHUB_MCP_SERVER_COMMAND`` | ``github-mcp-server`` | executable (or ``docker``) |
| ``GITHUB_MCP_SERVER_ARGS`` | ``stdio`` | its arguments, shell-split |
| ``GITHUB_MCP_TOOLSETS`` | server default | passed on as ``GITHUB_TOOLSETS`` |
| ``GITHUB_MCP_TIMEOUT_SECONDS`` | ``60`` | per tool call |
| ``GITHUB_REPOSITORY`` | ``tire0011/agent-lab`` | default ``owner/repo`` |

The token is passed to the subprocess environment only; it is never part of
the command line, a log line or a result.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

DEFAULT_COMMAND = "github-mcp-server"
DEFAULT_ARGS = ("stdio",)
DEFAULT_REPOSITORY = "tire0011/agent-lab"
DEFAULT_TIMEOUT_SECONDS = 60.0
TOKEN_ENV_NAMES = ("GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
SERVER_NAME = "github"

T = TypeVar("T")


class GitHubMcpError(RuntimeError):
    """The GitHub MCP server is not configured, not reachable, or refused a call."""


@dataclass(frozen=True, slots=True)
class GitHubMcpConfig:
    command: str = DEFAULT_COMMAND
    args: tuple[str, ...] = DEFAULT_ARGS
    token: str | None = field(default=None, repr=False)
    toolsets: str | None = None
    repository: str = DEFAULT_REPOSITORY
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "GitHubMcpConfig":
        env = os.environ if environ is None else environ
        token = next(
            (env[name].strip() for name in TOKEN_ENV_NAMES if (env.get(name) or "").strip()),
            None,
        )
        args_env = env.get("GITHUB_MCP_SERVER_ARGS")
        args = tuple(shlex.split(args_env)) if args_env and args_env.strip() else DEFAULT_ARGS
        try:
            timeout = float(env.get("GITHUB_MCP_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
        except ValueError:
            timeout = DEFAULT_TIMEOUT_SECONDS
        return cls(
            command=(env.get("GITHUB_MCP_SERVER_COMMAND") or "").strip() or DEFAULT_COMMAND,
            args=args,
            token=token,
            toolsets=(env.get("GITHUB_MCP_TOOLSETS") or "").strip() or None,
            repository=(env.get("GITHUB_REPOSITORY") or "").strip() or DEFAULT_REPOSITORY,
            timeout_seconds=timeout if timeout > 0 else DEFAULT_TIMEOUT_SECONDS,
        )

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.repository.split("/", 1)[-1]

    def readiness_error(self) -> str | None:
        """Why the server cannot be started, or ``None`` when it can."""
        missing = []
        if not self.token:
            missing.append("GITHUB_PERSONAL_ACCESS_TOKEN/GITHUB_TOKEN/GH_TOKEN is not set")
        if shutil.which(self.command) is None:
            missing.append(
                f"GitHub MCP server command '{self.command}' was not found "
                "(install github-mcp-server or set GITHUB_MCP_SERVER_COMMAND)"
            )
        return "; ".join(missing) or None

    @property
    def configured(self) -> bool:
        return self.readiness_error() is None

    def server_env(self) -> dict[str, str]:
        """The subprocess environment: a minimal base plus the token."""
        from mcp.client.stdio import get_default_environment

        env = dict(get_default_environment())
        if self.token:
            env["GITHUB_PERSONAL_ACCESS_TOKEN"] = self.token
        if self.toolsets:
            env["GITHUB_TOOLSETS"] = self.toolsets
        return env

    def stdio_params(self) -> dict[str, Any]:
        """``command``/``args``/``env`` for an MCP stdio client (SDK or ``mcp``)."""
        return {"command": self.command, "args": list(self.args), "env": self.server_env()}


@dataclass(slots=True)
class GitHubMcpToolResult:
    tool: str
    is_error: bool
    text: str
    structured: Any = None

    def json(self) -> Any:
        """The result decoded as JSON: structured content, else the text."""
        if self.structured is not None:
            return self.structured
        try:
            return json.loads(self.text)
        except (TypeError, ValueError):
            return None


def _result_from(tool: str, result: Any) -> GitHubMcpToolResult:
    texts = [
        getattr(item, "text", "")
        for item in (getattr(result, "content", None) or [])
        if getattr(item, "type", None) == "text"
    ]
    return GitHubMcpToolResult(
        tool=tool,
        is_error=bool(getattr(result, "is_error", False)),
        text="\n".join(texts),
        structured=getattr(result, "structured_content", None),
    )


class GitHubMcpSession:
    """One open connection to the server; several calls share one subprocess."""

    def __init__(self, session: Any, config: GitHubMcpConfig) -> None:
        self._session = session
        self.config = config

    async def list_tools(self) -> list[Any]:
        from mcp.types import PaginatedRequestParams

        tools: list[Any] = []
        cursor = None
        while True:
            page = await self._session.list_tools(
                params=PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            tools.extend(page.tools)
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> GitHubMcpToolResult:
        result = await self._session.call_tool(
            name,
            arguments or {},
            read_timeout_seconds=self.config.timeout_seconds,
        )
        return _result_from(name, result)


@asynccontextmanager
async def open_session(config: GitHubMcpConfig | None = None) -> AsyncIterator[GitHubMcpSession]:
    """Start the GitHub MCP server and yield an initialized session."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config = config or GitHubMcpConfig.from_env()
    error = config.readiness_error()
    if error:
        raise GitHubMcpError(f"GitHub MCP server unavailable: {error}")
    params = StdioServerParameters(**config.stdio_params())
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=config.timeout_seconds)
                yield GitHubMcpSession(session, config)


def run_sync(factory: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine to completion from sync code, inside a loop or not."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    thread = threading.Thread(target=_target, name="github-mcp", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def list_tools(config: GitHubMcpConfig | None = None) -> list[Any]:
    async def _run() -> list[Any]:
        async with open_session(config) as session:
            return await session.list_tools()

    return run_sync(_run)


def call_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    config: GitHubMcpConfig | None = None,
) -> GitHubMcpToolResult:
    async def _run() -> GitHubMcpToolResult:
        async with open_session(config) as session:
            return await session.call_tool(name, arguments)

    return run_sync(_run)
