from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from modules.operations_registry import OperationsRegistry
from modules.task_orchestrator import NexusTaskOrchestrator


@dataclass(frozen=True, slots=True)
class ToolResolverEvalCase:
    id: str
    goal: str
    expected_operation: str | None = None
    expected_status: str | None = None


EVALUATION_CASES: tuple[ToolResolverEvalCase, ...] = (
    ToolResolverEvalCase(
        id="git-status",
        goal="inspect whether the Nexus repository has local changes",
        expected_operation="git_status",
    ),
    ToolResolverEvalCase(
        id="git-history",
        goal="show recent git commit history for the repository",
        expected_operation="git_log",
    ),
    ToolResolverEvalCase(
        id="memory-usage",
        goal="inspect host memory usage",
        expected_operation="memory",
    ),
    ToolResolverEvalCase(
        id="service-logs",
        goal="show recent systemd journal logs for the API service",
        expected_operation="service_logs",
    ),
    ToolResolverEvalCase(
        id="local-model-status",
        goal="inspect local llama model server status",
        expected_operation="local_model_status",
    ),
    ToolResolverEvalCase(
        id="gpu-utilization",
        goal="inspect GPU utilization",
        expected_status="not_found",
    ),
    ToolResolverEvalCase(
        id="git-branch-protection",
        goal="inspect git branch protection rules for the repository",
        expected_status="not_found",
    ),
)

_TOOL_SEARCH_STOP_WORDS = {
    "a",
    "an",
    "and",
    "check",
    "for",
    "has",
    "inspect",
    "need",
    "needs",
    "nexus",
    "of",
    "repository",
    "show",
    "the",
    "to",
    "whether",
}


