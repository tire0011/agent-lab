from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

# Status constants — small deterministic state machine.

# Start of the error of a run the pipeline itself crashed in. Nexus reads it to
# tell such a run from a judged attempt (#467).
PIPELINE_CRASH_ERROR_PREFIX = "unhandled exception in experiment pipeline: "

STATUS_QUEUED = "queued"
STATUS_SELECTING = "selecting"
STATUS_REJECTED = "rejected"
STATUS_IMPLEMENTING = "implementing"
STATUS_VERIFYING = "verifying"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"


_TERMINAL_STATUSES = (
    STATUS_REJECTED,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    STATUS_NEEDS_REVIEW,
    STATUS_CANCELLED,
    STATUS_INTERRUPTED,
)

# Valid forward transitions; run_experiment walks this graph and refuses to run
# twice (only queued -> implementing is a valid entry point; queued -> selecting
# is kept for stored experiments from the old multi-phase pipeline).
_STATE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATUS_QUEUED: (STATUS_IMPLEMENTING, STATUS_REJECTED, STATUS_FAILED, STATUS_SELECTING),
    STATUS_SELECTING: (STATUS_REJECTED, STATUS_IMPLEMENTING, STATUS_FAILED, STATUS_TIMED_OUT),
    STATUS_IMPLEMENTING: (STATUS_VERIFYING, STATUS_REJECTED, STATUS_FAILED, STATUS_TIMED_OUT, STATUS_CANCELLED, STATUS_INTERRUPTED),
    STATUS_VERIFYING: (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_NEEDS_REVIEW, STATUS_TIMED_OUT, STATUS_CANCELLED, STATUS_INTERRUPTED),
}

class ExperimentNotFoundError(LookupError):
    """Raised when an experiment id does not exist in the store."""


class ExperimentAlreadyActiveError(RuntimeError):
    """Raised when an issue already has a non-terminal experiment."""

    def __init__(self, experiment_id: str, status: str) -> None:
        self.experiment_id = experiment_id
        self.status = status
        super().__init__(
            f"experiment {experiment_id!r} is already active (status={status!r})"
        )


class ExperimentNotRunnableError(RuntimeError):
    """Raised when ``run_experiment`` is called on an experiment already started."""


class ExperimentNotCancellableError(RuntimeError):
    """Raised when cancellation is requested outside an active phase."""


@dataclass
class ExperimentPlan:
    """Structured feature plan as selected by the read-only selection phase.

    ``change_kind``/``required_artifacts``/``acceptance_criteria`` serve the
    deterministic quality evaluation (``experiment_evaluation``) and default to
    safe values, so older stored plans keep working: ``change_kind`` defaults to
    ``"implementation"``, the strictest kind; the lists default to empty.
    """

    title: str
    motivation: str
    affected_files: list[str]
    expected_changes: str
    success_criteria: str
    planned_tests: str
    risks: str
    estimated_scope: str
    change_kind: str = "implementation"
    required_artifacts: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "motivation": self.motivation,
            "affected_files": self.affected_files,
            "expected_changes": self.expected_changes,
            "success_criteria": self.success_criteria,
            "planned_tests": self.planned_tests,
            "risks": self.risks,
            "estimated_scope": self.estimated_scope,
            "change_kind": self.change_kind,
            "required_artifacts": self.required_artifacts,
            "acceptance_criteria": self.acceptance_criteria,
        }

@dataclass
class Experiment:
    id: str
    goal: str
    status: str
    created_at: str
    selection_started_at: str | None = None
    selection_ended_at: str | None = None
    # Coding agent that ran the implementation (codex | claude); None for
    # pre-second-agent experiments.
    implementation_provider: str | None = None
    complexity: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    implementation_started_at: str | None = None
    implementation_ended_at: str | None = None
    ended_at: str | None = None
    cancelled_at: str | None = None
    cancelled_by: str | None = None
    plan: dict[str, Any] | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    base_commit_sha: str | None = None
    research_task_id: str | None = None
    research_started_at: str | None = None
    research_ended_at: str | None = None
    research_runtime_ms: int | None = None
    research_usage: dict[str, Any] | None = None
    research_raw_summary: str | None = None
    research_diagnostic_log: str | None = None
    selection_task_id: str | None = None
    reformat_retry_task_id: str | None = None
    policy_replan_task_id: str | None = None
    implementation_task_id: str | None = None
    selection_runtime_ms: int | None = None
    reformat_retry_runtime_ms: int | None = None
    policy_replan_runtime_ms: int | None = None
    implementation_runtime_ms: int | None = None
    selection_usage: dict[str, Any] | None = None
    reformat_retry_usage: dict[str, Any] | None = None
    policy_replan_usage: dict[str, Any] | None = None
    implementation_usage: dict[str, Any] | None = None
    selection_raw_message: str | None = None
    reformat_raw_message: str | None = None
    policy_replan_raw_message: str | None = None
    selection_diagnostic_log: str | None = None
    reformat_diagnostic_log: str | None = None
    policy_replan_diagnostic_log: str | None = None
    test_command: list[str] | None = None
    test_exit_code: int | None = None
    test_output: str | None = None
    git_status: str | None = None
    diff_stat: str | None = None
    diff_text: str | None = None
    error: str | None = None
    policy_rule: str | None = None
    quality_evaluation: dict[str, Any] | None = None
    baseline_test_classifications: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "created_at": self.created_at,
            "selection_started_at": self.selection_started_at,
            "selection_ended_at": self.selection_ended_at,
            "implementation_provider": self.implementation_provider,
            "complexity": self.complexity,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "implementation_started_at": self.implementation_started_at,
            "implementation_ended_at": self.implementation_ended_at,
            "ended_at": self.ended_at,
            "cancelled_at": self.cancelled_at,
            "cancelled_by": self.cancelled_by,
            "plan": self.plan,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "base_commit_sha": self.base_commit_sha,
            "research_task_id": self.research_task_id,
            "research_started_at": self.research_started_at,
            "research_ended_at": self.research_ended_at,
            "research_runtime_ms": self.research_runtime_ms,
            "research_usage": self.research_usage,
            "research_raw_summary": self.research_raw_summary,
            "research_diagnostic_log": self.research_diagnostic_log,
            "selection_task_id": self.selection_task_id,
            "reformat_retry_task_id": self.reformat_retry_task_id,
            "policy_replan_task_id": self.policy_replan_task_id,
            "implementation_task_id": self.implementation_task_id,
            "selection_runtime_ms": self.selection_runtime_ms,
            "reformat_retry_runtime_ms": self.reformat_retry_runtime_ms,
            "policy_replan_runtime_ms": self.policy_replan_runtime_ms,
            "implementation_runtime_ms": self.implementation_runtime_ms,
            "selection_usage": self.selection_usage,
            "reformat_retry_usage": self.reformat_retry_usage,
            "policy_replan_usage": self.policy_replan_usage,
            "implementation_usage": self.implementation_usage,
            "selection_raw_message": self.selection_raw_message,
            "reformat_raw_message": self.reformat_raw_message,
            "policy_replan_raw_message": self.policy_replan_raw_message,
            "selection_diagnostic_log": self.selection_diagnostic_log,
            "reformat_diagnostic_log": self.reformat_diagnostic_log,
            "policy_replan_diagnostic_log": self.policy_replan_diagnostic_log,
            "test_command": self.test_command,
            "test_exit_code": self.test_exit_code,
            "test_output": self.test_output,
            "git_status": self.git_status,
            "diff_stat": self.diff_stat,
            "diff_text": self.diff_text,
            "error": self.error,
            "policy_rule": self.policy_rule,
            "quality_evaluation": self.quality_evaluation,
            "baseline_test_classifications": self.baseline_test_classifications,
        }


