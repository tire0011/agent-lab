"""Agentic local-LLM controller for free-form Nexus work.

The local model remains in control across tasks, issues, ideas and discussions.
It can inspect Nexus state, search persistent memory, consult other LLMs and
then decide what Nexus should do next. Existing pipelines remain tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json
from modules.local_llm_jobs import LocalLlmJobError, LocalLlmJobService
from modules.mailbox_development import process_command
from modules.nexus_delegation import NexusDelegationService
from modules.nexus_memory import NexusMemory
from modules.nexus_task_store import NexusTaskStore
from modules.run_store import DEFAULT_DB_PATH

ACTION_NEW_EXPERIMENT = "new_experiment"
ACTION_RESUME_EXPERIMENT = "resume_experiment"
ACTION_COMMAND = "command"
ACTION_ANSWER = "answer"
ACTION_CLARIFICATION = "clarification"
ACTION_MISSING_CAPABILITY = "missing_capability"

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
    ) -> None:
        self.operations_registry = operations_registry
        self.experiment_service = experiment_service
        job_watchdog = float(os.environ.get("NEXUS_LOCAL_LLM_JOB_WATCHDOG_SECONDS", "3600"))
        job_watchdog = max(300.0, min(job_watchdog, 7200.0))
        self.llama = llama_client or LlamaCppClient(timeout=job_watchdog)
        self.max_steps = max(1, min(max_steps, 10))
        db_path = getattr(operations_registry, "db_path", DEFAULT_DB_PATH)
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
    ) -> TaskDecision:
        return asyncio.run(
            self.run_async(
                task,
                source_type=source_type,
                source_id=source_id,
                title=title,
                session_id=session_id,
            )
        )

    def continue_session(self, session_id: str, user_message: str) -> TaskDecision:
        session = self.task_store.get(session_id)
        if session is None:
            raise TaskOrchestratorError(f"unknown Nexus task session {session_id!r}")
        return self.run(
            user_message,
            source_type=session.source_type,
            source_id=session.source_id,
            title=session.title,
            session_id=session.id,
        )

    async def run_async(
        self,
        task: str,
        *,
        source_type: str = "task",
        source_id: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
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
        self.task_store.append_event(
            session.id,
            role="user",
            kind="input",
            content=task.strip(),
            metadata={"source_type": session.source_type, "source_id": session.source_id},
        )
        observations: list[dict[str, Any]] = []

        for _ in range(self.max_steps):
            history = self.task_store.history(session.id, limit=18, max_chars=7000)
            prompt = self._prompt(session, task, history, observations)
            logger.info(
                "llm_provider selected provider=local role=controller session_id=%s reason=persistent_controller",
                session.id,
            )
            job_id = self.llm_jobs.submit(
                prompt,
                temperature=0.0,
                max_tokens=550,
                session_id=session.id,
            )
            self.task_store.set_status(session.id, "waiting_for_llm")
            try:
                raw = await self.llm_jobs.wait(job_id)
            except LocalLlmJobError as exc:
                self.task_store.set_status(session.id, "active")
                raise TaskOrchestratorError(f"local LLM job failed: {exc}") from exc
            self.task_store.set_status(session.id, "active")
            decision = self._parse(raw)
            # Small local models occasionally emit the command capability with
            # the final-action envelope (``action: command``) even though the
            # prompt explicitly defines commands as tools. Treat that shape as
            # the command tool instead of failing the whole persistent task.
            if decision.get("type") != "tool" and str(decision.get("action", "")).strip().lower() == ACTION_COMMAND:
                decision = dict(decision)
                decision["type"] = "tool"
                decision["tool"] = ACTION_COMMAND
            if decision.get("type") == "tool":
                observation = await self._execute_tool(decision)
                observations.append(observation)
                self.task_store.append_event(
                    session.id,
                    role="tool",
                    kind=str(observation.get("tool", "tool")),
                    content=json.dumps(observation, separators=(",", ":"))[:10000],
                    metadata={"reason": str(decision.get("reason", ""))[:1000]},
                )
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
            self._persist_final(session.id, task, result)
            return result

        result = TaskDecision(
            action=ACTION_CLARIFICATION,
            reason="local orchestrator reached its bounded decision-step limit",
            message="I need one more piece of information before I can choose a reliable action.",
            observations=observations,
            session_id=session.id,
        )
        self._persist_final(session.id, task, result)
        return result

    def _capabilities(self) -> list[dict[str, Any]]:
        operations = [spec.to_dict() for spec in self.operations_registry.catalog()]
        return [
            {"name": "operation", "description": "Execute one existing Nexus inspection/operation and receive its real result before deciding again.", "operations": operations},
            {"name": "memory_search", "description": "Search Nexus memory: structured knowledge, previous task/dialog history and project memory documents.", "arguments": {"query": "string", "limit": "1..10"}},
            {"name": "delegate", "description": "Ask another bounded LLM worker for analysis/research/review. Provider failures automatically fall back to another available model and control then returns to Nexus.", "providers": self.delegation.catalog(), "arguments": {"provider": "provider name", "task": "sub-problem", "context": "optional compact context"}},
            {"name": ACTION_COMMAND, "description": "Local development tool: execute one shell command through the trusted development command primitive, observe its real output, then keep control and decide again.", "arguments": {"command": "shell command"}},
            {"name": ACTION_NEW_EXPERIMENT, "description": "Codex development path: start a new isolated implementation experiment for large/complex repository changes."},
            {"name": ACTION_RESUME_EXPERIMENT, "description": "Resume an existing persisted experiment/worktree without allocating another experiment."},
            {"name": ACTION_ANSWER, "description": "Finish directly when Nexus itself or its tool/delegate observations have solved the request."},
            {"name": ACTION_MISSING_CAPABILITY, "description": "State that no available capability can complete the request and record a structured requirement."},
            {"name": ACTION_CLARIFICATION, "description": "Pause the persistent task and ask one concise question when a material ambiguity/information gap cannot be resolved with tools or memory."},
        ]

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
{{"type":"tool","tool":"operation","operation":"<catalog operation>","args":{{...}},"reason":"..."}}
{{"type":"tool","tool":"memory_search","query":"...","limit":6,"reason":"..."}}
{{"type":"tool","tool":"delegate","provider":"local_worker|codex_readonly|azure_openai","task":"bounded sub-problem","context":"optional","reason":"..."}}
{{"type":"tool","tool":"command","command":"<shell command>","reason":"..."}}
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
- Prefer cheap/local reasoning and existing tools first. Delegate only when another model materially improves a difficult analysis, review or research step.
- Delegation is a tool call, never a transfer of control. Inspect its result and decide again.
- Commands are tool calls, never final actions. Inspect command output and decide again before finishing.
- For an existing experiment, inspect state if useful and select resume_experiment with its real id/prefix.
- Solve small requests with reasoning/operations/commands where practical, then answer instead of creating an experiment.
- Ask clarification only when missing information materially changes the result and cannot be obtained from tools or memory; the session persists for the reply.
- If a capability is genuinely missing, choose missing_capability and describe exactly what is needed.
- Never invent tool names, provider names, operation results or experiment IDs.
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

    async def _execute_tool(self, decision: dict[str, Any]) -> dict[str, Any]:
        tool = str(decision.get("tool", "")).strip()
        if tool == "operation":
            return self._execute_operation(decision)
        available_operations = {spec.name for spec in self.operations_registry.catalog()}
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
            try:
                result, _ = process_command(
                    SimpleNamespace(body=command, meta={}),
                    Path(getattr(self.operations_registry, "repo_root", "/agent")),
                    None,
                    0,
                )
            except (OSError, ValueError) as exc:
                return {"tool": ACTION_COMMAND, "command": command[:2000], "error": str(exc)[:4000]}
            return {"tool": ACTION_COMMAND, "command": command[:2000], "result": result[:10000], "provider_used": "local"}
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
        available = {spec.name for spec in self.operations_registry.catalog()}
        if operation not in available:
            raise TaskOrchestratorError(f"operation {operation!r} is not available")
        result = self.operations_registry.execute(operation, args, caller="local task orchestrator")
        payload = result.to_dict()
        payload["output_lines"] = [str(line)[:500] for line in list(payload.get("output_lines") or [])[:80]]
        return {"tool": "operation", "operation": operation, "args": args, "result": payload}

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
