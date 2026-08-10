import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents import (
    Agent,
    Runner,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.mcp import MCPServerStdio, create_static_tool_filter
from agents.model_settings import ModelSettings
from openai import AsyncOpenAI


@dataclass(frozen=True)
class AzureCostSnapshot:
    timestamp: str
    amount: float
    currency: str
    period: str = "MonthToDate"
    source: str = "Azure Cost Management PreTaxCost"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "amount": self.amount,
            "currency": self.currency,
            "period": self.period,
            "source": self.source,
        }


class AzureCostMonitor:
    API_VERSION = "2024-08-01"

    def __init__(self, enabled: bool | None = None, az_command: str = "az") -> None:
        if enabled is None:
            enabled = os.getenv("AGENT_COST_MONITOR", "1").lower() not in {
                "0",
                "false",
                "no",
            }
        self.enabled = enabled
        self.az_command = az_command
        self._subscription_id: str | None = None

    def _run_az(self, args: list[str]) -> str:
        completed = subprocess.run(
            [self.az_command, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return completed.stdout

    def _get_subscription_id(self) -> str:
        if self._subscription_id is None:
            self._subscription_id = self._run_az(
                ["account", "show", "--query", "id", "--output", "tsv"]
            ).strip()
            if not self._subscription_id:
                raise RuntimeError("Azure CLI returned an empty subscription ID")
        return self._subscription_id

    def capture(self) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled"}

        try:
            subscription_id = self._get_subscription_id()
            body = json.dumps(
                {
                    "type": "Usage",
                    "timeframe": "MonthToDate",
                    "dataset": {
                        "granularity": "None",
                        "aggregation": {
                            "totalCost": {
                                "name": "PreTaxCost",
                                "function": "Sum",
                            }
                        },
                    },
                }
            )
            output = self._run_az(
                [
                    "rest",
                    "--method",
                    "post",
                    "--url",
                    (
                        "https://management.azure.com/subscriptions/"
                        f"{subscription_id}/providers/Microsoft.CostManagement/"
                        f"query?api-version={self.API_VERSION}"
                    ),
                    "--headers",
                    "Content-Type=application/json",
                    "--body",
                    body,
                ]
            )
            result = json.loads(output)
            properties = result.get("properties", {})
            columns = [column["name"] for column in properties.get("columns", [])]
            rows = properties.get("rows", [])
            if not rows:
                raise RuntimeError("Azure Cost Management returned no cost row")

            snapshot = AzureCostSnapshot(
                timestamp=datetime.now(timezone.utc).isoformat(),
                amount=float(rows[0][columns.index("PreTaxCost")]),
                currency=str(rows[0][columns.index("Currency")]),
            )
            return {"status": "ok", **snapshot.to_dict()}
        except Exception as exc:
            return {
                "status": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def summarize(
        before: dict[str, Any], after: dict[str, Any]
    ) -> dict[str, Any]:
        summary = {
            "before": before,
            "after": after,
            "actual_cost_delta": None,
            "status": "unavailable",
        }
        if before.get("status") == "disabled" and after.get("status") == "disabled":
            summary["status"] = "disabled"
            return summary
        if before.get("status") != "ok" or after.get("status") != "ok":
            return summary
        if before["currency"] != after["currency"]:
            summary["status"] = "inconsistent_currency"
            return summary

        summary["currency"] = after["currency"]
        if after["amount"] > before["amount"]:
            summary["actual_cost_delta"] = round(
                after["amount"] - before["amount"], 10
            )
            summary["status"] = "updated"
        else:
            summary["status"] = "pending"
        return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal Azure OpenAI task runner")
    parser.add_argument("task", nargs="+", help="Task prompt to send")
    return parser.parse_args()


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def to_json_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(to_jsonable(value), ensure_ascii=False)


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


def extract_usage_totals(result: Any) -> tuple[int, int, int]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    for response in getattr(result, "raw_responses", []):
        usage = getattr(response, "usage", None)
        if usage is None:
            continue
        prompt_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        completion_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        total_tokens += int(getattr(usage, "total_tokens", 0) or 0)

    return prompt_tokens, completion_tokens, total_tokens


def extract_tool_trace(result: Any) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    by_call_id: dict[str, dict[str, Any]] = {}

    for item in getattr(result, "new_items", []):
        if isinstance(item, ToolCallItem):
            raw_item = getattr(item, "raw_item", None)
            call_id = item.call_id or f"call-{len(trace) + 1}"
            arguments = getattr(raw_item, "arguments", None)
            entry = {
                "tool_call_id": call_id,
                "tool_name": item.tool_name or getattr(raw_item, "name", "unknown"),
                "arguments": to_json_string(arguments if arguments is not None else {}),
            }
            trace.append(entry)
            by_call_id[call_id] = entry
            continue

        if isinstance(item, ToolCallOutputItem):
            call_id = item.call_id or ""
            output_text = to_json_string(getattr(item, "output", None))

            if call_id and call_id in by_call_id:
                by_call_id[call_id]["result"] = output_text
            else:
                trace.append(
                    {
                        "tool_call_id": call_id,
                        "tool_name": "unknown",
                        "arguments": "{}",
                        "result": output_text,
                    }
                )

    return trace


async def run_agent(task: str) -> tuple[str, int, int, int, str, list[dict[str, Any]]]:
    model_name = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

    set_tracing_disabled(True)
    set_default_openai_client(build_openai_client(), use_for_tracing=False)
    set_default_openai_api("chat_completions")

    async with build_filesystem_server() as fs_server:
        agent = Agent(
            name="Filesystem Reader",
            model=model_name,
            instructions=(
                "You are a minimal read-only coding agent. "
                "Use MCP tools only when needed to answer the task. "
                "Available tools are list_directory and read_file only. "
                "All paths must stay under /agent. "
                "Never invent file contents; read files first when needed."
            ),
            mcp_servers=[fs_server],
            model_settings=ModelSettings(temperature=0),
        )

        result = await Runner.run(agent, task, max_turns=8)

    response_text = to_json_string(getattr(result, "final_output", ""))
    prompt_tokens, completion_tokens, total_tokens = extract_usage_totals(result)
    tool_trace = extract_tool_trace(result)

    return (
        response_text,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        model_name,
        tool_trace,
    )


def log_run(entry: dict) -> None:
    runs_dir = Path("state/runs")
    runs_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{entry['timestamp'].replace(':', '-').replace('.', '-')}_{uuid4().hex}.json"
    filepath = runs_dir / filename
    filepath.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")


def print_cost_summary(cost: dict[str, Any]) -> None:
    before = cost["before"]
    after = cost["after"]
    if before.get("status") == "ok":
        print(
            f"Azure cost before run: {before['amount']} {before['currency']} "
            f"({before['period']})"
        )
    if after.get("status") == "ok":
        print(
            f"Azure cost after run: {after['amount']} {after['currency']} "
            f"({after['period']})"
        )
    if cost["status"] == "updated":
        print(
            f"Actual Azure cost delta: {cost['actual_cost_delta']} "
            f"{cost['currency']}"
        )
    elif cost["status"] == "pending":
        print("Actual Azure run cost: pending Azure Cost Management refresh")
    elif cost["status"] != "disabled":
        print("Actual Azure cost: unavailable; see run artifact")


def main() -> int:
    args = parse_args()
    task = " ".join(args.task).strip()
    if not task:
        print("Task cannot be empty.", file=sys.stderr)
        return 2

    timestamp = datetime.now(timezone.utc).isoformat()
    cost_monitor = AzureCostMonitor()
    cost_before = cost_monitor.capture()
    started = time.perf_counter()

    try:
        (
            response_text,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            model_name,
            tool_trace,
        ) = asyncio.run(run_agent(task))
        runtime_seconds = round(time.perf_counter() - started, 3)
        cost_after = cost_monitor.capture()
        azure_cost = cost_monitor.summarize(cost_before, cost_after)

        entry = {
            "timestamp": timestamp,
            "model": model_name,
            "task": task,
            "response": response_text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "runtime_seconds": runtime_seconds,
            "mcp_tool_calls": tool_trace,
            "azure_cost": azure_cost,
        }
        log_run(entry)

        print(response_text)
        print_cost_summary(azure_cost)
        return 0
    except Exception as exc:
        runtime_seconds = round(time.perf_counter() - started, 3)
        cost_after = cost_monitor.capture()
        azure_cost = cost_monitor.summarize(cost_before, cost_after)
        error_entry = {
            "timestamp": timestamp,
            "model": os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
            "task": task,
            "response": f"ERROR: {exc}",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "runtime_seconds": runtime_seconds,
            "mcp_tool_calls": [],
            "azure_cost": azure_cost,
        }
        log_run(error_entry)
        print_cost_summary(azure_cost)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