def _row_to_experiment(row: sqlite3.Row) -> Experiment:
    def _json_or_none(value: str | None) -> Any:
        return json.loads(value) if value else None

    return Experiment(
        id=row["id"],
        goal=row["goal"],
        status=row["status"],
        created_at=row["created_at"],
        selection_started_at=row["selection_started_at"],
        selection_ended_at=row["selection_ended_at"],
        implementation_provider=(
            row["implementation_provider"]
            if "implementation_provider" in row.keys()
            else None
        ),
        complexity=row["complexity"] if "complexity" in row.keys() else None,
        source_type=row["source_type"] if "source_type" in row.keys() else None,
        source_id=row["source_id"] if "source_id" in row.keys() else None,
        implementation_started_at=row["implementation_started_at"],
        implementation_ended_at=row["implementation_ended_at"],
        ended_at=row["ended_at"],
        cancelled_at=row["cancelled_at"] if "cancelled_at" in row.keys() else None,
        cancelled_by=row["cancelled_by"] if "cancelled_by" in row.keys() else None,
        plan=_json_or_none(row["plan_json"]),
        worktree_path=row["worktree_path"],
        branch_name=row["branch_name"],
        base_commit_sha=row["base_commit_sha"] if "base_commit_sha" in row.keys() else None,
        research_task_id=row["research_task_id"],
        research_started_at=row["research_started_at"],
        research_ended_at=row["research_ended_at"],
        research_runtime_ms=row["research_runtime_ms"],
        research_usage=_json_or_none(row["research_usage_json"]),
        research_raw_summary=row["research_raw_summary"],
        research_diagnostic_log=row["research_diagnostic_log"],
        selection_task_id=row["selection_task_id"],
        reformat_retry_task_id=row["reformat_retry_task_id"],
        policy_replan_task_id=row["policy_replan_task_id"],
        implementation_task_id=row["implementation_task_id"],
        selection_runtime_ms=row["selection_runtime_ms"],
        reformat_retry_runtime_ms=row["reformat_retry_runtime_ms"],
        policy_replan_runtime_ms=row["policy_replan_runtime_ms"],
        implementation_runtime_ms=row["implementation_runtime_ms"],
        selection_usage=_json_or_none(row["selection_usage_json"]),
        reformat_retry_usage=_json_or_none(row["reformat_retry_usage_json"]),
        policy_replan_usage=_json_or_none(row["policy_replan_usage_json"]),
        implementation_usage=_json_or_none(row["implementation_usage_json"]),
        selection_raw_message=row["selection_raw_message"],
        reformat_raw_message=row["reformat_raw_message"],
        policy_replan_raw_message=row["policy_replan_raw_message"],
        selection_diagnostic_log=row["selection_diagnostic_log"],
        reformat_diagnostic_log=row["reformat_diagnostic_log"],
        policy_replan_diagnostic_log=row["policy_replan_diagnostic_log"],
        test_command=_json_or_none(row["test_command_json"]),
        test_exit_code=row["test_exit_code"],
        test_output=row["test_output"],
        git_status=row["git_status"],
        diff_stat=row["diff_stat"],
        diff_text=row["diff_text"],
        error=row["error"],
        policy_rule=row["policy_rule"] if "policy_rule" in row.keys() else None,
        quality_evaluation=(
            _json_or_none(row["quality_evaluation_json"]) if "quality_evaluation_json" in row.keys() else None
        ),
        baseline_test_classifications=(
            _json_or_none(row["baseline_test_classifications_json"])
            if "baseline_test_classifications_json" in row.keys() else None
        ),
    )


