"""Local-LLM workflow selection for free-form mailbox tasks.

The mailbox task bridge must decide *which Nexus primitive to use* before it
creates an experiment.  The local model is the primary decision maker; a
small deterministic fallback exists only so mailbox processing remains
available when llama-server is temporarily unavailable.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json

if TYPE_CHECKING:
    from modules.experiment_service import ExperimentService

ACTION_NEW_EXPERIMENT = "new_experiment"
ACTION_RESUME_EXPERIMENT = "resume_experiment"
ACTION_READ_ONLY = "read_only"
ACTION_COMMAND = "command"
ACTION_MISSING_CAPABILITY = "missing_capability"
ACTION_CLARIFICATION = "clarification"

VALID_ACTIONS = frozenset(
    {
        ACTION_NEW_EXPERIMENT,
        ACTION_RESUME_EXPERIMENT,
        ACTION_READ_ONLY,
        ACTION_COMMAND,
        ACTION_MISSING_CAPABILITY,
        ACTION_CLARIFICATION,
    }
)

_HEX_ID_RE = re.compile(r"\b[0-9a-f]{8,64}\b", re.IGNORECASE)
_RESUME_KEYWORDS_RE = re.compile(
    r"\bresum(?:e|ing)\b|\bcontinu(?:e|ing)\b|\bexisting experiment\b|"
    r"\bdo not (?:re-?implement|create (?:a )?new experiment)\b|"
    r"\breuse (?:its |the )?(?:existing )?worktree\b",
    re.IGNORECASE,
)


@dataclass
class TaskIntentDecision:
    action: str
    experiment_id: str | None = None
    reason: str = ""
    message: str = ""
    command: str = ""
    source: str = "local_llm"


def _referenced_experiments(
    task_body: str, experiment_service: "ExperimentService | None"
) -> list[dict[str, str]]:
    """Resolve only ids mentioned by the user; do not dump the experiment DB."""
    if experiment_service is None:
        return []
    result: list[dict[str, str]] = []
    for candidate in sorted(set(_HEX_ID_RE.findall(task_body or "")), key=len, reverse=True):
        try:
            exp = experiment_service.resolve_experiment(candidate.lower())
        except Exception:
            continue
        item = {
            "id": exp.id,
            "status": str(exp.status),
            "worktree_path": str(exp.worktree_path or ""),
        }
        if item not in result:
            result.append(item)
    return result


def _build_prompt(task_body: str, experiments: list[dict[str, str]]) -> str:
    state = json.dumps(experiments, separators=(",", ":"))
    return f"""You are the Nexus workflow router. Decide what Nexus should do BEFORE any experiment is created.

User task:
{task_body}

Resolved experiments explicitly referenced by the task:
{state}

Available workflow choices:
- new_experiment: new implementation/change that belongs in the normal experiment pipeline.
- resume_experiment: continue a referenced existing experiment/worktree. Requires experiment_id from the resolved list.
- read_only: inspect/query existing state without changing code.
- command: execute an explicit operational command rather than implement code.
- missing_capability: the request is understood but Nexus lacks the required tool/capability.
- clarification: a material ambiguity remains and cannot be resolved from the supplied state.

Rules:
1. Natural language is the interface. Never require special user syntax.
2. If the user clearly asks to continue/reuse an existing resolved experiment, choose resume_experiment. Never create a new experiment for that.
3. Ask for clarification only when a real decision is missing; clear tasks should act autonomously.
4. missing_capability is a valid result; briefly state the capability that is missing.
5. Return JSON only with keys: action, experiment_id, reason, message, command.
6. experiment_id must be null except for resume_experiment.
7. command is optional and only for command; do not invent destructive commands from vague prose.
"""


def _parse_llm_decision(
    raw: str, experiment_service: "ExperimentService | None"
) -> TaskIntentDecision:
    data: dict[str, Any] = extract_json(raw)
    action = str(data.get("action", "")).strip().lower()
    if action not in VALID_ACTIONS:
        raise LlamaCppClientError(f"workflow router returned unknown action {action!r}")

    experiment_id = data.get("experiment_id")
    resolved_id: str | None = None
    if action == ACTION_RESUME_EXPERIMENT:
        if not experiment_id or experiment_service is None:
            raise LlamaCppClientError("resume_experiment requires an experiment_id")
        try:
            resolved_id = experiment_service.resolve_experiment(str(experiment_id)).id
        except Exception as exc:
            raise LlamaCppClientError(
                f"workflow router referenced unknown experiment {experiment_id!r}"
            ) from exc

    return TaskIntentDecision(
        action=action,
        experiment_id=resolved_id,
        reason=str(data.get("reason", ""))[:1000],
        message=str(data.get("message", ""))[:2000],
        command=str(data.get("command", ""))[:2000],
        source="local_llm",
    )


async def classify_task_intent_async(
    task_body: str,
    experiment_service: "ExperimentService | None",
    *,
    client: LlamaCppClient | None = None,
) -> TaskIntentDecision:
    """Ask the local Nexus model to select the workflow for a free-form task."""
    local_client = client or LlamaCppClient(timeout=90.0)
    prompt = _build_prompt(task_body or "", _referenced_experiments(task_body, experiment_service))
    raw = await local_client.complete(prompt=prompt, temperature=0.0, max_tokens=350)
    return _parse_llm_decision(raw, experiment_service)


def _fallback_decision(
    task_body: str, experiment_service: "ExperimentService | None", reason: str
) -> TaskIntentDecision:
    """Availability fallback only; the local LLM remains the normal router."""
    body = task_body or ""
    if experiment_service is not None and _RESUME_KEYWORDS_RE.search(body):
        for item in _referenced_experiments(body, experiment_service):
            return TaskIntentDecision(
                action=ACTION_RESUME_EXPERIMENT,
                experiment_id=item["id"],
                reason=f"local workflow router unavailable; deterministic resume fallback: {reason}",
                source="fallback",
            )
    return TaskIntentDecision(
        action=ACTION_NEW_EXPERIMENT,
        reason=f"local workflow router unavailable; preserving legacy new-experiment behavior: {reason}",
        source="fallback",
    )


def classify_task_intent(
    task_body: str,
    experiment_service: "ExperimentService | None",
    *,
    client: LlamaCppClient | None = None,
) -> TaskIntentDecision:
    """Synchronous mailbox-worker entry point for local-LLM workflow routing."""
    try:
        return asyncio.run(
            classify_task_intent_async(
                task_body,
                experiment_service,
                client=client,
            )
        )
    except (LlamaCppClientError, OSError, TimeoutError) as exc:
        return _fallback_decision(task_body, experiment_service, str(exc))
