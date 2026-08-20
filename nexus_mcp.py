from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server import MCPServer

from models import ChatRequest


def _api():
    import api as nexus_api

    return nexus_api


def _write_enabled() -> bool:
    """Return True when MCP write tools are active (``MCP_WRITE_ENABLED=true``)."""
    return os.environ.get("MCP_WRITE_ENABLED", "").lower() == "true"


def _require_write_enabled() -> None:
    """Raise ValueError if write tools are disabled."""
    if not _write_enabled():
        raise ValueError(
            "Write tools are disabled. Set MCP_WRITE_ENABLED=true to enable them."
        )

def _internal_api_key() -> str:
    nexus_api = _api()
    return getattr(nexus_api, "API_KEY", os.environ["AGENT_API_KEY"])


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _call_api(route, /, *args: Any, **kwargs: Any) -> Any:
    try:
        return _jsonable(route(*args, **kwargs))
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        raise ValueError(f"{exc.status_code}: {detail}") from exc


def _validate_limit(limit: int, *, minimum: int, maximum: int) -> int:
    if limit < minimum or limit > maximum:
        raise ValueError(f"invalid limit: must be between {minimum} and {maximum}")
    return limit


@dataclass
class NexusMcpMount:
    app: FastAPI | None = field(default=None, init=False)

    @asynccontextmanager
    async def lifespan(self):
        app, session_manager = _build_mount_app()
        self.app = app
        async with session_manager.run():
            yield
        self.app = None

    async def __call__(self, scope, receive, send):
        if self.app is None:
            raise RuntimeError("MCP app not initialized")
        await self.app(scope, receive, send)


