#!/usr/bin/env python3
"""Issue #246: exercise the real local preflight planner without executing developer actions."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.operations_registry import OperationsRegistry
from modules.prompt_budget import compute_budget, read_mem_available_kb, read_swap_free_kb, read_swap_total_kb
from modules.task_orchestrator import (
    NexusTaskOrchestrator,
    _PREFLIGHT_RESPONSE_FORMAT,
)
from scripts.check_issue_196_controller_progress import _experiment_service
from scripts.check_issue_246_controller_role import ISSUE_238_OBSERVATION

TASK = "solve github issue #238"

MEMORY_RESULTS = {
    "tool": "memory_search",
    "query": "exclude orchestrator bookkeeping from the memory index",
    "results": [
        {
            "source": "task_history",
            "kind": "operation",
            "content": "github_issue(238) succeeded; duplicate historical issue read with no new implementation information",
        }
        for _ in range(6)
    ],
    "preflight": True,
}

HISTORICAL_TASK = (
    "Continue the architecture approach we discussed last week for the Nexus "
    "controller and keep the earlier design decision."
)


def verdict(scenario: str, plan: dict) -> tuple[bool, str]:
    need_info = bool(plan.get("need_info"))
    kind = str(plan.get("kind", "")).strip().lower()
    request = str(plan.get("request", "")).strip().lower()
    next_goal = str(plan.get("next_goal", "")).strip().lower()

    if scenario == "initial":
        ok = need_info and kind == "read_only" and "238" in request and "issue" in request
        return ok, (
            f"expected concrete issue #238 read request; got need_info={need_info!r}, "
            f"kind={kind!r}, request={request!r}"
        )

    if scenario == "after-read":
        ok = (not need_info) and kind == "none" and bool(next_goal)
        return ok, (
            "expected no further information after root cause/fix/acceptance criteria were supplied; "
            f"got need_info={need_info!r}, kind={kind!r}, next_goal={next_goal!r}"
        )

    if scenario == "historical":
        ok = need_info and kind == "memory" and bool(request)
        return ok, (
            "expected a historical memory request; "
            f"got need_info={need_info!r}, kind={kind!r}, request={request!r}"
        )

    if scenario == "after-memory":
        ok = (not need_info) and kind == "none" and bool(next_goal)
        return ok, (
            "expected no further information after duplicate/no-new-information memory hits; "
            f"got need_info={need_info!r}, kind={kind!r}, request={request!r}, "
            f"next_goal={next_goal!r}"
        )

    raise ValueError(scenario)


async def planner_turn(scenario: str) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory(prefix="nexus-246-preflight-") as tmp:
        db_path = str(Path(tmp) / "nexus.db")
        registry = OperationsRegistry(
            experiment_service=_experiment_service(),
            db_path=db_path,
            repo_root="/agent",
        )
        orchestrator = NexusTaskOrchestrator(
            operations_registry=registry,
            experiment_service=_experiment_service(),
            max_steps=1,
        )
        try:
            session = orchestrator.task_store.get_or_create(
                source_type="task",
                source_id=f"issue-246-preflight-{scenario}",
                title=TASK,
            )
            if scenario == "initial":
                task = TASK
                observations = []
            elif scenario == "after-read":
                task = TASK
                observations = [ISSUE_238_OBSERVATION]
            elif scenario == "historical":
                task = HISTORICAL_TASK
                observations = []
            else:
                task = TASK
                observations = [ISSUE_238_OBSERVATION, MEMORY_RESULTS]

            budget = compute_budget(
                mem_available_kb=read_mem_available_kb(),
                swap_free_kb=read_swap_free_kb(),
                swap_total_kb=read_swap_total_kb(),
            )
            prompt = orchestrator._preflight_prompt(task, [], observations)
            raw, provider = await orchestrator._schema_completion(
                prompt,
                session_id=session.id,
                max_tokens=300,
                response_format=_PREFLIGHT_RESPONSE_FORMAT,
                budget_plan=budget,
                waiting_status="waiting_for_preflight",
                role="preflight_planner",
            )
            plan = orchestrator._parse_preflight(raw)
            return plan, {
                "scenario": scenario,
                "provider": provider,
                "prompt_chars": len(prompt),
            }
        finally:
            orchestrator.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=("initial", "after-read", "historical", "after-memory", "all"),
        default="all",
    )
    args = parser.parse_args()
    scenarios = (
        ["initial", "after-read", "historical", "after-memory"]
        if args.scenario == "all"
        else [args.scenario]
    )

    failed = False
    for scenario in scenarios:
        print(f"\n=== issue #246 preflight scenario: {scenario} ===")
        try:
            plan, metadata = asyncio.run(planner_turn(scenario))
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
            return 2
        print("metadata:", json.dumps(metadata, separators=(",", ":")))
        print("plan:")
        print(json.dumps(plan, indent=2, sort_keys=True))
        ok, reason = verdict(scenario, plan)
        print(("PASS: " if ok else "FAIL: ") + reason)
        failed = failed or not ok

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
