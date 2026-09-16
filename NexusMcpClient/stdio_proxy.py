"""MCP stdio server forwarding tools to a remote Nexus MCP endpoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP

from .config import BridgeConfig
from .github_mailbox import submit_github_message

logger = logging.getLogger(__name__)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


class RemoteNexus:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.session: ClientSession | None = None
        self._http_context = None
        self._http_streams = None

    async def connect(self) -> list[Any]:
        self._http_context = streamablehttp_client(
            self.config.nexus_mcp_url,
            headers={"x-api-key": self.config.api_key},
            timeout=self.config.timeout_seconds,
        )
        self._http_streams = await self._http_context.__aenter__()
        receive, send, _ = self._http_streams
        self.session = ClientSession(receive, send)
        await self.session.__aenter__()
        await self.session.initialize()
        result = await self.session.list_tools()
        return list(result.tools)

    async def close(self) -> None:
        if self.session is not None:
            await self.session.__aexit__(None, None, None)
            self.session = None
        if self._http_context is not None:
            await self._http_context.__aexit__(None, None, None)
            self._http_context = None

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.session is None:
            raise RuntimeError("remote Nexus MCP session is not connected")
        result = await self.session.call_tool(name, arguments=arguments)
        return _jsonable(result)


def _make_forwarder(remote: RemoteNexus, name: str) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def forward(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Forward arguments to the matching remote Nexus tool."""
        return await remote.call(name, arguments or {})

    forward.__name__ = name
    return forward


async def _connect_and_register(server: FastMCP, remote: RemoteNexus):
    tools = await remote.connect()
    for tool in tools:
        name = str(tool.name)
        server.add_tool(
            _make_forwarder(remote, name),
            name=name,
            description=str(tool.description or f"Forwarded Nexus MCP tool: {name}"),
            structured_output=True,
        )

    async def mailbox_submit_github(
        type: str,
        created_by: str,
        body: str,
        request_id: str | None = None,
    ) -> dict[str, str]:
        """Submit a canonical inbox message through GitHub Contents API."""
        return await asyncio.to_thread(
            submit_github_message,
            msg_type=type,
            created_by=created_by,
            body=body,
            request_id=request_id,
        )

    if not any(str(tool.name) == "mailbox_submit_github" for tool in tools):
        server.add_tool(
            mailbox_submit_github,
            name="mailbox_submit_github",
            description=(
                "Write a canonical Nexus inbox message through GitHub Contents API. "
                "Requires MCP_WRITE_ENABLED=true, GITHUB_TOKEN, and "
                "NEXUS_MAILBOX_GITHUB_REPOSITORY."
            ),
            structured_output=True,
        )
    logger.info("registered %d remote Nexus MCP tools", len(tools))
    try:
        yield
    finally:
        await remote.close()


def create_stdio_server(config: BridgeConfig | None = None) -> FastMCP:
    config = config or BridgeConfig.from_env()
    remote = RemoteNexus(config)

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        async for item in _connect_and_register(server, remote):
            yield item

    return FastMCP(
        name="nexus-stdio-bridge",
        host=config.http_host,
        port=config.http_port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        instructions=(
            "Forward tools to the configured remote Nexus MCP instance over stdio or Streamable HTTP. "
            "Use mailbox tools to send commands and inspect results. "
            "Mutating tools remain controlled by the remote MCP_WRITE_ENABLED gate."
        ),
        lifespan=lifespan,
    )


def run_http_server(server: FastMCP, config: BridgeConfig) -> None:
    """Serve the Streamable HTTP app with CORS enabled for browser-based MCP clients."""
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    app = server.streamable_http_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.http_cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )
    uvicorn.run(app, host=config.http_host, port=config.http_port, log_level="info")
