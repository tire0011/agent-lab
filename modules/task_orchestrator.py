"""Agentic local-LLM controller for free-form Nexus work.

The local model remains in control across tasks, issues, ideas and discussions.
It can inspect Nexus state, search persistent memory, consult other LLMs and
then decide what Nexus should do next. Existing pipelines remain tools.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json
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
        max_steps: int = 6,
    ) -> None:
        self.operations_registry = operations_registry
        self.experiment_service = experiment_service
        self.llama = llama_client or LlamaCppClient(timeout=90.0)
        self.max_steps = max(1, min(max_steps, 10))
        db_path = getattr(operations_registry, "db_path", DEFAULT_DB_PATH)
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
            raw = await self.llama.complete(
                prompt=self._prompt(session, task, history, observations),
                temperature=0.0,
                max_tokens=550,
            )
            decision = self._parse(raw)
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

            command = str(decision.get("command", "") or "").strip() or None
            if action == ACTION_COMMAND and not command:
                raise TaskOrchestratorError("command action requires command")

            result = TaskDecision(
                action=action,
                reason=str(decision.get("reason", ""))[:1000],
                message=str(decision.get("message", ""))[:3000],
                experiment_id=experiment_id,
                command=command,
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
            {"name": "delegate", "description": "Ask another bounded LLM worker for analysis/research/review. The result returns to the local controller; delegation never transfers control.", "providers": self.delegation.catalog(), "arguments": {"provider": "provider name", "task": "sub-problem", "context": "optional compact context"}},
            {"name": ACTION_NEW_EXPERIMENT, "description": "Start a new isolated implementation experiment. Use for repository changes too large to solve through existing direct tools."},
            {"name": ACTION_RESUME_EXPERIMENT, "description": "Resume an existing persisted experiment/worktree without allocating another experiment."},
            {"name": ACTION_COMMAND, "description": "Execute an explicit operational shell command through the trusted mailbox command primitive."},
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
Final action:
{{"type":"action","action":"new_experiment|resume_experiment|command|answer|missing_capability|clarification","experiment_id":null,"command":null,"message":"...","reason":"..."}}

Rules:
- The source may be a task, GitHub issue, idea or a reply in an ongoing discussion; treat all as work to understand and advance.
- Search memory when previous project decisions, experiments, discussions or ideas may materially help; do not search mechanically.
- Prefer cheap/local reasoning and existing tools first. Delegate only when another model materially improves a difficult analysis, review or research step.
- Delegation is a tool call, never a transfer of control. Inspect its result and decide again.
- `codex_readonly` and `azure_openai` are external/costlier; use them only when expected value justifies it.
- For repository implementation changes, use new_experiment rather than asking a read-only delegate to modify files.
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
            available = {item["provider"] for item in self.delegation.catalog()}
            if provider not in available:
                raise TaskOrchestratorError(f"delegation provider {provider!r} is not available")
            if not subtask:
                raise TaskOrchestratorError("delegate requires task")
            result = await self.delegation.delegate(provider, subtask, context=context)
            return {"tool": "delegate", "result": result.to_dict()}
        raise TaskOrchestratorError(f"unknown tool {tool!r}")

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
