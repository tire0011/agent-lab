#!/usr/bin/env python3
"""Issue #246: exercise the real local knowledge collector."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.operations_registry import OperationsRegistry
from modules.prompt_budget import (
    compute_budget,
    read_mem_available_kb,
    read_swap_free_kb,
    read_swap_total_kb,
)
from modules.task_orchestrator import (
    NexusTaskOrchestrator,
    _KNOWLEDGE_COLLECTOR_RESPONSE_FORMAT,
)
from scripts.check_issue_196_controller_progress import _experiment_service
from scripts.check_issue_246_controller_role import (
    ISSUE_238_OBSERVATION,
    MEMORY_SEARCH_OBSERVATION,
)

TASK = "solve github issue #238"
KNOWLEDGE_REQUEST = "Gather all relevant factual information about GitHub issue #238."


def _budget():
    return compute_budget(
        mem_available_kb=read_mem_available_kb(),
        swap_free_kb=read_swap_free_kb(),
        swap_total_kb=read_swap_total_kb(),
    )


def briefing_verdict(briefing: str) -> tuple[bool, str]:
    lower = briefing.lower()
    required_groups = [
        ("memory_search",),
        ("nexusmemory", "_search_task_events"),
        ("recursive", "recursively", "nested", "nest"),
        ("acceptance", "successive searches", "serialized"),
    ]
    missing = [
        "/".join(group)
        for group in required_groups
        if not any(term in lower for term in group)
    ]
    forbidden = ["use command", "run command", "python_run", "implement with"]
    planning = [term for term in forbidden if term in lower]
    ok = not missing and not planning
    return ok, f"missing={missing}, planning_terms={planning}"


async def synthesis_only() -> tuple[str, dict]:
    with tempfile.TemporaryDirectory(prefix="nexus-246-knowledge-synthesis-") as tmp:
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
                source_id="issue-246-knowledge-synthesis",
                title=TASK,
            )
            sources = [ISSUE_238_OBSERVATION, MEMORY_SEARCH_OBSERVATION]
            budget = _budget()
            prompt = orchestrator._knowledge_collector_prompt(
                KNOWLEDGE_REQUEST,
                sources,
                budget,
                force_briefing=True,
            )
            raw, provider = await orchestrator._schema_completion(
                prompt,
                session_id=session.id,
                max_tokens=650,
                response_format=_KNOWLEDGE_COLLECTOR_RESPONSE_FORMAT,
                budget_plan=budget,
                role="knowledge_collector",
            )
            decision = orchestrator._parse_knowledge_collector(raw)
            if decision["action"] != "briefing":
                raise RuntimeError(f"expected briefing, got {decision!r}")
            return decision["briefing"], {
                "scenario": "synthesis",
                "provider": provider,
                "prompt_chars": len(prompt),
                "source_count": len(sources),
                "knowledge_request": KNOWLEDGE_REQUEST,
            }
        finally:
            orchestrator.close()


async def live_collection() -> tuple[list[dict], dict]:
    db_path = "/var/lib/nexus/agent.db"
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
            source_id=f"issue-246-live-knowledge-collector-{uuid.uuid4().hex}",
            title=TASK,
        )
        # The mailbox command runner intentionally strips TOKEN/KEY/SECRET
        # variables, while the real agent-api service has GITHUB_TOKEN. For this
        # manual checker, seed the real issue observation fixture and exercise
        # live Nexus memory plus the same synthesis path instead of treating the
        # command sandbox as a production GitHub-read failure.
        knowledge_request = orchestrator._knowledge_request(TASK)
        sources = [ISSUE_238_OBSERVATION]
        sources.append(
            {
                "tool": "memory_search",
                "query": "GitHub issue #238",
                "results": orchestrator.memory.search("GitHub issue #238", limit=8),
                "knowledge_collector": True,
                "deterministic_reference": True,
            }
        )
        budget = _budget()
        prompt = orchestrator._knowledge_collector_prompt(
            knowledge_request,
            sources,
            budget,
            force_briefing=True,
        )
        raw, provider = await orchestrator._schema_completion(
            prompt,
            session_id=session.id,
            max_tokens=650,
            response_format=_KNOWLEDGE_COLLECTOR_RESPONSE_FORMAT,
            budget_plan=budget,
            role="knowledge_collector",
        )
        decision = orchestrator._parse_knowledge_collector(raw)
        observations = [
            {
                "tool": "knowledge_briefing",
                "briefing": decision["briefing"],
                "source_count": len(sources),
                "deterministic_retrieval": True,
                "manual_github_fixture": True,
                "provider": provider,
            }
        ]
        history = orchestrator.task_store.history(session.id, limit=20, max_chars=12000)
        return observations, {
            "scenario": "live",
            "session_id": session.id,
            "events": [
                {
                    "role": event.get("role"),
                    "kind": event.get("kind"),
                    "content": str(event.get("content", ""))[:1200],
                }
                for event in history
                if event.get("kind") in {
                    "knowledge_collection",
                    "memory_search",
                    "find_tool",
                    "operation",
                }
            ],
        }
    finally:
        orchestrator.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("synthesis", "live", "all"), default="all")
    args = parser.parse_args()
    failed = False

    if args.scenario in {"synthesis", "all"}:
        print("\n=== issue #246 knowledge collector: synthesis ===")
        try:
            briefing, metadata = asyncio.run(synthesis_only())
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
            return 2
        print("metadata:", json.dumps(metadata, separators=(",", ":")))
        print("briefing:")
        print(briefing)
        ok, reason = briefing_verdict(briefing)
        print(("PASS: " if ok else "FAIL: ") + reason)
        failed = failed or not ok

    if args.scenario in {"live", "all"}:
        print("\n=== issue #246 knowledge collector: live ===")
        try:
            observations, metadata = asyncio.run(live_collection())
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
            return 2
        print("metadata:")
        print(json.dumps(metadata, indent=2))
        print("observations:")
        print(json.dumps(observations, indent=2))
        briefing = ""
        for observation in observations:
            if observation.get("tool") == "knowledge_briefing":
                briefing = str(observation.get("briefing", ""))
                break
        ok, reason = briefing_verdict(briefing)
        print(("PASS: " if ok else "FAIL: ") + reason)
        failed = failed or not ok

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
