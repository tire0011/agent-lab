"""Read-only re-evaluation of terminal experiments against today's gates."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from modules.experiment_evaluation import evaluate_implementation, parse_git_status_short

from .models import STATUS_REJECTED, Experiment
from .parsing import parse_plan
from .policy import _check_goal_policy, plan_violates_policy


_ISSUE_RE = re.compile(r"\bGitHub\s+Issue\s+#(\d+)\b", re.IGNORECASE)


class GateRecheckStore(Protocol):
    def list_gate_recheck_candidates(self) -> list[Experiment]: ...


def _issue_number(goal: str) -> int | None:
    match = _ISSUE_RE.search(goal or "")
    return int(match.group(1)) if match else None


def _result(experiment: Experiment, verdict: str, reason: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": experiment.id,
        "issue_number": _issue_number(experiment.goal),
        "old_reason": experiment.error,
        "new_verdict": verdict,
    }
    if reason:
        result["new_reason"] = reason
    return result


def gate_recheck(store: GateRecheckStore) -> list[dict[str, Any]]:
    """Re-run current deterministic gates without changing stored state."""
    results: list[dict[str, Any]] = []
    for experiment in store.list_gate_recheck_candidates():
        if experiment.status == STATUS_REJECTED:
            reason = _check_goal_policy(experiment.goal)
            if reason is None and experiment.plan is not None:
                plan = parse_plan(json.dumps(experiment.plan))
                if plan is None:
                    reason = "stored plan cannot be parsed by the current policy gate"
                else:
                    reason = plan_violates_policy(plan, json.dumps(experiment.plan))
            results.append(
                _result(
                    experiment,
                    "still_blocked" if reason else "would_pass",
                    reason,
                )
            )
            continue

        worktree = Path(experiment.worktree_path) if experiment.worktree_path else None
        if worktree is None or not worktree.is_dir():
            results.append(_result(experiment, "worktree_missing"))
            continue

        evaluation = evaluate_implementation(
            experiment.plan,
            experiment.goal,
            parse_git_status_short(experiment.git_status),
            str(worktree),
        )
        results.append(
            _result(
                experiment,
                "would_pass" if evaluation.outcome == "pass" else "still_blocked",
                None if evaluation.outcome == "pass" else evaluation.reason,
            )
        )
    return results
