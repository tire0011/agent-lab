import argparse
import asyncio
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cost_monitor import AzureCostMonitor
from run_store import (
    calculate_estimated_cost,
    get_applicable_price,
    init_db,
    insert_run,
    insert_tool_calls,
)

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


def print_run_summary(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    estimated_cost: float | None,
    estimated_cost_currency: str | None,
    runtime_ms: int,
    run_id: int,
) -> None:
    print(f"Tokens: {prompt_tokens} input / {completion_tokens} output")
    if estimated_cost is None:
        print("Estimated cost: pricing unavailable")
    else:
        currency = estimated_cost_currency or "USD"
        print(f"Estimated cost: {currency} {estimated_cost:.6f}")
    print(f"Runtime: {runtime_ms} ms")
    print(f"Run ID: {run_id}")


def main() -> int:
    args = parse_args()
    task = " ".join(args.task).strip()
    if not task:
        print("Task cannot be empty.", file=sys.stderr)
        return 2

    timestamp = datetime.now(timezone.utc).isoformat()
    provider = "azure_openai"
    model_name = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    db = init_db()
    cost_monitor = AzureCostMonitor()
    cost_before = cost_monitor.capture()
    started = time.perf_counter()

    try:
        (
            response_text,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            _,
            tool_trace,
        ) = asyncio.run(run_agent(task))
        runtime_seconds = round(time.perf_counter() - started, 3)
        runtime_ms = int(round(runtime_seconds * 1000))

        price = get_applicable_price(db, provider, model_name, timestamp)
        estimated_cost: float | None = None
        estimated_cost_currency: str | None = None
        if price is not None:
            estimated_cost = calculate_estimated_cost(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                input_price_per_million_tokens=price[
                    "input_price_per_million_tokens"
                ],
                output_price_per_million_tokens=price[
                    "output_price_per_million_tokens"
                ],
            )
            estimated_cost_currency = str(price["currency"])

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

        run_id = insert_run(
            db,
            timestamp=timestamp,
            provider=provider,
            model=model_name,
            input_description=task,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            total_tokens=total_tokens,
            runtime_ms=runtime_ms,
            estimated_cost=estimated_cost,
            actual_cost=None,
            cost_status="estimated",
            success=True,
            error=None,
        )
        insert_tool_calls(db, run_id, timestamp, tool_trace)

        print(response_text)
        print_cost_summary(azure_cost)
        print_run_summary(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost=estimated_cost,
            estimated_cost_currency=estimated_cost_currency,
            runtime_ms=runtime_ms,
            run_id=run_id,
        )
        return 0
    except Exception as exc:
        runtime_seconds = round(time.perf_counter() - started, 3)
        runtime_ms = int(round(runtime_seconds * 1000))
        cost_after = cost_monitor.capture()
        azure_cost = cost_monitor.summarize(cost_before, cost_after)
        error_entry = {
            "timestamp": timestamp,
            "model": model_name,
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

        run_id = insert_run(
            db,
            timestamp=timestamp,
            provider=provider,
            model=model_name,
            input_description=task,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            runtime_ms=runtime_ms,
            estimated_cost=None,
            actual_cost=None,
            cost_status="estimated",
            success=False,
            error=str(exc),
        )

        print_cost_summary(azure_cost)
        print_run_summary(
            prompt_tokens=0,
            completion_tokens=0,
            estimated_cost=None,
            estimated_cost_currency=None,
            runtime_ms=runtime_ms,
            run_id=run_id,
        )
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
