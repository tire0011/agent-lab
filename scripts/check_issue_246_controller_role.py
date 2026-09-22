#!/usr/bin/env python3
"""Issue #246: exercise one real Nexus controller turn without executing it.

This is deliberately a controller-only check. It renders the production Nexus
prompt and sends it through the real local llama-server, then stops after
parsing the chosen action. No selected tool is executed.

Scenarios:
- initial: "solve github issue #238" before issue contents are known.
- after-read: the same task after a successful github_issue(238) observation
  already supplied the root cause, suggested fix and acceptance criteria.

Exit codes:
  0 = controller chose an acceptable next action
  1 = regression reproduced
  2 = setup/inference failure
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from modules.operations_registry import OperationsRegistry
from modules.prompt_budget import compute_budget, read_mem_available_kb, read_swap_free_kb, read_swap_total_kb
from modules.task_orchestrator import (
    ACTION_COMMAND,
    TOOL_FIND_TOOL,
    TOOL_PYTHON_RUN,
    NexusTaskOrchestrator,
    _ORCHESTRATOR_RESPONSE_FORMAT,
)
from scripts.check_issue_196_controller_progress import _experiment_service


TASK = "solve github issue #238"

ISSUE_238_OBSERVATION = {
    "tool": "operation",
    "operation": "github_issue",
    "args": {"issue_number": 238},
    "result": {
        "operation": "github_issue",
        "success": True,
        "output_lines": [
            'issue: {"number":238,"state":"open","title":"memory_search indexes its own output and feeds the controller recursively nested results"}',
            "Problem: NexusMemory._search_task_events selects every nexus_task_events row with no kind filter. memory_search observations are journalled and later returned by memory_search, recursively nesting previous search output.",
            "Suggested fix: exclude orchestrator bookkeeping from the memory index, at minimum memory_search and probably controller_decision, find_tool_redirected, redundant_tool_request and terminal_decision_rejected.",
            "Acceptance criteria: a memory_search result never contains a serialized memory_search result; two successive searches do not nest the first output; canonical solve #196 does not loop; the controller-progress checker reports no loop-guard termination.",
        ],
    },
}


def verdict(scenario: str, decision: dict) -> tuple[bool, str]:
    action = str(decision.get("action", "")).strip().lower()

    if scenario == "initial":
        if action != TOOL_FIND_TOOL:
            return False, (
                "initial task should first obtain the concrete issue contents; "
                f"got action={action!r}"
            )
        goal = str(decision.get("goal", "")).strip().lower()
        forbidden_reasoning = (
            "root cause",
            "propose a solution",
            "propose solution",
            "design a fix",
            "implement the solution",
            "solve the issue",
            "resolve the issue",
        )
        if any(term in goal for term in forbidden_reasoning):
            return False, (
                "find_tool goal treats Nexus reasoning/development as a capability: "
                f"{goal!r}"
            )
        if "238" not in goal or "issue" not in goal:
            return False, (
                "find_tool goal is not concrete enough to retrieve GitHub issue #238: "
                f"{goal!r}"
            )
        return True, f"concrete read-only discovery: {goal}"

    if scenario == "after-read":
        if action not in {ACTION_COMMAND, TOOL_PYTHON_RUN}:
            return False, (
                "after github_issue(238) already supplied root cause/fix, Nexus should "
                "start local repository work itself; expected command/python_run, "
                f"got action={action!r}"
            )
        return True, f"local development next step: {action}"

    raise ValueError(f"unknown scenario {scenario!r}")


async def controller_turn(scenario: str) -> tuple[dict, str]:
    with tempfile.TemporaryDirectory(prefix="nexus-246-") as tmp:
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
                source_id=f"issue-246-{scenario}",
                title=TASK,
            )
            observations = [] if scenario == "initial" else [ISSUE_238_OBSERVATION]
            budget = compute_budget(
                mem_available_kb=read_mem_available_kb(),
                swap_free_kb=read_swap_free_kb(),
                swap_total_kb=read_swap_total_kb(),
            )
            prompt, meta = orchestrator._build_prompt(
                session,
                TASK,
                [],
                observations,
                budget,
            )
            raw, provider = await orchestrator._schema_completion(
                prompt,
                session_id=session.id,
                max_tokens=700,
                response_format=_ORCHESTRATOR_RESPONSE_FORMAT,
                budget_plan=budget,
                role="controller",
            )
            decision = orchestrator._parse(raw)
            return decision, json.dumps(
                {
                    "scenario": scenario,
                    "provider": provider,
                    "prompt_chars": len(prompt),
                    "omitted_observation_indices": meta.get("omitted_observation_indices"),
                },
                separators=(",", ":"),
            )
        finally:
            orchestrator.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=("initial", "after-read", "all"),
        default="all",
    )
    args = parser.parse_args()

    scenarios = ["initial", "after-read"] if args.scenario == "all" else [args.scenario]
    failed = False
    for scenario in scenarios:
        print(f"\n=== issue #246 controller-only scenario: {scenario} ===")
        try:
            decision, metadata = asyncio.run(controller_turn(scenario))
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
            return 2

        print(f"metadata: {metadata}")
        print("decision:")
        print(json.dumps(decision, indent=2, sort_keys=True))
        ok, reason = verdict(scenario, decision)
        print(("PASS: " if ok else "FAIL: ") + reason)
        failed = failed or not ok

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
