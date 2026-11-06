"""The standalone tool-router MCP server, driven as a real external client (#118).

Every test here starts ``python -m modules.tool_router_mcp`` as a subprocess
and talks MCP over its stdio. Nothing calls the router in-process: what is
under test is the surface a client like Copilot CLI actually binds to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import LATEST_PROTOCOL_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_MODULE = "modules.tool_router_mcp"
GIT_STATUS_GOAL = "inspect repository changes"
NOT_FOUND_WITH_RELATED_GOAL = "inspect git branch protection rules for the repository"
TIMEOUT_SECONDS = 60


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


class _ScriptedLlamaHandler(BaseHTTPRequestHandler):
    """llama-server's chat endpoint, answering not_found with a suggestion."""

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        answer = json.dumps(
            {
                "status": "not_found",
                "operation": "",
                "reason": "Only a related operation exists.",
                "related_tools": ["git_status"],
            }
        )
        body = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


class ToolRouterMcpTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        (self.repo / "tracked.txt").write_text("content\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-q", "-m", "initial")
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self.env = {
            **os.environ,
            "AGENT_DB_PATH": str(self.tmp / "agent.db"),
            "NEXUS_DB_DURABILITY": "relaxed",
        }

    def server_args(self, resolver: str = "deterministic") -> list[str]:
        return ["-m", SERVER_MODULE, "--repo-root", str(self.repo), "--resolver", resolver]

    @asynccontextmanager
    async def client(self, resolver: str = "deterministic"):
        params = StdioServerParameters(
            command=sys.executable,
            args=self.server_args(resolver),
            env=self.env,
            cwd=str(REPO_ROOT),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def find_tool(self, session: ClientSession, goal: str) -> dict[str, Any]:
        result = await session.call_tool(
            "find_tool", {"goal": goal}, read_timeout_seconds=TIMEOUT_SECONDS
        )
        self.assertFalse(result.is_error)
        return result.structured_content

    async def call_tool(
        self, session: ClientSession, tool: str, arguments: dict[str, Any] | None = None
    ):
        return await session.call_tool(
            "call_tool",
            {"tool": tool, "arguments": arguments or {}},
            read_timeout_seconds=TIMEOUT_SECONDS,
        )

    def scripted_llama(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedLlamaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env["LOCAL_MODEL_URL"] = f"http://127.0.0.1:{server.server_address[1]}/v1"


class ToolSurfaceTests(ToolRouterMcpTestCase):
    async def test_only_the_two_router_tools_are_exposed(self) -> None:
        async with self.client() as session:
            tools = (await session.list_tools()).tools
        self.assertEqual(sorted(tool.name for tool in tools), ["call_tool", "find_tool"])
        schemas = {tool.name: tool.input_schema for tool in tools}
        self.assertEqual(schemas["find_tool"]["required"], ["goal"])
        self.assertIn("arguments", schemas["call_tool"]["properties"])


class FindToolTests(ToolRouterMcpTestCase):
    async def test_found_returns_the_selected_operation_schema(self) -> None:
        async with self.client() as session:
            found = await self.find_tool(session, GIT_STATUS_GOAL)
        self.assertEqual(found["status"], "found")
        self.assertEqual(found["resolved_tool"], "git_status")
        self.assertEqual(found["operation"]["operation"], "git_status")
        self.assertEqual(found["arguments_schema"], [])

    async def test_missing_capability_is_not_found(self) -> None:
        async with self.client() as session:
            answer = await self.find_tool(session, "inspect GPU utilization")
        self.assertEqual(answer["status"], "not_found")
        self.assertEqual(answer["related_tools"], [])
        self.assertFalse(answer["retryable"])

    async def test_placeholder_goal_is_invalid(self) -> None:
        async with self.client() as session:
            answer = await self.find_tool(session, "plain-language capability needed")
        self.assertEqual(answer["status"], "invalid_goal")
        self.assertTrue(answer["guidance"])

    async def test_unreachable_model_is_unavailable_and_grants_nothing(self) -> None:
        self.env["LOCAL_MODEL_URL"] = "http://127.0.0.1:9/v1"
        async with self.client("local") as session:
            answer = await self.find_tool(session, GIT_STATUS_GOAL)
            refused = await self.call_tool(session, "git_status")
        self.assertEqual(answer["status"], "unavailable")
        self.assertTrue(answer["retryable"])
        self.assertTrue(refused.is_error)
        self.assertEqual(refused.structured_content["error"], "operation_not_granted")


class GrantTests(ToolRouterMcpTestCase):
    async def test_find_tool_then_call_tool_returns_the_real_result(self) -> None:
        async with self.client() as session:
            await self.find_tool(session, GIT_STATUS_GOAL)
            result = await self.call_tool(session, "git_status")
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["result"]["success"])
        self.assertTrue(any("tracked.txt" in line for line in payload["result"]["output_lines"]))
        self.assertEqual(payload["operation_schema"]["operation"], "git_status")

    async def test_call_without_a_grant_fails_closed(self) -> None:
        async with self.client() as session:
            result = await self.call_tool(session, "git_status")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"], "operation_not_granted")
        self.assertNotIn("result", result.structured_content)

    async def test_a_grant_covers_only_the_selected_operation(self) -> None:
        async with self.client() as session:
            await self.find_tool(session, GIT_STATUS_GOAL)
            result = await self.call_tool(session, "git_log")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"], "operation_not_granted")

    async def test_a_grant_does_not_reach_another_connection(self) -> None:
        async with self.client() as granted:
            await self.find_tool(granted, GIT_STATUS_GOAL)
            async with self.client() as other:
                result = await self.call_tool(other, "git_status")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"], "operation_not_granted")

    async def test_related_tools_never_grant(self) -> None:
        self.scripted_llama()
        async with self.client("local") as session:
            answer = await self.find_tool(session, NOT_FOUND_WITH_RELATED_GOAL)
            result = await self.call_tool(session, "git_status")
        self.assertEqual(answer["status"], "not_found")
        self.assertEqual(answer["related_tools"], ["git_status"])
        self.assertEqual(answer["recommended_operation"], "git_status")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"], "operation_not_granted")

    async def test_controller_only_operations_stay_out_of_reach(self) -> None:
        async with self.client() as session:
            answer = await self.find_tool(session, "service_restart machine_reboot")
            restart = await self.call_tool(session, "service_restart", {"service": "nexus-mailbox"})
            reboot = await self.call_tool(session, "machine_reboot")
        candidates = answer["metrics"]["candidate_operations"]
        self.assertNotIn("service_restart", candidates)
        self.assertNotIn("machine_reboot", candidates)
        self.assertEqual(restart.structured_content["error"], "operation_rejected")
        self.assertEqual(reboot.structured_content["error"], "operation_rejected")

    async def test_rejected_arguments_are_a_tool_error_and_the_server_survives(self) -> None:
        async with self.client() as session:
            await self.find_tool(session, "show recent git commit history for the repository")
            rejected = await self.call_tool(session, "git_log", {"limit": "not a number"})
            accepted = await self.call_tool(session, "git_log", {"limit": 1})
        self.assertTrue(rejected.is_error)
        self.assertEqual(rejected.structured_content["error"], "operation_rejected")
        self.assertFalse(accepted.is_error)
        self.assertEqual(len(accepted.structured_content["result"]["output_lines"]), 1)

    async def test_a_failing_operation_is_reported_as_an_error(self) -> None:
        async with self.client() as session:
            await self.find_tool(session, "experiment_show summary")
            result = await self.call_tool(session, "experiment_show", {"experiment_id": "deadbeef"})
        self.assertTrue(result.is_error)
        self.assertIs(result.structured_content["result"]["success"], False)


class TransportTests(ToolRouterMcpTestCase):
    def test_stdout_carries_nothing_but_json_rpc(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "raw-test", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "find_tool", "arguments": {"goal": GIT_STATUS_GOAL}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "call_tool", "arguments": {"tool": "git_status"}},
            },
        ]
        process = subprocess.Popen(
            [sys.executable, *self.server_args()],
            cwd=REPO_ROOT,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(process.kill)
        self.addCleanup(process.stdout.close)
        lines: list[str] = []
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            if "id" in request:
                lines.append(process.stdout.readline())
        process.stdin.close()
        lines.extend(process.stdout.readlines())
        self.assertEqual(process.wait(timeout=TIMEOUT_SECONDS), 0)

        messages = [json.loads(line) for line in lines]
        self.assertEqual([message.get("id") for message in messages], [1, 2, 3])
        self.assertTrue(all(message["jsonrpc"] == "2.0" for message in messages))
        self.assertTrue(messages[2]["result"]["structuredContent"]["result"]["success"])

    def test_startup_does_not_load_the_web_service_or_the_orchestrator(self) -> None:
        check = (
            f"import sys, {SERVER_MODULE}; "
            "print(sorted({'api', 'fastapi', 'nexus_mcp', 'modules.task_orchestrator'} "
            "& set(sys.modules)))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", check],
            cwd=REPO_ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