class DeterministicResolverClient:
    async def complete(
        self,
        *,
        prompt: str,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        del temperature, max_tokens, response_format
        goal = _extract_section(prompt, "GOAL:", "CANDIDATE READ-ONLY OPERATIONS:")
        candidates_json = _extract_section(
            prompt,
            "CANDIDATE READ-ONLY OPERATIONS:",
            "Rules:",
        )
        candidates = json.loads(candidates_json)
        if not candidates:
            return json.dumps(
                {
                    "status": "not_found",
                    "operation": "",
                    "reason": "No candidates were supplied.",
                    "related_tools": [],
                },
                separators=(",", ":"),
            )
        selected = _select_candidate(goal, candidates)
        if selected is None:
            return json.dumps(
                {
                    "status": "not_found",
                    "operation": "",
                    "reason": "No candidate had enough lexical evidence.",
                    "related_tools": [str(item.get("operation", "")) for item in candidates[:3]],
                },
                separators=(",", ":"),
            )
        return json.dumps(
            {
                "status": "found",
                "operation": selected,
                "reason": "Deterministic lexical evaluation resolver selected the strongest candidate.",
                "related_tools": [],
            },
            separators=(",", ":"),
        )


def _extract_section(text: str, start: str, end: str) -> str:
    _, _, tail = text.partition(start)
    value, _, _ = tail.partition(end)
    return value.strip()


def _tokens(text: str) -> set[str]:
    import re

    return {
        token for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 1 and token not in _TOOL_SEARCH_STOP_WORDS
    }


def _select_candidate(goal: str, candidates: list[dict[str, Any]]) -> str | None:
    goal_tokens = _tokens(goal)
    best_score = 0
    best_name: str | None = None
    for candidate in candidates:
        name = str(candidate.get("operation", ""))
        tags = " ".join(str(tag) for tag in candidate.get("tags") or [])
        haystack = f"{name} {candidate.get('description', '')} {tags}"
        score = len(goal_tokens & _tokens(haystack))
        if name.lower() in goal.lower():
            score += 5
        for token in goal_tokens:
            if token in name.lower():
                score += 2
        if score > best_score:
            best_score = score
            best_name = name
    return best_name if best_score > 0 else None


class _NoopDelegation:
    def catalog(self) -> list[dict[str, Any]]:
        return []

    async def delegate(self, provider: str, task: str, *, context: str = "") -> Any:
        del provider, task, context
        raise RuntimeError("external delegation is not used by the deterministic evaluator")


def _experiment_service() -> Any:
    return SimpleNamespace(resolve_experiment=lambda experiment_id: SimpleNamespace(id=experiment_id))


async def evaluate_tool_resolver(
    *,
    repo_root: Path,
    db_path: Path,
    mode: str,
) -> dict[str, Any]:
    llama_client = DeterministicResolverClient() if mode == "deterministic" else None
    registry = OperationsRegistry(
        experiment_service=_experiment_service(),
        db_path=str(db_path),
        repo_root=str(repo_root),
    )
    orchestrator = NexusTaskOrchestrator(
        operations_registry=registry,
        experiment_service=_experiment_service(),
        llama_client=llama_client,
        delegation_service=_NoopDelegation(),
        max_steps=1,
    )
    try:
        cases: list[dict[str, Any]] = []
        for case in EVALUATION_CASES:
            session = orchestrator.task_store.get_or_create(
                source_type="tool_resolver_evaluation",
                source_id=case.id,
                title=case.goal,
            )
            observation = await orchestrator._resolve_find_tool(
                case.goal,
                session_id=session.id,
                expected_operation=case.expected_operation,
                expected_status=case.expected_status,
            )
            operation = observation.get("operation")
            selected_operation = (
                str(operation.get("operation"))
                if isinstance(operation, dict)
                else None
            )
            metrics = observation.get("metrics") or {}
            candidate_count = metrics.get("candidate_count")
            candidate_operations = list(metrics.get("candidate_operations") or [])
            resolver_invoked = bool(
                isinstance(candidate_count, int)
                and candidate_count > 0
                and metrics.get("resolver_llm_calls", 0)
            )
            cases.append(
                {
                    "id": case.id,
                    "goal": case.goal,
                    "expected_operation": case.expected_operation,
                    "expected_status": case.expected_status or "found",
                    "status": observation.get("status"),
                    "selected_operation": selected_operation,
                    "selection_correctness": metrics.get("selection_correctness"),
                    "candidate_count": candidate_count,
                    "candidate_operations": candidate_operations,
                    "expected_operation_in_candidates": (
                        case.expected_operation in candidate_operations
                        if case.expected_operation else None
                    ),
                    "resolver_invoked": resolver_invoked,
                    "resolver_provider": (
                        "deterministic"
                        if mode == "deterministic"
                        else metrics.get("resolver_provider")
                    ),
                    "runtime_provider": (
                        "local_job_queue_with_deterministic_client"
                        if mode == "deterministic" and metrics.get("resolver_provider")
                        else metrics.get("resolver_provider")
                    ),
                    "reason": observation.get("reason"),
                }
            )
    finally:
        orchestrator.close()
        registry.close()
    return _summarize(mode=mode, cases=cases)


def _summarize(*, mode: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    correctness_values = {"correct", "incorrect"}
    evaluable = [
        case for case in cases
        if case.get("selection_correctness") in correctness_values
        and case.get("status") != "unavailable"
    ]
    correct = [
        case for case in evaluable
        if case.get("selection_correctness") == "correct"
    ]
    incorrect = [
        case for case in evaluable
        if case.get("selection_correctness") == "incorrect"
    ]
    unavailable = [case for case in cases if case.get("status") == "unavailable"]
    resolver_invoked = [case for case in cases if case.get("resolver_invoked")]
    llm_evaluable = [
        case for case in evaluable
        if mode == "local-llm" and case.get("resolver_invoked")
    ]
    llm_correct = [
        case for case in llm_evaluable
        if case.get("selection_correctness") == "correct"
    ]
    llm_selection_accuracy = (
        len(llm_correct) / len(llm_evaluable)
        if llm_evaluable else None
    )
    accuracy_cases = llm_evaluable if mode == "local-llm" else []
    expected_not_found = [
        case for case in accuracy_cases
        if case.get("expected_status") == "not_found"
    ]
    correct_not_found = [
        case for case in expected_not_found
        if case.get("selection_correctness") == "correct"
    ]
    expected_found = [
        case for case in accuracy_cases
        if case.get("expected_status") == "found"
    ]
    correct_found = [
        case for case in expected_found
        if case.get("selection_correctness") == "correct"
    ]
    candidate_expected_present = [
        case for case in cases
        if case.get("expected_operation")
        and case.get("expected_operation_in_candidates")
    ]
    candidate_expected_total = [
        case for case in cases
        if case.get("expected_operation")
    ]
    negative_with_candidates = [
        case for case in cases
        if case.get("expected_status") == "not_found"
        and int(case.get("candidate_count") or 0) > 0
    ]
    total = len(cases)
    return {
        "experiment": "operations_registry_find_tool_gateway",
        "mode": mode,
        "command": (
            "python -m modules.tool_resolver_evaluation --mode all "
            "--db-path /tmp/nexus-tool-resolver-eval.db --output docs/tool-resolver-evaluation.json"
        ),
        "deterministic_candidate_command": (
            "python -m modules.tool_resolver_evaluation --mode deterministic "
            "--db-path /tmp/nexus-tool-resolver-eval.db"
        ),
        "local_llm_command": (
            "python -m modules.tool_resolver_evaluation --mode local-llm "
            "--db-path /tmp/nexus-tool-resolver-eval-local.db"
        ),
        "summary": {
            "total_cases": total,
            "resolver_invoked_cases": len(resolver_invoked),
            "evaluable_cases": len(evaluable),
            "correct": len(correct),
            "incorrect": len(incorrect),
            "unavailable": len(unavailable),
            "accuracy": (
                llm_selection_accuracy if mode == "local-llm" else None
            ),
            "tool_selection_accuracy": (
                len(correct_found) / len(expected_found)
                if mode == "local-llm" and expected_found else None
            ),
            "not_found_accuracy": (
                len(correct_not_found) / len(expected_not_found)
                if mode == "local-llm" and expected_not_found else None
            ),
            "llm_selection_accuracy": llm_selection_accuracy,
            "candidate_expected_present_cases": len(candidate_expected_present),
            "candidate_expected_total_cases": len(candidate_expected_total),
            "candidate_expected_present_rate": (
                len(candidate_expected_present) / len(candidate_expected_total)
                if candidate_expected_total else None
            ),
            "negative_cases_with_candidates": len(negative_with_candidates),
        },
        "cases": cases,
    }


async def evaluate_all(*, repo_root: Path, db_path: Path) -> dict[str, Any]:
    deterministic_db = db_path.with_name(f"{db_path.stem}-deterministic{db_path.suffix}")
    local_llm_db = db_path.with_name(f"{db_path.stem}-local-llm{db_path.suffix}")
    for path in (deterministic_db, local_llm_db):
        if path.exists():
            path.unlink()
    deterministic = await evaluate_tool_resolver(
        repo_root=repo_root,
        db_path=deterministic_db,
        mode="deterministic",
    )
    local_llm = await evaluate_tool_resolver(
        repo_root=repo_root,
        db_path=local_llm_db,
        mode="local-llm",
    )
    return {
        "experiment": "operations_registry_find_tool_gateway",
        "mode": "all",
        "command": (
            "python -m modules.tool_resolver_evaluation --mode all "
            "--db-path /tmp/nexus-tool-resolver-eval.db --output docs/tool-resolver-evaluation.json"
        ),
        "local_llm_evaluation": local_llm,
        "deterministic_candidate_check": deterministic,
        "notes": [
            "deterministic_candidate_check uses a hand-coded resolver client and is not local LLM accuracy.",
            "local_llm_evaluation is the only section that may report LLM selection accuracy; unavailable resolver calls are excluded from accuracy denominators.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate OperationsRegistry find_tool selection accuracy.")
    parser.add_argument("--mode", choices=["deterministic", "local-llm", "all"], default="all")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--db-path", type=Path, default=Path(tempfile.gettempdir()) / "nexus-tool-resolver-eval.db")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.db_path.exists():
        args.db_path.unlink()
    if args.mode == "all":
        result = asyncio.run(
            evaluate_all(
                repo_root=args.repo_root,
                db_path=args.db_path,
            )
        )
    else:
        result = asyncio.run(
            evaluate_tool_resolver(
                repo_root=args.repo_root,
                db_path=args.db_path,
                mode=args.mode,
            )
        )
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
