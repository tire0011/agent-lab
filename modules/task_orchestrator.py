"""Agentic local-LLM orchestration for free-form Nexus tasks.

This layer decides *what Nexus should do* before any lifecycle pipeline is
started. Existing pipelines and operations remain execution primitives.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json


ACTION_NEW_EXPERIMENT = "new_experiment"
ACTION_RESUME_EXPERIMENT = "resume_experiment"
ACTION_COMMAND = "command"
ACTION_ANSWER = "answer"
ACTION_CLARIFICATION = "clarification"
ACTION_MISSING_CAPABILITY = "missing_capability"

_FINAL_ACTIONS = {
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
    ACTION_COMMAND,
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_MISSING_CAPABILITY,
}


@dataclass(slots=True)
class TaskDecision:
    action: str
    reason: str = ""
    message: str = ""
    experiment_id: str | None = None
    command: str | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)


class TaskOrchestratorError(RuntimeError):
    pass


class NexusTaskOrchestrator:
    """Let the local Nexus model choose and refine the next workflow action.

    The model may inspect Nexus through existing read-only OperationsRegistry
    tools, receive the real tool output, and then choose again. Only after that
    loop finishes does it return a write/lifecycle action to the caller.
    """

    def __init__(
        self,
        *,
        operations_registry: Any,
        experiment_service: Any,
        llama_client: LlamaCppClient | None = None,
        max_steps: int = 4,
    ) -> None:
        self.operations_registry = operations_registry
        self.experiment_service = experiment_service
        self.llama = llama_client or LlamaCppClient(timeout=90.0)
        self.max_steps = max(1, min(max_steps, 8))

    def run(self, task: str) -> TaskDecision:
        return asyncio.run(self.run_async(task))

    async def run_async(self, task: str) -> TaskDecision:
        observations: list[dict[str, Any]] = []
        for _ in range(self.max_steps):
            raw = await self.llama.complete(
                prompt=self._prompt(task, observations),
                temperature=0.0,
                max_tokens=450,
            )
            decision = self._parse(raw)

            if decision.get("type") == "tool":
                observation = self._execute_tool(decision)
                observations.append(observation)
                continue

            action = str(decision.get("action", "")).strip().lower()
            if action not in _FINAL_ACTIONS:
                raise TaskOrchestratorError(f"unknown final action {action!r}")

            experiment_id = decision.get("experiment_id")
            if action == ACTION_RESUME_EXPERIMENT:
                if not experiment_id:
                    raise TaskOrchestratorError("resume_experiment requires experiment_id")
                try:
                    experiment_id = self.experiment_service.resolve_experiment(
                        str(experiment_id)
                    ).id
                except Exception as exc:
                    raise TaskOrchestratorError(
                        f"unknown experiment {experiment_id!r}"
                    ) from exc
            else:
                experiment_id = None

            command = str(decision.get("command", "") or "").strip() or None
            if action == ACTION_COMMAND and not command:
                raise TaskOrchestratorError("command action requires command")

            return TaskDecision(
                action=action,
                reason=str(decision.get("reason", ""))[:1000],
                message=str(decision.get("message", ""))[:2000],
                experiment_id=experiment_id,
                command=command,
                observations=observations,
            )

        return TaskDecision(
            action=ACTION_CLARIFICATION,
            reason="local orchestrator reached its bounded decision-step limit",
            message="I need one more piece of information before I can choose a reliable action.",
            observations=observations,
        )

    def _capabilities(self) -> list[dict[str, Any]]:
        operations = [spec.to_dict() for spec in self.operations_registry.catalog()]
        return [
            {
                "name": "operation",
                "description": "Execute one existing read-only Nexus operation and inspect its real result before deciding again.",
                "operations": operations,
            },
            {
                "name": ACTION_NEW_EXPERIMENT,
                "description": "Start a new implementation experiment using the existing experiment pipeline.",
            },
            {
                "name": ACTION_RESUME_EXPERIMENT,
                "description": "Resume an existing persisted experiment/worktree without creating another experiment.",
            },
            {
                "name": ACTION_COMMAND,
                "description": "Execute an explicit operational shell command through the existing trusted mailbox command primitive.",
            },
            {
                "name": ACTION_ANSWER,
                "description": "Return a direct result when inspection already answered the request and no mutation is needed.",
            },
            {
                "name": ACTION_MISSING_CAPABILITY,
                "description": "State that no available Nexus capability can complete the request and describe the missing capability.",
            },
            {
                "name": ACTION_CLARIFICATION,
                "description": "Ask one concise question only when a material ambiguity cannot be resolved with available tools/state.",
            },
        ]

    def _prompt(self, task: str, observations: list[dict[str, Any]]) -> str:
        return f"""You are Nexus, the local orchestration model. You decide WHAT Nexus does next.
Do not force every request into an experiment. Existing pipelines are tools, not the control flow.

User task:
{task}

Available capabilities:
{json.dumps(self._capabilities(), separators=(',', ':'))}

Observations from tools already used in this task:
{json.dumps(observations, separators=(',', ':'))}

Choose exactly one next step.

To inspect state first, return JSON:
{{"type":"tool","tool":"operation","operation":"<catalog operation>","args":{{...}},"reason":"..."}}

To finish routing, return JSON:
{{"type":"action","action":"new_experiment|resume_experiment|command|answer|missing_capability|clarification","experiment_id":null,"command":null,"message":"...","reason":"..."}}

Rules:
- Prefer using a read-only operation when state can resolve uncertainty.
- For a clear new code change, choose new_experiment.
- For a request to continue an existing experiment, inspect it if useful, then choose resume_experiment with its id/prefix.
- For an explicit operational command, choose command rather than creating an experiment.
- If inspection answers the request, choose answer.
- If the needed capability does not exist, choose missing_capability instead of pretending another workflow fits.
- Ask clarification only when tools/state cannot resolve a material ambiguity.
- Never invent operation names or experiment IDs.
- Return JSON only.
"""

    @staticmethod
    def _parse(raw: str) -> dict[str, Any]:
        try:
            data = extract_json(raw)
        except LlamaCppClientError as exc:
            raise TaskOrchestratorError(str(exc)) from exc
        if not isinstance(data, dict):
            raise TaskOrchestratorError("orchestrator response must be a JSON object")
        return data

    def _execute_tool(self, decision: dict[str, Any]) -> dict[str, Any]:
        if decision.get("tool") != "operation":
            raise TaskOrchestratorError(f"unknown tool {decision.get('tool')!r}")
        operation = str(decision.get("operation", "")).strip()
        args = decision.get("args") or {}
        if not isinstance(args, dict):
            raise TaskOrchestratorError("operation args must be an object")

        available = {spec.name for spec in self.operations_registry.catalog()}
        if operation not in available:
            raise TaskOrchestratorError(f"operation {operation!r} is not available")

        result = self.operations_registry.execute(
            operation,
            args,
            caller="local task orchestrator",
        )
        payload = result.to_dict()
        lines = list(payload.get("output_lines") or [])[:80]
        payload["output_lines"] = [str(line)[:500] for line in lines]
        return {
            "operation": operation,
            "args": args,
            "result": payload,
        }
