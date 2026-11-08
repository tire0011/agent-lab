"""The Nexus tool router as a standalone MCP stdio server (#118):
``python -m modules.tool_router_mcp`` exposes exactly two tools, ``find_tool`` and ``call_tool``,
so an external MCP client (e.g. Copilot CLI) can exercise the router without the orchestrator prompt
loop. This module is transport only - scoring, the resolver prompt, argument binding, the grant rule,
registry validation, redaction and audit stay in NexusToolRouter/OperationsRegistry, and a find_tool
answer passes on unchanged. Two things are specific to this entry point: the caller is NOT the
controller (``controller=False`` keeps ``service_restart``/``machine_reboot`` out of reach - a local
stdio client must not inherit that privilege), and the session is the process (one session id per
connection scopes every grant to it; grants are in memory and end with it)."""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from modules.local_llm_jobs import (
    ROLE_TOOL_ROUTER,
    LocalLlmJobError,
    LocalLlmJobService,
    acquire_local_llm_job_service,
    release_local_llm_job_service,
)
from modules.nexus_tool_router import (
    InMemoryToolGrantLedger,
    NexusToolRouter,
    ToolResolverUnavailableError,
    ToolRouterError,
)
from modules.operations_registry import _DEFAULT_REPO_ROOT, OperationsRegistry
from modules.run_store import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

SERVER_NAME = "nexus-tool-router"
TOOL_CALLER = "tool router mcp"
RESOLVER_LOCAL = "local"
RESOLVER_DETERMINISTIC = "deterministic"

SERVER_INSTRUCTIONS = (
    "Call find_tool with a concrete capability first. Only when it answers "
    "status=found may call_tool run the selected operation, using the "
    "arguments_schema from that answer. related_tools and related_operations "
    "are suggestions and never authorize a call."
)


class LocalLlmResolver:
    """The router inference path here: the shared local-LLM job queue, so a resolver call from this
    process takes its turn on the one inference slot instead of competing with the controller for
    llama-server. The job service starts a worker thread, so it is acquired on first use and a lookup
    that never reaches the resolver (``invalid_goal``, no candidates) costs nothing."""

    def __init__(self, *, db_path: str = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._jobs: LocalLlmJobService | None = None

    async def __call__(
        self,
        prompt: str,
        *,
        session_id: str,
        max_tokens: int,
        response_format: dict[str, Any],
        role: str = "tool_resolver",
        task_logger: Any | None = None,
    ) -> tuple[str, str]:
        del role, task_logger
        if self._jobs is None:
            self._jobs = acquire_local_llm_job_service(db_path=self._db_path)
        job_id = self._jobs.submit(
            prompt,
            temperature=0.0,
            max_tokens=max_tokens,
            session_id=session_id,
            response_format=response_format,
            role=ROLE_TOOL_ROUTER,
        )
        try:
            return await self._jobs.wait(job_id), "local"
        except LocalLlmJobError as exc:
            raise ToolResolverUnavailableError(str(exc)[:500], provider="local") from exc

    def close(self) -> None:
        if self._jobs is not None:
            release_local_llm_job_service(self._jobs)
            self._jobs = None


class DeterministicResolver:
    """The evaluator lexical resolver for checks that need no model: answers in the same resolver
    schema so everything after inference runs the production path; its selection is lexical and binds
    no argument values, so it is opt-in and never the default."""

    def __init__(self) -> None:
        # Lazy: the evaluation module pulls in the task orchestrator.
        from modules.tool_resolver_evaluation import DeterministicResolverClient

        self._client = DeterministicResolverClient()

    async def __call__(
        self,
        prompt: str,
        *,
        session_id: str,
        max_tokens: int,
        response_format: dict[str, Any],
        role: str = "tool_resolver",
        task_logger: Any | None = None,
    ) -> tuple[str, str]:
        del session_id, role, task_logger
        raw = await self._client.complete(
            prompt=prompt,
            temperature=0.0,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        return raw, "none"

    def close(self) -> None:
        pass


class LazyExperimentService:
    """``ExperimentService``, built only if an experiment operation needs it - most lookups never touch
    one, and constructing the service would almost double this process startup time."""

    def __init__(self) -> None:
        self._service: Any | None = None

    def __getattr__(self, name: str) -> Any:
        if self._service is None:
            from modules.experiment_service import ExperimentService

            self._service = ExperimentService()
        return getattr(self._service, name)


def tool_result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=is_error,
    )


def build_server(router: NexusToolRouter, *, session_id: str) -> MCPServer:
    """An MCP server exposing *router* to the single session *session_id*."""
    server = MCPServer(SERVER_NAME, instructions=SERVER_INSTRUCTIONS, log_level="WARNING")

    @server.tool()
    async def find_tool(goal: str) -> CallToolResult:
        """Resolve a concrete capability to one Nexus operation: returns found / invalid_goal / not_found /
        unavailable; only found grants the selected operation to this session."""
        return tool_result(await router.resolve_find_tool(goal, session_id=session_id))

    @server.tool()
    async def call_tool(tool: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        """Run an operation granted by an earlier find_tool result (status=found)."""
        args = arguments or {}
        try:
            payload = router.execute_operation(
                {"operation": tool, "args": args, "session_id": session_id}
            )
        except ToolRouterError as exc:
            payload = {
                "tool": "operation",
                "operation": tool,
                "args": args,
                "error": "operation_rejected",
                "reason": str(exc),
            }
        failed = "error" in payload or not payload["result"].get("success")
        return tool_result(payload, is_error=failed)

    return server


def build_router(
    *,
    registry: OperationsRegistry,
    experiment_service: Any,
    resolver: LocalLlmResolver | DeterministicResolver,
) -> NexusToolRouter:
    return NexusToolRouter(
        operations_registry=registry,
        experiment_service=experiment_service,
        schema_completion=resolver,
        grant_ledger=InMemoryToolGrantLedger(),
        caller=TOOL_CALLER,
        controller=False,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        default=_DEFAULT_REPO_ROOT,
        help="Repository the git operations read (default: %(default)s).",
    )
    parser.add_argument(
        "--resolver",
        choices=[RESOLVER_LOCAL, RESOLVER_DETERMINISTIC],
        default=RESOLVER_LOCAL,
        help="local queues on llama-server; deterministic needs no model (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    resolver: LocalLlmResolver | DeterministicResolver = (
        DeterministicResolver()
        if args.resolver == RESOLVER_DETERMINISTIC
        else LocalLlmResolver()
    )
    experiment_service = LazyExperimentService()
    registry = OperationsRegistry(experiment_service=experiment_service, repo_root=args.repo_root)
    router = build_router(
        registry=registry,
        experiment_service=experiment_service,
        resolver=resolver,
    )
    try:
        build_server(router, session_id=f"mcp-{uuid.uuid4().hex}").run("stdio")
    finally:
        resolver.close()
        registry.close()


if __name__ == "__main__":
    main()
