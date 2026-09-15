import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from agents import (
    Agent,
    Runner,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from agents.model_settings import ModelSettings

from modules.agent_clients import build_filesystem_server, build_openai_client
from modules.agent_result import extract_tool_trace, extract_usage_totals
from modules.cli_output import print_cost_summary, print_run_summary
from modules.cost_monitor import AzureCostMonitor
from modules.run_log import log_run
from modules.run_store import (
    calculate_estimated_cost,
    get_applicable_price,
    init_db,
    insert_run,
    insert_tool_calls,
)
from modules.logging_config import configure_logging
from modules.serialization import to_json_string


configure_logging()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal Azure OpenAI task runner")
    parser.add_argument("task", nargs="+", help="Task prompt to send")
    parser.add_argument(
        "--use-router",
        action="store_true",
        default=False,
        help=(
            "Route the task through the local model decision router. "
            "The local model decides the provider (local / azure_openai / codex) "
            "and orchestrates context-gathering, planning, development, "
            "real verification, and review sequentially."
        ),
    )
    return parser.parse_args()


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


async def run_router_pipeline(task: str, db: Any) -> str:
    """Run the local-model router pipeline and return the final response text.

    This function lazily imports the orchestrator modules so the base agent
    continues to work even when the local model server is not running.
    """
    from modules.llama_cpp_client import LlamaCppClient
    from modules.multi_provider_orchestrator import MultiProviderOrchestrator

    llama_client = LlamaCppClient()

    async def azure_run_fn(prompt: str) -> str:
        response, _, _, _, _, _ = await run_agent(prompt)
        return response

    async def codex_run_fn(prompt: str) -> str:
        """Delegate a sub-task to Codex via CodexTaskService.

        The router picked ``codex`` by name here, so the provider is pinned
        rather than drawn: only callers that did not name an agent get the
        random choice.
        """
        from modules.codex_task_service import CodexTaskService, STATUS_SUCCEEDED
        from modules.codex_worker import PROVIDER as PROVIDER_CODEX
        from modules.run_store import DEFAULT_DB_PATH

        service = CodexTaskService(db_path=DEFAULT_DB_PATH)
        codex_task = service.create_task(
            prompt=prompt,
            working_dir=os.getcwd(),
            provider=PROVIDER_CODEX,
        )
        completed = service.run_task(codex_task.id)
        if completed.status == STATUS_SUCCEEDED:
            return completed.final_message or "(Codex completed with no output)"
        raise RuntimeError(
            f"Codex task failed (status={completed.status}): {completed.error}"
        )

    orchestrator = MultiProviderOrchestrator(
        llama_client=llama_client,
        azure_run_fn=azure_run_fn,
        codex_run_fn=codex_run_fn,
        db=db,
    )

    result = await orchestrator.run(task)
    if result.error:
        raise RuntimeError(result.error)
    return result.final_response


def main() -> int:
    args = parse_args()
    task = " ".join(args.task).strip()
    if not task:
        print("Task cannot be empty.", file=sys.stderr)
        return 2

    timestamp = datetime.now(timezone.utc).isoformat()
    db = init_db()
    cost_monitor = AzureCostMonitor()
    cost_before = cost_monitor.capture()
    started = time.perf_counter()

    if args.use_router:
        # ── Router pipeline (local model → Azure/Codex/local) ─────────────
        provider = "local_router"
        model_name = os.getenv("LOCAL_MODEL_NAME") or os.getenv("LLAMA_CPP_MODEL", "qwen2.5-coder-7b-instruct")
        try:
            response_text = asyncio.run(run_router_pipeline(task, db))
            runtime_seconds = round(time.perf_counter() - started, 3)
            runtime_ms = int(round(runtime_seconds * 1000))

            cost_after = cost_monitor.capture()
            azure_cost = cost_monitor.summarize(cost_before, cost_after)

            entry = {
                "timestamp": timestamp,
                "model": model_name,
                "task": task,
                "response": response_text,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "runtime_seconds": runtime_seconds,
                "mcp_tool_calls": [],
                "azure_cost": azure_cost,
            }
            log_run(entry)

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
                success=True,
                error=None,
            )

            print(response_text)
            print_cost_summary(azure_cost)
            print_run_summary(
                prompt_tokens=0,
                completion_tokens=0,
                estimated_cost=None,
                estimated_cost_currency=None,
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

    # ── Standard Azure OpenAI pipeline ────────────────────────────────────────
    provider = "azure_openai"
    model_name = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

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