def _build_mount_app() -> tuple[FastAPI, Any]:
    mcp = MCPServer(
        name="nexus",
        title="Nexus MCP Adapter",
        description=(
            "Read-only MCP adapter for Nexus. Reuses the existing Nexus API and services, "
            "returns compact structured responses, and excludes write/shell operations."
        ),
        instructions=(
            "Use the read-only tools to inspect Nexus status, mailbox, experiments, activity, "
            "diagnostics, Codex task records, and maintenance state. No write tools are exposed."
        ),
        version="1.0.0",
    )

    @mcp.tool(description="GET /health", structured_output=True)
    def health() -> dict[str, Any]:
        return _call_api(_api().health)

    @mcp.tool(description="GET /secure-health", structured_output=True)
    def secure_health() -> dict[str, Any]:
        return _call_api(_api().secure_health, x_api_key=_internal_api_key())

    @mcp.tool(description="GET /status", structured_output=True)
    def status() -> dict[str, Any]:
        return _call_api(_api().status)

    @mcp.tool(description="GET /problems", structured_output=True)
    def problems() -> dict[str, Any]:
        return _call_api(_api().problems)

    @mcp.tool(description="GET /ideas", structured_output=True)
    def ideas() -> dict[str, Any]:
        return _call_api(_api().ideas)

    @mcp.tool(description="POST /chat (read-only)", structured_output=True)
    def chat(message: str) -> dict[str, Any]:
        return _call_api(_api().chat, ChatRequest(message=message))

    @mcp.tool(description="GET /codex/tasks", structured_output=True)
    def codex_tasks_list(limit: int | None = None) -> dict[str, Any]:
        nexus_api = _api()
        limit = nexus_api.CODEX_LIST_DEFAULT_LIMIT if limit is None else limit
        limit = _validate_limit(limit, minimum=nexus_api.LIMIT_MIN, maximum=nexus_api.CODEX_LIST_MAX_LIMIT)
        return _call_api(
            nexus_api.list_codex_tasks,
            x_api_key=_internal_api_key(),
            limit=limit,
            service=nexus_api._resolve_codex_service(),
        )

    @mcp.tool(description="GET /codex/tasks/{task_id}", structured_output=True)
    def codex_task_get(task_id: str) -> dict[str, Any]:
        nexus_api = _api()
        return _call_api(
            nexus_api.get_codex_task,
            task_id=task_id,
            x_api_key=_internal_api_key(),
            service=nexus_api._resolve_codex_service(),
        )

    @mcp.tool(description="GET /mailbox/tasks", structured_output=True)
    def mailbox_tasks_list(limit: int | None = None) -> dict[str, Any]:
        nexus_api = _api()
        limit = nexus_api.MAILBOX_TASKS_DEFAULT_LIMIT if limit is None else limit
        limit = _validate_limit(limit, minimum=nexus_api.LIMIT_MIN, maximum=nexus_api.MAILBOX_TASKS_MAX_LIMIT)
        return _call_api(
            nexus_api.list_mailbox_tasks,
            x_api_key=_internal_api_key(),
            limit=limit,
            service=nexus_api._resolve_mailbox_task_service(),
        )

    @mcp.tool(description="GET /mailbox/tasks/{approval_id}", structured_output=True)
    def mailbox_task_get(approval_id: str) -> dict[str, Any]:
        nexus_api = _api()
        return _call_api(
            nexus_api.get_mailbox_task,
            approval_id=approval_id,
            x_api_key=_internal_api_key(),
            service=nexus_api._resolve_mailbox_task_service(),
        )

    @mcp.tool(description="GET /mailbox/messages", structured_output=True)
    def mailbox_messages_list(limit: int | None = None) -> dict[str, Any]:
        nexus_api = _api()
        limit = nexus_api.MAILBOX_MESSAGES_DEFAULT_LIMIT if limit is None else limit
        limit = _validate_limit(limit, minimum=nexus_api.LIMIT_MIN, maximum=nexus_api.MAILBOX_MESSAGES_MAX_LIMIT)
        return _call_api(
            nexus_api.list_mailbox_messages,
            limit=limit,
            service=nexus_api._resolve_experiment_service(),
        )

    @mcp.tool(description="GET /experiments", structured_output=True)
    def experiments_list(limit: int | None = None) -> dict[str, Any]:
        nexus_api = _api()
        limit = nexus_api.EXPERIMENTS_DEFAULT_LIMIT if limit is None else limit
        limit = _validate_limit(limit, minimum=nexus_api.LIMIT_MIN, maximum=nexus_api.EXPERIMENTS_MAX_LIMIT)
        return _call_api(
            nexus_api.list_experiments,
            x_api_key=_internal_api_key(),
            limit=limit,
            service=nexus_api._resolve_experiment_service(),
        )

    @mcp.tool(description="GET /experiments/{experiment_id}", structured_output=True)
    def experiment_get(experiment_id: str) -> dict[str, Any]:
        nexus_api = _api()
        return _call_api(
            nexus_api.get_experiment,
            experiment_id=experiment_id,
            x_api_key=_internal_api_key(),
            service=nexus_api._resolve_experiment_service(),
        )

    @mcp.tool(description="GET /experiments/{experiment_id}/logs", structured_output=True)
    def experiment_logs(
        experiment_id: str,
        limit: int | None = None,
        level: str | None = None,
        phase: str | None = None,
    ) -> dict[str, Any]:
        nexus_api = _api()
        limit = nexus_api.EXPERIMENT_LOGS_DEFAULT_LIMIT if limit is None else limit
        limit = _validate_limit(limit, minimum=nexus_api.LIMIT_MIN, maximum=nexus_api.EXPERIMENT_LOGS_MAX_LIMIT)
        return _call_api(
            nexus_api.get_experiment_logs,
            experiment_id=experiment_id,
            x_api_key=_internal_api_key(),
            level=level,
            phase=phase,
            limit=limit,
            service=nexus_api._resolve_experiment_service(),
        )

    @mcp.tool(description="GET /experiments/{experiment_id}/adoption", structured_output=True)
    def experiment_adoption_get(experiment_id: str) -> dict[str, Any]:
        nexus_api = _api()
        return _call_api(
            nexus_api.get_experiment_adoption,
            experiment_id=experiment_id,
            x_api_key=_internal_api_key(),
            service=nexus_api._resolve_experiment_service(),
            adoption_service=nexus_api._resolve_experiment_adoption_service(),
        )

    @mcp.tool(description="GET /experiments/{experiment_id}/public", structured_output=True)
    def experiment_public_get(experiment_id: str) -> dict[str, Any]:
        nexus_api = _api()
        return _call_api(
            nexus_api.get_public_experiment,
            experiment_id=experiment_id,
            service=nexus_api._resolve_experiment_service(),
        )

    @mcp.tool(description="GET /activity/public", structured_output=True)
    def activity_public_list() -> dict[str, Any]:
        nexus_api = _api()
        return _call_api(
            nexus_api.list_public_activity,
            service=nexus_api._resolve_experiment_service(),
        )

    @mcp.tool(description="GET /activity", structured_output=True)
    def activity_list(
        limit: int | None = None,
        experiment_id: str | None = None,
        mailbox_task_id: str | None = None,
    ) -> dict[str, Any]:
        nexus_api = _api()
        limit = nexus_api.ACTIVITY_DEFAULT_LIMIT if limit is None else limit
        limit = _validate_limit(limit, minimum=nexus_api.LIMIT_MIN, maximum=nexus_api.ACTIVITY_MAX_LIMIT)
        return _call_api(
            nexus_api.list_activity,
            x_api_key=_internal_api_key(),
            limit=limit,
            experiment_id=experiment_id,
            mailbox_task_id=mailbox_task_id,
            service=nexus_api._resolve_experiment_service(),
        )

    @mcp.tool(description="GET /diagnostics/public", structured_output=True)
    def diagnostics_public_get(limit: int | None = None) -> dict[str, Any]:
        nexus_api = _api()
        limit = nexus_api.DIAGNOSTICS_DEFAULT_LIMIT if limit is None else limit
        limit = _validate_limit(limit, minimum=nexus_api.LIMIT_MIN, maximum=nexus_api.DIAGNOSTICS_MAX_LIMIT)
        return _call_api(
            nexus_api.get_public_diagnostics,
            limit=limit,
            service=nexus_api._resolve_experiment_service(),
            mailbox_service=nexus_api._resolve_mailbox_task_service(),
        )

    @mcp.tool(description="GET /operations/catalog", structured_output=True)
    def operations_catalog_get() -> dict[str, Any]:
        nexus_api = _api()
        return _call_api(
            nexus_api.get_operations_catalog,
            x_api_key=_internal_api_key(),
            operations_service=nexus_api._resolve_operations_service(),
        )

    @mcp.tool(description="GET /operations/chains", structured_output=True)
    def operation_chains_list(limit: int | None = None) -> list[dict[str, Any]]:
        nexus_api = _api()
        limit = nexus_api.OPERATION_CHAINS_DEFAULT_LIMIT if limit is None else limit
        limit = _validate_limit(limit, minimum=nexus_api.LIMIT_MIN, maximum=nexus_api.OPERATION_CHAINS_MAX_LIMIT)
        return _call_api(
            nexus_api.list_operation_chains,
            limit=limit,
            x_api_key=_internal_api_key(),
            service=nexus_api._resolve_operation_chain_service(),
        )

    @mcp.tool(description="GET /operations/chains/{request_id}", structured_output=True)
    def operation_chain_get(request_id: str) -> dict[str, Any]:
        nexus_api = _api()
        return _call_api(
            nexus_api.get_operation_chain,
            request_id=request_id,
            x_api_key=_internal_api_key(),
            service=nexus_api._resolve_operation_chain_service(),
        )

    @mcp.tool(description="GET /maintenance/status", structured_output=True)
    def maintenance_status_get() -> dict[str, Any]:
        nexus_api = _api()
        return _call_api(
            nexus_api.get_maintenance_status,
            x_api_key=_internal_api_key(),
            service=nexus_api._resolve_maintenance_service(),
        )

    @mcp.tool(description="GET /maintenance/audit", structured_output=True)
    def maintenance_audit_get() -> dict[str, Any]:
        nexus_api = _api()
        return _call_api(
            nexus_api.get_maintenance_audit,
            x_api_key=_internal_api_key(),
            service=nexus_api._resolve_maintenance_service(),
        )

    # ------------------------------------------------------------------
    # Write tools — only active when MCP_WRITE_ENABLED=true
    # All tools call _require_write_enabled() as their first action so
    # they fail immediately (and loudly) when the flag is off.
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Pull the latest commits for /agent via git pull --ff-only. "
            "Requires MCP_WRITE_ENABLED=true."
        ),
        structured_output=True,
    )
    async def git_pull() -> dict[str, Any]:
        _require_write_enabled()
        nexus_api = _api()
        executor = nexus_api.get_operations_executor()
        result = await executor.execute("git/pull", {})
        return {"success": result.success, "data": result.data, "error": result.error}

    @mcp.tool(
        description=(
            "Restart a systemd service by name. Allowed names: agent-api, nexus-mailbox, "
            "llama-server, all. Requires MCP_WRITE_ENABLED=true."
        ),
        structured_output=True,
    )
    async def service_restart(service: str) -> dict[str, Any]:
        _require_write_enabled()
        nexus_api = _api()
        executor = nexus_api.get_operations_executor()
        result = await executor.execute("services/restart", {"service": service})
        return {"success": result.success, "data": result.data, "error": result.error}

    @mcp.tool(
        description=(
            "Return the systemd status of all allowed services. "
            "Requires MCP_WRITE_ENABLED=true."
        ),
        structured_output=True,
    )
    async def service_status() -> dict[str, Any]:
        _require_write_enabled()
        nexus_api = _api()
        executor = nexus_api.get_operations_executor()
        result = await executor.execute("services/status", {})
        return {"success": result.success, "data": result.data, "error": result.error}

    @mcp.tool(
        description=(
            "Run the bounded test suite (pytest tests/ -q --tb=short) "
            "with a configurable timeout (default 120 s, max 300 s). "
            "Returns redacted, truncated output. "
            "Requires MCP_WRITE_ENABLED=true."
        ),
        structured_output=True,
    )
    async def run_tests(timeout_seconds: int = 120) -> dict[str, Any]:
        _require_write_enabled()
        import asyncio as _asyncio

        timeout_seconds = max(10, min(timeout_seconds, 300))
        repo_root = os.environ.get("AGENT_FS_ROOT", "/agent")

        def _run_sync() -> dict[str, Any]:
            t0 = time.monotonic()
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"],
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    cwd=repo_root,
                )
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                from modules.redaction import bounded_lines
                combined = proc.stdout + proc.stderr
                return {
                    "success": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "elapsed_ms": elapsed_ms,
                    "lines": bounded_lines(combined, max_lines=200, max_line_chars=500),
                }
            except subprocess.TimeoutExpired:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                return {
                    "success": False,
                    "exit_code": -1,
                    "elapsed_ms": elapsed_ms,
                    "lines": [f"[timed out after {timeout_seconds}s]"],
                }

        return await _asyncio.to_thread(_run_sync)

    @mcp.tool(
        description=(
            "Create and start a self-improvement experiment with the given goal. "
            "Returns experiment_id immediately; poll experiment_get for status. "
            "Requires MCP_WRITE_ENABLED=true."
        ),
        structured_output=True,
    )
    async def experiment_start(goal: str) -> dict[str, Any]:
        _require_write_enabled()
        import asyncio as _asyncio
        import threading

        nexus_api = _api()
        svc = nexus_api._resolve_experiment_service()
        # create_experiment does SQLite I/O; run it off the event loop thread.
        experiment = await _asyncio.to_thread(svc.create_experiment, goal)

        def _run():
            try:
                svc.run_experiment(experiment.id)
            except Exception:
                pass

        t = threading.Thread(target=_run, daemon=True, name=f"mcp-exp-{experiment.id[:8]}")
        t.start()
        return {"experiment_id": experiment.id, "status": experiment.status}

    @mcp.tool(
        description=(
            "Route a task through the local llama-cpp router and return the decision. "
            "Returns decision, provider, role, and reasoning. "
            "Requires MCP_WRITE_ENABLED=true and a running llama-server."
        ),
        structured_output=True,
    )
    async def router_run(task: str) -> dict[str, Any]:
        _require_write_enabled()
        from modules.llama_router import LlamaRouter

        router = LlamaRouter()
        decision = await router.route(task)
        return {
            "decision": decision.decision,
            "provider": decision.provider,
            "role": decision.role,
            "reasoning": decision.reasoning,
            "confidence": decision.confidence,
        }

    @mcp.tool(
        description=(
            "Execute an arbitrary shell command as the service user with cwd /agent. "
            "Bounded timeout (default 30 s, max 120 s) and output (16 KiB). "
            "Output is redacted. Command and result are audit-logged. "
            "TEMPORARY bootstrap tool — only available when MCP_WRITE_ENABLED=true."
        ),
        structured_output=True,
    )
    async def shell(command: str, timeout_seconds: int = 30) -> dict[str, Any]:
        _require_write_enabled()
        nexus_api = _api()
        executor = nexus_api.get_operations_executor()
        result = await executor.execute(
            "shell", {"command": command, "timeout_seconds": timeout_seconds}
        )
        return {"success": result.success, "data": result.data, "error": result.error}

    subapp = FastAPI()

    @subapp.middleware("http")
    async def require_api_key(request: Request, call_next):
        if request.headers.get("x-api-key") != _internal_api_key():
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)

    mcp_http_app = mcp.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",
    )
    subapp.mount("", mcp_http_app)
    return subapp, mcp.session_manager


def create_mcp_mount() -> NexusMcpMount:
    return NexusMcpMount()
