"""Agentic local-LLM controller for free-form Nexus work.

The local model remains in control across tasks, issues, ideas and discussions.
It can inspect Nexus state, search persistent memory, consult other LLMs and
then decide what Nexus should do next. Existing pipelines remain tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json
from modules.local_llm_jobs import LocalLlmJobError, LocalLlmJobService
from modules.mailbox.development import process_command
from modules.nexus_delegation import NexusDelegationService
from modules.nexus_memory import NexusMemory
from modules.nexus_task_store import NexusTaskStore
from modules.prompt_budget import (
    CHARS_PER_TOKEN,
    PromptBudgetPlan,
    compact_observations,
    compute_budget,
    dedupe_nested_results,
    read_mem_available_kb,
    read_swap_free_kb,
    read_swap_total_kb,
    truncate_text,
)
from modules.redaction import redact_secrets
from modules.run_store import DEFAULT_DB_PATH
from modules.runtime_execution import (
    RuntimeExecutionError,
    build_python_command,
    classify_command_failure,
    corrected_command,
    resolve_python_interpreter,
)
from modules.tool_loop_guard import PersistentToolLoopGuard

ACTION_NEW_EXPERIMENT = "new_experiment"
ACTION_RESUME_EXPERIMENT = "resume_experiment"
ACTION_COMMAND = "command"
TOOL_FIND_TOOL = "find_tool"
TOOL_PYTHON_RUN = "python_run"
ACTION_ANSWER = "answer"
ACTION_CLARIFICATION = "clarification"
ACTION_MISSING_CAPABILITY = "missing_capability"

# Delegates the local controller may fall back to when its compacted prompt
# still exceeds the safe local budget. ``local_worker`` is deliberately
# excluded: it also calls the local llama-server and carries the same OOM
# risk this fallback exists to avoid.
_EXTERNAL_FALLBACK_ORDER: tuple[str, ...] = ("azure_openai", "codex_readonly")


_FINAL_ACTIONS = {
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_MISSING_CAPABILITY,
}

logger = logging.getLogger(__name__)

_PROVIDER_FALLBACKS: dict[str, tuple[str, ...]] = {
    "local_worker": ("local_worker", "azure_openai", "codex_readonly"),
    "azure_openai": ("azure_openai", "local_worker", "codex_readonly"),
    "codex_readonly": ("codex_readonly", "azure_openai", "local_worker"),
}

_ORCHESTRATOR_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["tool", "action"]},
            "tool": {"type": "string"},
            "goal": {"type": "string"},
            "operation": {"type": "string"},
            "args": {"type": "object", "additionalProperties": True},
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "provider": {"type": "string"},
            "task": {"type": "string"},
            "context": {"type": "string"},
            "command": {"type": "string"},
            "script": {"type": "string"},
            "module": {"type": "string"},
            "argv": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
            "action": {"type": "string"},
            "experiment_id": {"type": ["string", "null"]},
            "message": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["type"],
        "additionalProperties": False,
    },
}

_TOOL_RESOLVER_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["found", "not_found", "unavailable"]},
            "operation": {"type": "string"},
            "reason": {"type": "string"},
            "related_tools": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "reason"],
        "additionalProperties": False,
    },
}

_READ_ONLY_OPERATION_DENYLIST = {"inbox_refresh"}
_MAX_RESOLVER_CANDIDATES = 5
_TOOL_ACCURACY_UNKNOWN = "unknown"
_TOOL_ACCURACY_CORRECT = "correct"
_TOOL_ACCURACY_INCORRECT = "incorrect"
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
    "show",
    "the",
    "to",
    "whether",
}


@dataclass(slots=True)
class TaskDecision:
    action: str
    reason: str = ""
    message: str = ""
    experiment_id: str | None = None
    command: str | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None


class TaskOrchestratorError(RuntimeError):
    pass


class ControllerBudgetExceededError(TaskOrchestratorError):
    """Raised when a compacted controller prompt still exceeds the safe local
    budget and no external provider is available/succeeds.

    Callers must translate this into a controlled, non-crashing decision
    (``missing_capability``/needs_review) rather than invoking llama-server.
    """


class NexusTaskOrchestrator:
    """Persistent local-LLM controller that chooses Nexus's next action."""

    def __init__(
        self,
        *,
        operations_registry: Any,
        experiment_service: Any,
        llama_client: LlamaCppClient | None = None,
        memory: NexusMemory | None = None,
        delegation_service: NexusDelegationService | None = None,
        task_store: NexusTaskStore | None = None,
        llm_job_service: LocalLlmJobService | None = None,
        max_steps: int = 6,
        max_total_steps: int | None = None,
        max_identical_calls: int | None = None,
        max_non_progressing_cycles: int | None = None,
    ) -> None:
        self.operations_registry = operations_registry
        self.experiment_service = experiment_service
        job_watchdog = float(os.environ.get("NEXUS_LOCAL_LLM_JOB_WATCHDOG_SECONDS", "3600"))
        job_watchdog = max(300.0, min(job_watchdog, 7200.0))
        self.llama = llama_client or LlamaCppClient(timeout=job_watchdog)
        self.max_steps = max(1, min(max_steps, 10))
        db_path = getattr(operations_registry, "db_path", DEFAULT_DB_PATH)
        self.loop_guard = PersistentToolLoopGuard(
            db_path=db_path,
            max_total_steps=max_total_steps or int(os.environ.get("NEXUS_ORCHESTRATOR_MAX_TOTAL_STEPS", "20")),
            max_identical_calls=max_identical_calls or int(os.environ.get("NEXUS_ORCHESTRATOR_MAX_IDENTICAL_CALLS", "3")),
            max_non_progressing_cycles=max_non_progressing_cycles or int(os.environ.get("NEXUS_ORCHESTRATOR_MAX_CYCLES", "2")),
        )
        self.llm_jobs = llm_job_service or LocalLlmJobService(db_path=db_path, llama_client=self.llama)
        self._owns_llm_jobs = llm_job_service is None
        repo_root = str(getattr(operations_registry, "repo_root", "/agent"))
        self.task_store = task_store or NexusTaskStore(db_path=db_path)
        self._owns_task_store = task_store is None
        self.memory = memory or NexusMemory(db_path=db_path, repo_root=repo_root, task_store=self.task_store)
        self._owns_memory = memory is None
        self.delegation = delegation_service or NexusDelegationService(
            llama_client=self.llama, db_path=db_path, repo_root=repo_root
        )
        self._owns_delegation = delegation_service is None

    def close(self) -> None:
        self.loop_guard.close()
        if self._owns_llm_jobs:
            self.llm_jobs.close()
        if self._owns_delegation:
            self.delegation.close()
        if self._owns_memory:
            self.memory.close()
        if self._owns_task_store:
            self.task_store.close()

    def run(
        self,
        task: str,
        *,
        source_type: str = "task",
        source_id: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
        task_logger: Any | None = None,
    ) -> TaskDecision:
        return asyncio.run(
            self.run_async(
                task,
                source_type=source_type,
                source_id=source_id,
                title=title,
                session_id=session_id,
                task_logger=task_logger,
            )
        )

    def continue_session(
        self,
        session_id: str,
        user_message: str,
        *,
        task_logger: Any | None = None,
    ) -> TaskDecision:
        session = self.task_store.get(session_id)
        if session is None:
            raise TaskOrchestratorError(f"unknown Nexus task session {session_id!r}")
        return self.run(
            user_message,
            source_type=session.source_type,
            source_id=session.source_id,
            title=session.title,
            session_id=session.id,
            task_logger=task_logger,
        )

    async def run_async(
        self,
        task: str,
        *,
        source_type: str = "task",
        source_id: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
        task_logger: Any | None = None,
    ) -> TaskDecision:
        if not task or not task.strip():
            raise TaskOrchestratorError("task must not be empty")
        session = self.task_store.get_or_create(
            source_type=source_type,
            source_id=source_id,
            title=title,
            session_id=session_id,
        )
        self.task_store.set_status(session.id, "active")
        # This event id anchors "prior" history: persisted history is never
        # re-read past this point, since every tool observation produced by
        # this run is already tracked (and budgeted) via ``observations``
        # in-memory. Without this boundary, persisted history and in-memory
        # observations duplicate each other and the prompt grows on every step.
        run_start_event_id = self.task_store.append_event(
            session.id,
            role="user",
            kind="input",
            content=task.strip(),
            metadata={"source_type": session.source_type, "source_id": session.source_id},
        )
        observations: list[dict[str, Any]] = []

        for _ in range(self.max_steps):
            state_token = self._state_token(task)
            within_budget, step = self.loop_guard.claim_step(session.id, state_token)
            if not within_budget:
                return self._loop_limit_result(
                    session.id,
                    task,
                    observations,
                    f"persistent orchestration step limit reached ({step - 1})",
                    task_logger,
                )
            budget_plan = compute_budget(
                mem_available_kb=read_mem_available_kb(),
                swap_free_kb=read_swap_free_kb(),
                swap_total_kb=read_swap_total_kb(),
            )
            history = self.task_store.history(
                session.id,
                limit=18,
                max_chars=budget_plan.history_chars,
                max_event_id=run_start_event_id,
            )
            prompt, prompt_meta = self._build_prompt(session, task, history, observations, budget_plan)
            logger.info(
                "controller_prompt_budget session_id=%s prompt_chars=%s estimated_tokens=%s "
                "history_events=%s observation_count=%s omitted_observations=%s %s",
                session.id,
                len(prompt),
                len(prompt) // CHARS_PER_TOKEN,
                len(history),
                len(observations),
                prompt_meta["omitted_observation_indices"],
                budget_plan.as_log_fields(),
            )
            logger.info(
                "llm_provider selected provider=local role=controller session_id=%s reason=persistent_controller",
                session.id,
            )
            try:
                raw, provider_used = await self._controller_completion(
                    prompt,
                    session_id=session.id,
                    max_tokens=550,
                    budget_plan=budget_plan,
                    task_logger=task_logger,
                )
            except ControllerBudgetExceededError as exc:
                return self._budget_exceeded_result(
                    session.id, task, observations, str(exc), task_logger
                )
            if provider_used != "local":
                logger.warning(
                    "controller_external_fallback session_id=%s provider_used=%s prompt_chars=%s "
                    "safe_local_budget_chars=%s",
                    session.id,
                    provider_used,
                    len(prompt),
                    budget_plan.safe_local_budget_chars,
                )
            try:
                decision = self._parse(raw)
            except TaskOrchestratorError as first_error:
                logger.warning(
                    "controller JSON invalid session_id=%s; requesting one correction: %s",
                    session.id,
                    str(first_error).splitlines()[0][:500],
                )
                correction_prompt = self._json_correction_prompt(raw, first_error)
                try:
                    corrected_raw, _ = await self._controller_completion(
                        correction_prompt,
                        session_id=session.id,
                        max_tokens=650,
                        budget_plan=budget_plan,
                        task_logger=task_logger,
                    )
                except ControllerBudgetExceededError as exc:
                    return self._budget_exceeded_result(
                        session.id, task, observations, str(exc), task_logger
                    )
                try:
                    decision = self._parse(corrected_raw)
                except TaskOrchestratorError as correction_error:
                    raise TaskOrchestratorError(
                        "controller returned invalid JSON and its single correction "
                        f"retry also failed: {correction_error}"
                    ) from correction_error
            # Small local models occasionally emit the command capability with
            # the final-action envelope (``action: command``) even though the
            # prompt explicitly defines commands as tools. Treat that shape as
            # the command tool instead of failing the whole persistent task.
            if decision.get("type") != "tool" and str(decision.get("action", "")).strip().lower() == ACTION_COMMAND:
                decision = dict(decision)
                decision["type"] = "tool"
                decision["tool"] = ACTION_COMMAND
            if decision.get("type") == "tool":
                decision = self._normalize_tool_decision(decision)
                if task_logger is not None:
                    task_logger.info(
                        "orchestration",
                        "tool_selected",
                        (
                            "orchestrator selected intermediate tool "
                            f"{decision.get('tool', '')}: {decision.get('reason', '')}"
                        ),
                        tool=str(decision.get("tool", "")),
                        reason=str(decision.get("reason", "")),
                        session_id=session.id,
                    )
                tool, arguments = self._tool_identity(decision)
                check = self.loop_guard.check(
                    session.id,
                    tool,
                    arguments,
                    state_token=state_token,
                    reason=str(decision.get("reason", "")),
                )
                if not check.allowed:
                    observation = check.observation or {"event": "duplicate_tool_call"}
                    logger.warning(
                        "tool loop detected session_id=%s signature=%s previous_call_id=%s recovery=%s",
                        session.id,
                        check.signature,
                        observation.get("previous_call_id"),
                        observation.get("recovery"),
                        extra={"session_id": session.id, "event": "tool_loop_detected"},
                    )
                    if task_logger is not None:
                        task_logger.warning(
                            "orchestration",
                            str(observation.get("event", "duplicate_tool_call")),
                            (
                                f"tool loop detected signature={check.signature} "
                                f"previous_call_id={observation.get('previous_call_id')} "
                                f"recovery={observation.get('recovery')}"
                            ),
                            session_id=session.id,
                            signature=check.signature,
                            previous_call_id=str(observation.get("previous_call_id", "")),
                            recovery=str(observation.get("recovery", "")),
                        )
                    if check.stop:
                        return self._loop_limit_result(
                            session.id,
                            task,
                            observations + [observation],
                            f"tool loop limit reached after {observation.get('event')}",
                            task_logger,
                        )
                else:
                    execution_decision = dict(decision)
                    execution_decision["session_id"] = session.id
                    observation = await self._execute_tool(
                        execution_decision,
                        budget_plan=budget_plan,
                        task_logger=task_logger,
                    )
                    self.loop_guard.record_result(check.call_id, observation)
                # Persist the full raw observation so the durable event_id can
                # later retrieve the complete tool result; only the
                # prompt-facing view (compact_observations) is truncated.
                event_id = self.task_store.append_event(
                    session.id,
                    role="tool",
                    kind=str(observation.get("tool", "tool")),
                    content=json.dumps(observation, separators=(",", ":")),
                    metadata={"reason": str(decision.get("reason", ""))[:1000]},
                )
                observation = dict(observation)
                observation["event_id"] = event_id
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
                    experiment_id = self.experiment_service.resolve_experiment(str(experiment_id)).id
                except Exception as exc:
                    raise TaskOrchestratorError(f"unknown experiment {experiment_id!r}") from exc
            else:
                experiment_id = None

            if action == ACTION_NEW_EXPERIMENT:
                logger.info(
                    "llm_provider selected provider=codex role=developer session_id=%s reason=%s",
                    session.id,
                    str(decision.get("reason", ""))[:500],
                )

            result = TaskDecision(
                action=action,
                reason=str(decision.get("reason", ""))[:1000],
                message=str(decision.get("message", ""))[:3000],
                experiment_id=experiment_id,
                observations=observations,
                session_id=session.id,
            )
            if task_logger is not None:
                task_logger.info(
                    "orchestration",
                    "route_selected",
                    f"orchestrator selected route {result.action}: {result.reason}",
                    action=result.action,
                    reason=result.reason,
                    session_id=session.id,
                )
            self._persist_final(session.id, task, result)
            return result

        result = TaskDecision(
            action=ACTION_CLARIFICATION,
            reason="local orchestrator reached its bounded decision-step limit",
            message="I need one more piece of information before I can choose a reliable action.",
            observations=observations,
            session_id=session.id,
        )
        if task_logger is not None:
            task_logger.warning(
                "orchestration",
                "route_selected",
                "orchestrator requested clarification after reaching its decision-step limit",
                action=result.action,
                reason=result.reason,
                session_id=session.id,
            )
        self._persist_final(session.id, task, result)
        return result

    async def _controller_completion(
        self,
        prompt: str,
        *,
        session_id: str,
        max_tokens: int,
        budget_plan: "PromptBudgetPlan | None" = None,
        task_logger: Any | None = None,
    ) -> tuple[str, str]:
        """Run one controller completion.

        Returns ``(raw_response, provider_used)``. When ``budget_plan`` shows
        the compacted prompt still exceeds the safe local budget, llama-server
        is never invoked; the compact package is delegated to an available
        external provider instead so a persistent session cannot OOM the
        local model host.
        """
        return await self._schema_completion(
            prompt,
            session_id=session_id,
            max_tokens=max_tokens,
            response_format=_ORCHESTRATOR_RESPONSE_FORMAT,
            budget_plan=budget_plan,
            waiting_status="waiting_for_llm",
            role="controller",
            task_logger=task_logger,
        )

    async def _external_fallback_completion(
        self,
        prompt: str,
        *,
        session_id: str,
        budget_plan: "PromptBudgetPlan",
        task_logger: Any | None,
        role: str = "controller",
    ) -> tuple[str, str]:
        """Delegate a compact, still-over-budget local inference externally.

        Never falls back to ``local_worker``: that delegate also calls the
        same local llama-server and would carry the same OOM risk this
        fallback exists to avoid.
        """
        available = {item["provider"] for item in self.delegation.catalog()}
        candidates = [p for p in _EXTERNAL_FALLBACK_ORDER if p in available]
        if not candidates:
            raise ControllerBudgetExceededError(
                "compact controller prompt still exceeds the safe local budget "
                f"({len(prompt)} chars > {budget_plan.safe_local_budget_chars}) and no "
                "external provider is configured; refusing to risk a local OOM"
            )
        if role == "tool_resolver":
            task_text = (
                "Continue as the Nexus tool resolver for this turn only. "
                "Return exactly one JSON object matching the resolver "
                "schema/instructions below; no Markdown, no prose.\n\n" + prompt
            )[:16000]
        else:
            task_text = (
                "Continue as the Nexus persistent controller for this turn only. "
                "Return exactly one JSON object matching the schema/instructions below; "
                "no Markdown, no prose.\n\n" + prompt
            )[:16000]
        attempts: list[str] = []
        for provider in candidates:
            result = await self.delegation.delegate(provider, task_text, context="")
            payload = result.to_dict()
            attempts.append(provider)
            if task_logger is not None:
                task_logger.info(
                    "orchestration",
                    f"{role}_external_fallback",
                    f"provider={provider} success={payload.get('success')}",
                    session_id=session_id,
                    provider=provider,
                )
            if payload.get("success"):
                logger.info(
                    "llm_provider completed provider=%s role=%s_fallback session_id=%s "
                    "prompt_chars=%s safe_local_budget_chars=%s",
                    provider,
                    role,
                    session_id,
                    len(prompt),
                    budget_plan.safe_local_budget_chars,
                )
                return str(payload.get("content", "")), provider
        raise ControllerBudgetExceededError(
            "compact controller prompt still exceeds the safe local budget and all "
            f"external providers failed: {attempts}"
        )

    async def _schema_completion(
        self,
        prompt: str,
        *,
        session_id: str,
        max_tokens: int,
        response_format: dict[str, Any],
        budget_plan: "PromptBudgetPlan | None" = None,
        waiting_status: str = "waiting_for_llm",
        role: str = "controller",
        task_logger: Any | None = None,
    ) -> tuple[str, str]:
        if budget_plan is not None and len(prompt) > budget_plan.safe_local_budget_chars:
            logger.warning(
                "%s_budget_exceeded session_id=%s prompt_chars=%s safe_local_budget_chars=%s "
                "low_memory=%s mem_available_kb=%s",
                role,
                session_id,
                len(prompt),
                budget_plan.safe_local_budget_chars,
                budget_plan.low_memory,
                budget_plan.mem_available_kb,
            )
            return await self._external_fallback_completion(
                prompt,
                session_id=session_id,
                budget_plan=budget_plan,
                task_logger=task_logger,
                role=role,
            )
        job_id = self.llm_jobs.submit(
            prompt,
            temperature=0.0,
            max_tokens=max_tokens,
            session_id=session_id,
            response_format=response_format,
        )
        self.task_store.set_status(session_id, waiting_status)
        try:
            return await self.llm_jobs.wait(job_id), "local"
        except LocalLlmJobError as exc:
            raise TaskOrchestratorError(f"local LLM job failed: {exc}") from exc
        finally:
            self.task_store.set_status(session_id, "active")

    @staticmethod
    def _json_correction_prompt(
        raw: str,
        error: TaskOrchestratorError,
    ) -> str:
        return (
            "Your previous Nexus controller response was not valid JSON. "
            "Correct syntax only and preserve the intended next step. "
            "Return exactly one JSON object with no Markdown or prose.\n\n"
            f"VALIDATION ERROR:\n{str(error)[:1000]}\n\n"
            f"REQUIRED JSON SCHEMA:\n"
            f"{json.dumps(_ORCHESTRATOR_RESPONSE_FORMAT['schema'], separators=(',', ':'))}\n\n"
            f"INVALID RESPONSE:\n{raw[:6000]}"
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {
            token for token in re.findall(r"[a-z0-9]+", text.lower())
            if len(token) > 1 and token not in _TOOL_SEARCH_STOP_WORDS
        }

    def _read_only_operation_specs(self) -> list[Any]:
        return [
            spec for spec in self.operations_registry.catalog()
            if spec.name not in _READ_ONLY_OPERATION_DENYLIST
        ]

    def _candidate_operations(self, goal: str) -> list[Any]:
        goal_tokens = self._tokens(goal)
        scored: list[tuple[int, str, Any]] = []
        for spec in self._read_only_operation_specs():
            tags = " ".join(str(tag) for tag in getattr(spec, "tags", []) or [])
            haystack = f"{spec.name} {spec.description} {tags}"
            haystack_tokens = self._tokens(haystack)
            score = len(goal_tokens & haystack_tokens)
            if spec.name.lower() in goal.lower():
                score += 5
            for token in goal_tokens:
                if token and token in spec.name.lower():
                    score += 2
            if score > 0:
                scored.append((score, spec.name, spec))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [spec for _, _, spec in scored[:_MAX_RESOLVER_CANDIDATES]]

    def _latest_operation_grant(self, session_id: str, operation: str) -> dict[str, Any] | None:
        if not session_id or not operation:
            return None
        recent_events = getattr(self.task_store, "recent_events", None)
        if not callable(recent_events):
            return None
        for event in recent_events(
            session_id,
            role="tool",
            kind=TOOL_FIND_TOOL,
            limit=50,
        ):
            try:
                payload = json.loads(event["content"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("status") != "found":
                continue
            schema = payload.get("operation")
            if not isinstance(schema, dict):
                continue
            if str(schema.get("operation", "")).strip() != operation:
                continue
            return {
                "event_id": int(event["id"]),
                "operation": operation,
                "schema": schema,
            }
        return None

    def _resolver_prompt(self, goal: str, candidates: list[Any]) -> str:
        candidate_summaries = [
            {
                "operation": spec.name,
                "description": spec.description,
                "arguments": [
                    argument.to_dict()
                    for argument in getattr(spec, "arguments", [])
                ],
                "tags": list(getattr(spec, "tags", []) or []),
            }
            for spec in candidates
        ]
        return f"""TOOL RESOLVER MODE.
You are resolving exactly one requested Nexus capability against a small candidate list.
Return exactly one JSON object. Do not plan the task. Do not implement missing tools.

GOAL:
{goal[:1000]}

CANDIDATE READ-ONLY OPERATIONS:
{json.dumps(candidate_summaries, separators=(',', ':'))}

Rules:
- Choose status "found" only when one candidate directly satisfies the goal.
- Choose status "not_found" when none is suitable.
- If found, set operation to the exact candidate operation name.
- If not_found, leave operation empty and list at most three related candidate names.
"""

    @staticmethod
    def _selection_correctness(
        *,
        status: str,
        selected_operation: str | None,
        expected_operation: str | None,
        expected_status: str | None,
    ) -> str:
        if status == "unavailable":
            return _TOOL_ACCURACY_UNKNOWN
        if not expected_operation and not expected_status:
            return _TOOL_ACCURACY_UNKNOWN
        if expected_status == "not_found":
            return (
                _TOOL_ACCURACY_CORRECT
                if status == "not_found" and not selected_operation
                else _TOOL_ACCURACY_INCORRECT
            )
        if expected_operation:
            return (
                _TOOL_ACCURACY_CORRECT
                if status == "found" and selected_operation == expected_operation
                else _TOOL_ACCURACY_INCORRECT
            )
        return _TOOL_ACCURACY_UNKNOWN

    async def _resolve_find_tool(
        self,
        goal: str,
        *,
        session_id: str,
        budget_plan: PromptBudgetPlan | None = None,
        task_logger: Any | None = None,
        expected_operation: str | None = None,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        del budget_plan  # resolver must recompute current memory before its own inference
        clean_goal = goal.strip()[:1000]
        if not clean_goal:
            raise TaskOrchestratorError("find_tool requires goal")
        started = time.perf_counter()
        legacy_catalog_schema_chars = len(
            json.dumps(
                [spec.to_dict() for spec in self.operations_registry.catalog()],
                separators=(",", ":"),
            )
        )
        candidates = self._candidate_operations(clean_goal)
        if not candidates:
            return {
                "tool": TOOL_FIND_TOOL,
                "status": "not_found",
                "goal": clean_goal,
                "reason": "No read-only OperationsRegistry candidate matched the requested capability.",
                "related_tools": [],
                "metrics": {
                    "candidate_count": 0,
                    "candidate_operations": [],
                    "main_selected_schema_chars": 0,
                    "legacy_catalog_schema_chars": legacy_catalog_schema_chars,
                    "resolver_input_chars": 0,
                    "resolver_output_chars": 0,
                    "resolver_llm_calls": 0,
                    "selected_operation": None,
                    "expected_operation": expected_operation,
                    "expected_status": expected_status,
                    "selection_correctness": self._selection_correctness(
                        status="not_found",
                        selected_operation=None,
                        expected_operation=expected_operation,
                        expected_status=expected_status,
                    ),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                },
            }
        prompt = self._resolver_prompt(clean_goal, candidates)
        resolver_budget_plan = compute_budget(
            mem_available_kb=read_mem_available_kb(),
            swap_free_kb=read_swap_free_kb(),
            swap_total_kb=read_swap_total_kb(),
        )
        try:
            raw, provider_used = await self._schema_completion(
                prompt,
                session_id=session_id,
                max_tokens=250,
                response_format=_TOOL_RESOLVER_RESPONSE_FORMAT,
                budget_plan=resolver_budget_plan,
                waiting_status="waiting_for_tool_resolver",
                role="tool_resolver",
                task_logger=task_logger,
            )
            resolver_unavailable = False
        except ControllerBudgetExceededError as exc:
            raw = json.dumps(
                {
                    "status": "unavailable",
                    "operation": "",
                    "reason": str(exc)[:500],
                    "related_tools": [spec.name for spec in candidates[:3]],
                },
                separators=(",", ":"),
            )
            provider_used = "none"
            resolver_unavailable = True
        except TaskOrchestratorError as exc:
            raw = json.dumps(
                {
                    "status": "unavailable",
                    "operation": "",
                    "reason": f"Tool resolver LLM job failed: {str(exc)[:500]}",
                    "related_tools": [spec.name for spec in candidates[:3]],
                },
                separators=(",", ":"),
            )
            provider_used = "local"
            resolver_unavailable = True

        candidate_by_name = {spec.name: spec for spec in candidates}
        try:
            parsed = extract_json(raw)
        except LlamaCppClientError:
            parsed = {
                "status": "unavailable",
                "operation": "",
                "reason": "Tool resolver returned invalid JSON.",
                "related_tools": [spec.name for spec in candidates[:3]],
            }
            resolver_unavailable = True
        if not isinstance(parsed, dict):
            parsed = {
                "status": "unavailable",
                "operation": "",
                "reason": "Tool resolver response was not a JSON object.",
                "related_tools": [spec.name for spec in candidates[:3]],
            }
            resolver_unavailable = True
        status = str(parsed.get("status", "")).strip()
        operation = str(parsed.get("operation", "")).strip()
        reason = str(parsed.get("reason", "")).strip()[:1000] or "Tool resolver did not provide a reason."
        resolver_output_chars = len(raw)
        resolver_llm_calls = 0 if provider_used == "none" else 1
        if status == "found" and operation in candidate_by_name:
            selected = candidate_by_name[operation].to_dict()
            return {
                "tool": TOOL_FIND_TOOL,
                "status": "found",
                "goal": clean_goal,
                "operation": selected,
                "reason": reason,
                "metrics": {
                    "candidate_count": len(candidates),
                    "candidate_operations": [spec.name for spec in candidates],
                    "main_selected_schema_chars": len(
                        json.dumps(selected, separators=(",", ":"))
                    ),
                    "legacy_catalog_schema_chars": legacy_catalog_schema_chars,
                    "resolver_input_chars": len(prompt),
                    "resolver_output_chars": resolver_output_chars,
                    "resolver_llm_calls": resolver_llm_calls,
                    "resolver_provider": provider_used,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "selected_operation": operation,
                    "expected_operation": expected_operation,
                    "expected_status": expected_status,
                    "selection_correctness": self._selection_correctness(
                        status="found",
                        selected_operation=operation,
                        expected_operation=expected_operation,
                        expected_status=expected_status,
                    ),
                },
            }
        related = parsed.get("related_tools")
        if not isinstance(related, list):
            related = [spec.name for spec in candidates[:3]]
        related_tools = [
            str(item) for item in related
            if str(item) in candidate_by_name
        ][:3]
        final_status = "not_found" if status == "not_found" and not resolver_unavailable else "unavailable"
        final_reason = (
            reason
            if final_status == "not_found" or resolver_unavailable
            else f"Resolver selected unavailable operation {operation!r}."
        )
        return {
            "tool": TOOL_FIND_TOOL,
            "status": final_status,
            "goal": clean_goal,
            "reason": final_reason,
            "related_tools": related_tools,
            "metrics": {
                "candidate_count": len(candidates),
                "candidate_operations": [spec.name for spec in candidates],
                "main_selected_schema_chars": 0,
                "legacy_catalog_schema_chars": legacy_catalog_schema_chars,
                "resolver_input_chars": len(prompt),
                "resolver_output_chars": resolver_output_chars,
                "resolver_llm_calls": resolver_llm_calls,
                "resolver_provider": provider_used,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "selected_operation": operation or None,
                "expected_operation": expected_operation,
                "expected_status": expected_status,
                "selection_correctness": self._selection_correctness(
                    status=final_status,
                    selected_operation=operation or None,
                    expected_operation=expected_operation,
                    expected_status=expected_status,
                ),
            },
        }

    def _capabilities(self) -> list[dict[str, Any]]:
        legacy_catalog_chars = len(
            json.dumps(
                [spec.to_dict() for spec in self.operations_registry.catalog()],
                separators=(",", ":"),
            )
        )
        return [
            {
                "name": TOOL_FIND_TOOL,
                "description": (
                    "Resolve one needed read-only Nexus operation by goal. Returns either "
                    "one full operation schema or structured not_found; it never "
                    "implements missing tools."
                ),
                "arguments": {"goal": "plain-language capability needed"},
                "legacy_catalog_schema_chars": legacy_catalog_chars,
            },
            {
                "name": "operation",
                "description": (
                    "Execute one read-only Nexus operation only after find_tool has "
                    "returned that operation's schema in this session."
                ),
                "arguments": {
                    "operation": "operation name from the latest find_tool result",
                    "args": "object matching that schema",
                },
            },
            {
                "name": "memory_search",
                "description": (
                    "Search Nexus memory: structured knowledge, previous task/dialog "
                    "history and project memory documents."
                ),
                "arguments": {"query": "string", "limit": "1..10"},
            },
            {
                "name": "delegate",
                "description": (
                    "Ask another bounded LLM worker for analysis/research/review. "
                    "Provider failures automatically fall back to another available "
                    "model and control then returns to Nexus."
                ),
                "providers": self.delegation.catalog(),
                "arguments": {
                    "provider": "provider name",
                    "task": "sub-problem",
                    "context": "optional compact context",
                },
            },
            {
                "name": ACTION_COMMAND,
                "description": (
                    "Local development tool: execute one shell command through the "
                    "trusted development command primitive, observe its real output, "
                    "then keep control and decide again."
                ),
                "arguments": {"command": "shell command"},
            },
            {
                "name": TOOL_PYTHON_RUN,
                "description": (
                    "Run project Python deterministically: Nexus resolves its own "
                    "interpreter, so never write an interpreter name or path yourself. "
                    "Give either a repository-relative script or an importable module "
                    "plus arguments."
                ),
                "arguments": {
                    "script": "repository-relative .py path",
                    "module": "importable module name",
                    "argv": "list of arguments",
                },
            },
            {"name": ACTION_NEW_EXPERIMENT, "description": "Codex development path: start a new isolated implementation experiment for large/complex repository changes."},
            {"name": ACTION_RESUME_EXPERIMENT, "description": "Resume an existing persisted experiment/worktree without allocating another experiment."},
            {"name": ACTION_ANSWER, "description": "Finish directly when Nexus itself or its tool/delegate observations have solved the request."},
            {"name": ACTION_MISSING_CAPABILITY, "description": "State that no available capability can complete the request and record a structured requirement."},
            {"name": ACTION_CLARIFICATION, "description": "Pause the persistent task and ask one concise question when a material ambiguity/information gap cannot be resolved with tools or memory."},
        ]

    def _build_prompt(
        self,
        session: Any,
        current_input: str,
        history: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        budget_plan: PromptBudgetPlan,
    ) -> tuple[str, dict[str, Any]]:
        """Compact observations/current input to fit ``budget_plan`` and render.

        Prevents nested duplication (e.g. a ``memory_search`` observation
        surfacing content already present in persisted history or in an
        earlier observation of this same run) and deterministically bounds
        any individually oversized observation before the next model call.
        """
        truncated_input, input_truncated = truncate_text(current_input, budget_plan.current_input_chars)
        seen: set[str] = set()
        for event in history:
            content = str(event.get("content", "")).strip()[:400]
            if content:
                seen.add(content)
        deduped_observations = dedupe_nested_results(observations, already_seen=seen)
        compacted_observations, omitted_indices = compact_observations(
            deduped_observations,
            budget_chars=budget_plan.observation_chars,
            keep_recent_full=2,
        )
        prompt = self._prompt(session, truncated_input, history, compacted_observations)
        meta = {
            "input_truncated": input_truncated,
            "omitted_observation_indices": omitted_indices,
            "deduped_observation_count": sum(
                int(obs.get("deduped", 0)) for obs in deduped_observations if isinstance(obs, dict)
            ),
        }
        return prompt, meta

    def _prompt(self, session: Any, current_input: str, history: list[dict[str, Any]], observations: list[dict[str, Any]]) -> str:
        return f"""You are Nexus, the LOCAL persistent controller. You decide WHAT Nexus does next.
You are not merely an intent classifier. Work on the request yourself when practical, use tools and memory, consult other LLMs when that is valuable, and keep control after every observation.

SOURCE:
{json.dumps({'type': session.source_type, 'id': session.source_id, 'title': session.title}, separators=(',', ':'))}

CURRENT INPUT:
{current_input}

PERSISTENT TASK/DISCUSSION HISTORY:
{json.dumps(history, separators=(',', ':'))}

AVAILABLE CAPABILITIES:
{json.dumps(self._capabilities(), separators=(',', ':'))}

OBSERVATIONS FROM TOOLS USED IN THIS RUN:
{json.dumps(observations, separators=(',', ':'))}

Choose exactly one next step.
Tool calls:
{{"type":"tool","tool":"find_tool","goal":"plain-language capability needed","reason":"..."}}
{{"type":"tool","tool":"operation","operation":"<operation returned by find_tool>","args":{{...}},"reason":"..."}}
{{"type":"tool","tool":"memory_search","query":"...","limit":6,"reason":"..."}}
{{"type":"tool","tool":"delegate","provider":"local_worker|codex_readonly|azure_openai","task":"bounded sub-problem","context":"optional","reason":"..."}}
{{"type":"tool","tool":"command","command":"<shell command>","reason":"..."}}
{{"type":"tool","tool":"python_run","script":"<repo-relative .py>","argv":["--version"],"reason":"..."}}
Final action:
{{"type":"action","action":"new_experiment|resume_experiment|answer|missing_capability|clarification","experiment_id":null,"message":"...","reason":"..."}}

Model policy:
- LOCAL is a real development path, not merely a fallback. For small/simple changes, inspect the repository and implement with the command tool directly when practical.
- Prefer LOCAL for simple analysis, small fixes, inexpensive work, and whenever external providers are unavailable or quota/token limited.
- Prefer `azure_openai` for research, difficult reasoning and medium programming work. Azure returns specialist guidance; Nexus keeps control and can apply the resulting implementation with local command tools.
- Prefer `new_experiment` (Codex implementation) for large, broad or complex repository changes where an isolated workspace-writing worker is justified.
- Do not choose Codex merely because it is strongest. Use the cheapest capable route first.
- A failed delegate is not a final failure: provider fallback is automatic. Inspect which provider actually succeeded and continue from that observation.

Rules:
- The source may be a task, GitHub issue, idea or a reply in an ongoing discussion; treat all as work to understand and advance.
- Search memory when previous project decisions, experiments, discussions or ideas may materially help; do not search mechanically.
- Use find_tool when you need an OperationsRegistry capability that has not already been resolved in this session; do not guess operation names.
- Prefer cheap/local reasoning and existing tools first. Delegate only when another model materially improves a difficult analysis, review or research step.
- Delegation is a tool call, never a transfer of control. Inspect its result and decide again.
- Commands are tool calls, never final actions. Inspect command output and decide again before finishing.
- Use `python_run` for every Python execution; Nexus supplies the interpreter. Never write an interpreter name or path in a command.
- When an observation reports command_not_found, do not repeat the command and do not repeat memory searches: switch to `python_run`, another capability, or the missing_capability action.
- For an existing experiment, inspect state if useful and select resume_experiment with its real id/prefix.
- Solve small requests with reasoning/operations/commands where practical, then answer instead of creating an experiment.
- Ask clarification only when missing information materially changes the result and cannot be obtained from tools or memory; the session persists for the reply.
- If a capability is genuinely missing, choose missing_capability and describe exactly what is needed.
- Never invent tool names, provider names, operation results or experiment IDs.
- When an observation reports duplicate_tool_call, non_progressing_cycle or retry_reason_required, do not repeat it. Choose a materially different tool or a controlled final action.
- Return JSON only.
"""

    def _normalize_tool_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        tool = str(decision.get("tool", "")).strip()
        available_operations = {spec.name for spec in self._read_only_operation_specs()}
        if tool in available_operations:
            normalized = dict(decision)
            normalized["tool"] = "operation"
            normalized["operation"] = tool
            return normalized
        return decision

    def _tool_identity(self, decision: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        tool = str(decision.get("tool", "")).strip()
        if tool == "operation":
            return tool, {
                "operation": str(decision.get("operation", "")).strip(),
                "args": decision.get("args") or {},
            }
        if tool == "memory_search":
            try:
                limit = int(decision.get("limit", 6))
            except (TypeError, ValueError):
                limit = 6
            return tool, {"query": str(decision.get("query", "")).strip(), "limit": limit}
        if tool == TOOL_FIND_TOOL:
            return tool, {"goal": str(decision.get("goal", "")).strip()}
        if tool == "delegate":
            return tool, {
                "provider": str(decision.get("provider", "")).strip(),
                "task": str(decision.get("task", "")).strip(),
                "context": str(decision.get("context", "")),
            }
        if tool == ACTION_COMMAND:
            return tool, {"command": str(decision.get("command", "")).strip()}
        if tool == TOOL_PYTHON_RUN:
            # The working directory changes module/script resolution, so two
            # otherwise identical python_run calls in different directories are
            # materially different calls and must not collapse into one
            # duplicate-tool-call signature.
            try:
                cwd = str(self._resolve_cwd(decision.get("cwd")))
            except TaskOrchestratorError:
                cwd = str(decision.get("cwd", "")).strip()
            return tool, {
                "script": str(decision.get("script", "")).strip(),
                "module": str(decision.get("module", "")).strip(),
                "argv": [str(item) for item in (decision.get("argv") or [])],
                "cwd": cwd,
            }
        return tool, dict(decision.get("args") or {})

    def _state_token(self, current_input: str) -> str:
        root = Path(getattr(self.operations_registry, "repo_root", "/agent"))
        repo_state = "not-a-git-repository"
        try:
            completed = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if completed.returncode == 0 and head.returncode == 0:
                repo_state = f"{head.stdout.strip()}\n{completed.stdout}"
        except (OSError, subprocess.SubprocessError):
            pass
        payload = f"{current_input.strip()}\0{repo_state}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _loop_limit_result(
        self,
        session_id: str,
        task: str,
        observations: list[dict[str, Any]],
        reason: str,
        task_logger: Any | None,
    ) -> TaskDecision:
        result = TaskDecision(
            action=ACTION_CLARIFICATION,
            reason=reason,
            message="Nexus stopped a non-progressing tool loop. Choose a different tool or provide changed state before retrying.",
            observations=observations,
            session_id=session_id,
        )
        if task_logger is not None:
            task_logger.warning(
                "orchestration",
                "tool_loop_stopped",
                reason,
                action=result.action,
                reason=reason,
                session_id=session_id,
            )
        self._persist_final(session_id, task, result)
        return result

    def _budget_exceeded_result(
        self,
        session_id: str,
        task: str,
        observations: list[dict[str, Any]],
        reason: str,
        task_logger: Any | None,
    ) -> TaskDecision:
        """Controlled, non-crashing outcome when no provider can safely take this turn.

        The session and its full history/observations remain persisted so a
        human can resolve capacity/configuration and resume; nothing required
        to continue is silently discarded.
        """
        result = TaskDecision(
            action=ACTION_MISSING_CAPABILITY,
            reason=reason,
            message=(
                "Nexus could not safely take this controller turn: the compact "
                "prompt still exceeds the safe local budget and no external "
                "provider is available. This needs human review before retrying."
            ),
            observations=observations,
            session_id=session_id,
        )
        if task_logger is not None:
            task_logger.warning(
                "orchestration",
                "controller_budget_exceeded",
                reason,
                action=result.action,
                reason=reason,
                session_id=session_id,
            )
        self._persist_final(session_id, task, result)
        return result

    @staticmethod
    def _parse(raw: str) -> dict[str, Any]:
        try:
            data = extract_json(raw)
        except LlamaCppClientError as exc:
            raise TaskOrchestratorError(str(exc)) from exc
        if not isinstance(data, dict):
            raise TaskOrchestratorError("orchestrator response must be a JSON object")
        response_type = str(data.get("type", "")).strip().lower()
        if response_type == "tool" and not str(data.get("tool", "")).strip():
            raise TaskOrchestratorError("tool response requires tool")
        if response_type == "action" and not str(data.get("action", "")).strip():
            raise TaskOrchestratorError("action response requires action")
        if response_type not in {"tool", "action"}:
            raise TaskOrchestratorError(f"unknown response type {response_type!r}")
        return data

    async def _execute_tool(
        self,
        decision: dict[str, Any],
        *,
        budget_plan: PromptBudgetPlan | None = None,
        task_logger: Any | None = None,
    ) -> dict[str, Any]:
        tool = str(decision.get("tool", "")).strip()
        if tool == TOOL_FIND_TOOL:
            return await self._resolve_find_tool(
                str(decision.get("goal", "")).strip(),
                session_id=str(decision.get("session_id", "")),
                budget_plan=budget_plan,
                task_logger=task_logger,
            )
        if tool == "operation":
            return self._execute_operation(decision)
        available_operations = {spec.name for spec in self._read_only_operation_specs()}
        if tool in available_operations:
            normalized = dict(decision)
            normalized["tool"] = "operation"
            normalized["operation"] = tool
            return self._execute_operation(normalized)
        if tool == ACTION_COMMAND:
            command = str(decision.get("command", "")).strip()
            if not command:
                raise TaskOrchestratorError("command tool requires command")
            logger.info(
                "llm_provider selected provider=local role=developer reason=%s command_chars=%s",
                str(decision.get("reason", ""))[:500],
                len(command),
            )
            return self._run_command_with_recovery(command, tool=ACTION_COMMAND)
        if tool == TOOL_PYTHON_RUN:
            return self._execute_python_run(decision)
        if tool == "memory_search":
            query = str(decision.get("query", "")).strip()
            if not query:
                raise TaskOrchestratorError("memory_search requires query")
            try:
                limit = int(decision.get("limit", 6))
            except (TypeError, ValueError):
                limit = 6
            return {"tool": "memory_search", "query": query[:500], "results": self.memory.search(query, limit=limit)}
        if tool == "delegate":
            provider = str(decision.get("provider", "")).strip()
            subtask = str(decision.get("task", "")).strip()
            context = str(decision.get("context", ""))[:6000]
            if not subtask:
                raise TaskOrchestratorError("delegate requires task")
            return await self._delegate_with_fallback(
                provider,
                subtask,
                context=context,
                reason=str(decision.get("reason", "")),
            )
        raise TaskOrchestratorError(f"unknown tool {tool!r}")

    def _repo_root(self) -> Path:
        return Path(getattr(self.operations_registry, "repo_root", "/agent"))

    def _resolve_cwd(self, raw: Any) -> Path:
        repo_root = self._repo_root()
        relative = str(raw or "").strip()
        if not relative:
            return repo_root
        candidate = (repo_root / relative).resolve() if not Path(relative).is_absolute() else Path(relative).resolve()
        root = repo_root.resolve()
        if candidate != root and root not in candidate.parents:
            raise TaskOrchestratorError("working directory must stay inside the repository")
        return candidate

    def _run_command(self, command: str, cwd: Path) -> tuple[str | None, str | None]:
        try:
            result, _ = process_command(SimpleNamespace(body=command, meta={}), cwd, None, 0)
        except (OSError, ValueError) as exc:
            return None, str(exc)[:4000]
        return result, None

    def _run_command_with_recovery(
        self,
        command: str,
        *,
        tool: str,
        cwd: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one command and permit at most one deterministic corrected retry."""
        working_directory = cwd or self._repo_root()
        interpreter = resolve_python_interpreter(self._repo_root())
        payload: dict[str, Any] = {
            "tool": tool,
            "command": redact_secrets(command)[:2000],
            "cwd": str(working_directory),
            "interpreter": interpreter,
            "provider_used": "local",
            **(extra or {}),
        }

        result, error = self._run_command(command, working_directory)
        if error is not None:
            return {**payload, "error": error}

        failure = classify_command_failure(result)
        if failure is None:
            return {**payload, "result": result[:10000]}

        mapping = corrected_command(
            command,
            missing_executable=failure.executable,
            interpreter=interpreter,
        )
        if mapping is None:
            logger.warning(
                "command_resolution original=%s missing_executable=%s mapping=none outcome=missing_capability",
                redact_secrets(command)[:500],
                failure.executable[:120],
            )
            return {
                **payload,
                "event": "command_not_found",
                "missing_executable": failure.executable[:120],
                "detail": failure.detail,
                "result": result[:4000],
                "message": (
                    "No known runtime mapping exists for this executable. Use python_run for Python, "
                    "select another capability, or choose missing_capability. Do not repeat this command "
                    "and do not repeat memory searches for it."
                ),
            }

        corrected, mapping_name = mapping
        logger.info(
            "command_resolution original=%s missing_executable=%s mapping=%s corrected=%s",
            redact_secrets(command)[:500],
            failure.executable[:120],
            mapping_name,
            redact_secrets(corrected)[:500],
        )
        retried, retry_error = self._run_command(corrected, working_directory)
        recovery = {
            "event": "command_resolution_retry",
            "original_command": redact_secrets(command)[:2000],
            "missing_executable": failure.executable[:120],
            "mapping": mapping_name,
            "attempts": 2,
        }
        payload = {**payload, "command": redact_secrets(corrected)[:2000], "recovery": recovery}
        if retry_error is not None:
            logger.warning("command_resolution mapping=%s outcome=retry_error", mapping_name)
            return {**payload, "error": retry_error}
        logger.info(
            "command_resolution mapping=%s outcome=%s",
            mapping_name,
            "unresolved" if classify_command_failure(retried) else "recovered",
        )
        return {**payload, "result": (retried or "")[:10000]}

    def _execute_python_run(self, decision: dict[str, Any]) -> dict[str, Any]:
        argv = decision.get("argv")
        if argv is None and isinstance(decision.get("args"), list):
            argv = decision.get("args")
        if argv is not None and not isinstance(argv, list):
            raise TaskOrchestratorError("python_run argv must be a list")
        cwd = self._resolve_cwd(decision.get("cwd"))
        try:
            plan = build_python_command(
                script=decision.get("script"),
                module=decision.get("module"),
                args=argv or (),
                repo_root=self._repo_root(),
            )
        except RuntimeExecutionError as exc:
            raise TaskOrchestratorError(str(exc)) from exc

        logger.info(
            "python_run target=%s argument_count=%s cwd=%s reason=%s",
            plan.target[:200],
            len(argv or []),
            cwd,
            str(decision.get("reason", ""))[:500],
        )
        result, error = self._run_command(plan.command, cwd)
        payload: dict[str, Any] = {
            "tool": TOOL_PYTHON_RUN,
            "target": plan.target[:500],
            "command": redact_secrets(plan.command)[:2000],
            "interpreter": plan.interpreter,
            "cwd": str(cwd),
            "provider_used": "local",
        }
        if error is not None:
            return {**payload, "error": error}
        return {**payload, "result": (result or "")[:10000]}

    async def _delegate_with_fallback(
        self,
        provider: str,
        task: str,
        *,
        context: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        available = {item["provider"] for item in self.delegation.catalog()}
        if provider not in available:
            raise TaskOrchestratorError(f"delegation provider {provider!r} is not available")

        order = [p for p in _PROVIDER_FALLBACKS.get(provider, (provider,)) if p in available]
        attempts: list[dict[str, Any]] = []
        previous_error = ""

        for index, candidate in enumerate(order):
            if index == 0:
                logger.info(
                    "llm_provider selected provider=%s role=delegate reason=%s",
                    candidate,
                    reason[:500],
                )
            else:
                logger.warning(
                    "llm_provider fallback from=%s to=%s reason=%s",
                    order[index - 1],
                    candidate,
                    previous_error[:500] or "previous_provider_failed",
                )

            result = await self.delegation.delegate(candidate, task, context=context)
            payload = result.to_dict()
            attempts.append(
                {
                    "provider": candidate,
                    "success": bool(payload.get("success")),
                    "metadata": payload.get("metadata") or {},
                }
            )
            if payload.get("success"):
                logger.info(
                    "llm_provider completed provider=%s role=delegate fallback_count=%s",
                    candidate,
                    index,
                )
                return {
                    "tool": "delegate",
                    "requested_provider": provider,
                    "provider_used": candidate,
                    "fallback_count": index,
                    "attempts": attempts,
                    "result": payload,
                }

            metadata = payload.get("metadata") or {}
            previous_error = str(metadata.get("error") or "provider returned unsuccessful result")

        logger.error(
            "llm_provider exhausted requested=%s attempts=%s reason=%s",
            provider,
            [item["provider"] for item in attempts],
            previous_error[:500],
        )
        return {
            "tool": "delegate",
            "requested_provider": provider,
            "provider_used": None,
            "fallback_count": max(0, len(attempts) - 1),
            "attempts": attempts,
            "result": {
                "provider": provider,
                "success": False,
                "content": "",
                "metadata": {"error": previous_error or "all available providers failed"},
            },
        }

    def _execute_operation(self, decision: dict[str, Any]) -> dict[str, Any]:
        operation = str(decision.get("operation", "")).strip()
        args = decision.get("args") or {}
        if not isinstance(args, dict):
            raise TaskOrchestratorError("operation args must be an object")
        available = {spec.name for spec in self._read_only_operation_specs()}
        if operation not in available:
            raise TaskOrchestratorError(f"operation {operation!r} is not available")
        grant = self._latest_operation_grant(
            str(decision.get("session_id", "")).strip(),
            operation,
        )
        if grant is None:
            return {
                "tool": "operation",
                "operation": operation,
                "args": args,
                "error": "operation_not_granted",
                "reason": (
                    "Operation execution requires a prior successful find_tool result "
                    "for the same persistent Nexus session."
                ),
            }
        result = self.operations_registry.execute(
            operation,
            args,
            caller="local task orchestrator",
        )
        payload = result.to_dict()
        payload["output_lines"] = [
            str(line)[:500]
            for line in list(payload.get("output_lines") or [])[:80]
        ]
        return {
            "tool": "operation",
            "operation": operation,
            "args": args,
            "grant_event_id": grant["event_id"],
            "operation_schema": grant["schema"],
            "result": payload,
        }

    def _persist_final(self, session_id: str, task: str, decision: TaskDecision) -> None:
        self.task_store.append_event(
            session_id,
            role="assistant",
            kind=decision.action,
            content=decision.message or decision.reason or decision.action,
            metadata={"reason": decision.reason, "experiment_id": decision.experiment_id, "has_command": bool(decision.command)},
        )
        if decision.action == ACTION_CLARIFICATION:
            self.task_store.set_status(session_id, "waiting_for_user")
        elif decision.action == ACTION_MISSING_CAPABILITY:
            self.task_store.set_status(session_id, "blocked_missing_capability")
            self._record_missing_capability(task, decision)
        else:
            self.task_store.set_status(session_id, "action_selected")

    def _record_missing_capability(self, task: str, decision: TaskDecision) -> None:
        repo_root = getattr(self.operations_registry, "repo_root", None)
        if repo_root is None:
            return
        try:
            root = Path(repo_root) / "state" / "requirements"
            root.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            path = root / f"{now.strftime('%Y%m%d%H%M%S%f')}-missing-capability.json"
            path.write_text(
                json.dumps({"timestamp": now.isoformat(), "type": "missing_capability", "session_id": decision.session_id, "trigger_task": task[:4000], "requirement": decision.message[:3000], "reason": decision.reason[:1000], "source": "local_task_orchestrator", "status": "new"}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return
