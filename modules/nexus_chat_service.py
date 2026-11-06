from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

from modules.central_logging import DEFAULT_LOG_DIR, get_chat_logger
from modules.experiment_handover import with_handover
from modules.experiment_service import ExperimentAlreadyActiveError
from modules.experiment_wait import resume_with_new_orchestrator, run_then_resume
from modules.experiment_resume import resume_experiment
from modules.github_work_tracking import GitHubWorkTrackingService
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_COMMAND,
    ACTION_MISSING_CAPABILITY,
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
    TOOL_PYTHON_RUN,
    NexusTaskOrchestrator,
    TaskDecision,
    TaskOrchestratorError,
)

logger = logging.getLogger(__name__)

_EXECUTION_RE = re.compile(
    r"^(?:bitte\s+)?(?:mach(?:e)?(?:\s+das|\b.*\bweiter\b)|setz(?:e)?(?:\s+das\s+um|\b.*\bfort\b)|implementier(?:e)?\b|beheb(?:e)?\b|fix(?:e)?\b|führ(?:e)?\b|starte\b|änder(?:e)?\b|füg(?:e)?\b|entfern(?:e)?\b|integrier(?:e)?\b|do\s+it\b|implement\b|fix\b|execute\b|apply\b|run\s+it\b|go\s+ahead\b)",
    re.IGNORECASE,
)
_POLITE_EXECUTION_RE = re.compile(
    r"\b(?:kannst\s+du|könntest\s+du|please)\b.*\b(?:umsetzen|implementieren|beheben|fixen|ausführen|ändern|hinzufügen|entfernen|integrieren|implement|fix|execute|apply)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class NexusChatResult:
    reply: str
    session_id: str
    action: str
    experiment_id: str | None = None
    status: str = "completed"


def is_execution_request(message: str) -> bool:
    text = (message or "").strip()
    return bool(_EXECUTION_RE.search(text) or _POLITE_EXECUTION_RE.search(text))


class _ChatOrchestrator(NexusTaskOrchestrator):
    def __init__(self, *args: Any, allow_mutation: bool, **kwargs: Any) -> None:
        self.allow_mutation = allow_mutation
        super().__init__(*args, **kwargs)

    def _capabilities(self) -> list[dict[str, Any]]:
        capabilities = super()._capabilities()
        if self.allow_mutation:
            return capabilities
        blocked = {ACTION_COMMAND, TOOL_PYTHON_RUN, ACTION_NEW_EXPERIMENT, ACTION_RESUME_EXPERIMENT}
        return [item for item in capabilities if item.get("name") not in blocked]

    def _compact_capabilities(self) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for item in self._capabilities():
            # Chat has a deliberately small prompt budget. Capability descriptions
            # are hints here; the full task controller keeps the complete definitions.
            entry = {
                "name": item.get("name"),
                "description": str(item.get("description", ""))[:160],
            }
            if item.get("name") == "operation":
                entry["operations"] = [{"name": op.get("operation") or op.get("name"), "description": op.get("description", ""), "arguments": [arg.get("name") for arg in op.get("arguments", []) if isinstance(arg, dict)]} for op in item.get("operations", [])]
            elif "providers" in item:
                entry["providers"] = [provider.get("provider") for provider in item.get("providers", [])]
            elif "arguments" in item:
                entry["arguments"] = item.get("arguments")
            compact.append(entry)
        return compact

    @staticmethod
    def _compact_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        chars = 0
        for event in reversed(history[-10:]):
            if event.get("role") == "tool":
                continue
            content = str(event.get("content", "")).strip()
            if not content:
                continue
            content = content[:700]
            if chars + len(content) > 2500 and compact:
                break
            compact.append({"role": event.get("role", ""), "kind": event.get("kind", ""), "content": content})
            chars += len(content)
            if len(compact) >= 6:
                break
        compact.reverse()
        return compact

    def _prompt(self, session: Any, current_input: str, history: list[dict[str, Any]], observations: list[dict[str, Any]]) -> str:
        import json
        final_actions = [ACTION_ANSWER, ACTION_CLARIFICATION, ACTION_MISSING_CAPABILITY]
        policy = "Discussion only: use read-only tools when needed; do not mutate."
        tool_names = ["operation", "memory_search", "delegate"]
        if self.allow_mutation:
            final_actions = [ACTION_NEW_EXPERIMENT, ACTION_RESUME_EXPERIMENT, ACTION_ANSWER, ACTION_CLARIFICATION, ACTION_MISSING_CAPABILITY]
            policy = "Execution explicitly requested: mutating command/experiment actions are allowed when useful."
            tool_names = [*tool_names, ACTION_COMMAND, TOOL_PYTHON_RUN]
        return "\n".join([
            "You are Nexus, the persistent local chat controller. Be concise and solve the current turn with the cheapest capable path.",
            "", "CURRENT INPUT:", current_input,
            "", "RECENT CHAT:", json.dumps(self._compact_history(history), separators=(",", ":")),
            "", "CAPABILITIES:", json.dumps(self._compact_capabilities(), separators=(",", ":")),
            "", "OBSERVATIONS:", json.dumps(observations, separators=(",", ":")),
            "", "POLICY: " + policy,
            "Use memory/inspection only when it materially helps. Commands are tools, never final actions. After any tool result, decide again. Never invent tool results or IDs.",
            'Tool JSON: {"type":"tool","tool":"' + "|".join(tool_names) + '",...}',
            'Final JSON: {"type":"action","action":"' + "|".join(final_actions) + '","experiment_id":null,"message":"...","reason":"..."}',
            'For missing_capability also include "information_gap":"...", "gap_kind":"missing_capability", "capability":"stable identifier", and "required_inputs":["input_name"].',
            "Return JSON only.",
        ])

    async def _execute_tool(self, decision: dict[str, Any]) -> dict[str, Any]:
        tool = str(decision.get("tool", "")).strip()
        if not self.allow_mutation and tool in {ACTION_COMMAND, TOOL_PYTHON_RUN}:
            return {"tool": tool, "error": "mutation blocked in discussion-only chat turn"}
        return await super()._execute_tool(decision)


class NexusChatService:
    def __init__(
        self,
        *,
        operations_registry: Any,
        experiment_service: Any,
        orchestrator_factory: Callable[..., NexusTaskOrchestrator] = _ChatOrchestrator,
        background_starter: Callable[[str, Callable[[], None]], None] | None = None,
        resume_runner: Callable[[Any, str], Any] = resume_experiment,
        github_work_tracking: GitHubWorkTrackingService | None = None,
    ) -> None:
        self.operations_registry = operations_registry
        self.experiment_service = experiment_service
        self.orchestrator_factory = orchestrator_factory
        self.background_starter = background_starter or self._start_background
        self.resume_runner = resume_runner
        self.github_work_tracking = github_work_tracking or GitHubWorkTrackingService(
            db_path=str(getattr(experiment_service, "db_path", "/agent/state/agent.db")),
            repo_root=str(getattr(operations_registry, "repo_root", "/agent")),
        )

    def handle(
        self,
        message: str,
        *,
        session_id: str | None = None,
        job_id: str | None = None,
    ) -> NexusChatResult:
        text = (message or "").strip()
        if not text:
            raise ValueError("message must not be empty")
        task_log = (
            get_chat_logger(
                job_id,
                session_id=session_id,
                log_dir=getattr(self.experiment_service, "experiment_log_dir", DEFAULT_LOG_DIR),
            )
            if job_id
            else None
        )
        if task_log is not None:
            task_log.info("chat", "request_received", "chat request received")
        allow_mutation = is_execution_request(text)
        orchestrator = self.orchestrator_factory(
            operations_registry=self.operations_registry,
            experiment_service=self.experiment_service,
            allow_mutation=allow_mutation,
        )
        try:
            if session_id:
                if task_log is None:
                    decision = orchestrator.continue_session(session_id, text)
                else:
                    decision = orchestrator.continue_session(
                        session_id,
                        text,
                        task_logger=task_log,
                    )
            else:
                decision = orchestrator.run(
                    text,
                    source_type="chat",
                    title="Nexus chat",
                    task_logger=task_log,
                )

            if not decision.session_id:
                raise TaskOrchestratorError("Nexus chat decision has no session_id")
            if task_log is not None:
                task_log.bind_session_id(decision.session_id)

            if not allow_mutation and decision.action in (ACTION_NEW_EXPERIMENT, ACTION_RESUME_EXPERIMENT):
                return NexusChatResult(
                    reply=decision.message or "Ich kann das weiter diskutieren; ausführen werde ich es erst nach einer eindeutigen Umsetzungsanweisung.",
                    session_id=decision.session_id,
                    action=ACTION_CLARIFICATION,
                    status="discussion",
                )

            if decision.action == ACTION_NEW_EXPERIMENT:
                goal = self._implementation_goal(orchestrator, decision, text)
                # What Nexus already learned travels with the conversation (#263).
                goal = with_handover(goal, decision.experiment_handover)
                work_ref = self.github_work_tracking.ensure_for_source(
                    SimpleNamespace(
                        source_type="chat",
                        source_id=decision.session_id,
                        title="Nexus chat",
                        content=goal,
                    )
                )
                try:
                    experiment = self.experiment_service.create_experiment(
                        goal, complexity=decision.complexity
                    )
                except ExperimentAlreadyActiveError as exc:
                    return NexusChatResult(
                        reply=(
                            "Für dieses GitHub-Issue läuft bereits ein Experiment "
                            f"({exc.experiment_id}, Status: {exc.status})."
                        ),
                        session_id=decision.session_id,
                        action=decision.action,
                        experiment_id=exc.experiment_id,
                        status="already_active",
                    )
                self.github_work_tracking.bind_experiment(work_ref, experiment.id)
                # Nexus pauses on the experiment and continues the moment it ends (#263).
                orchestrator.task_store.wait_for_experiment(decision.session_id, experiment.id)
                self.background_starter(
                    experiment.id,
                    lambda: run_then_resume(
                        lambda: self.experiment_service.run_experiment(
                            experiment.id,
                            github_work_tracking=self.github_work_tracking,
                        ),
                        lambda: resume_with_new_orchestrator(
                            self.orchestrator_factory,
                            self.experiment_service,
                            self.github_work_tracking,
                            experiment.id,
                            operations_registry=self.operations_registry,
                            allow_mutation=False,
                        ),
                        experiment.id,
                    ),
                )
                return NexusChatResult(
                    reply=decision.message or "Ich habe die vereinbarte Änderung als Experiment gestartet.",
                    session_id=decision.session_id,
                    action=decision.action,
                    experiment_id=experiment.id,
                    status="submitted",
                )

            if decision.action == ACTION_RESUME_EXPERIMENT:
                if not decision.experiment_id:
                    raise TaskOrchestratorError("resume_experiment requires experiment_id")
                experiment = self.experiment_service.resolve_experiment(decision.experiment_id)
                self.github_work_tracking.recover_experiment(experiment)
                self.background_starter(
                    experiment.id,
                    lambda: self.resume_runner(
                        self.experiment_service,
                        experiment.id,
                        github_work_tracking=self.github_work_tracking,
                    ),
                )
                return NexusChatResult(
                    reply=decision.message or "Ich setze das bestehende Experiment fort.",
                    session_id=decision.session_id,
                    action=decision.action,
                    experiment_id=experiment.id,
                    status="submitted",
                )

            status = "waiting_for_user" if decision.action in (ACTION_CLARIFICATION, ACTION_MISSING_CAPABILITY) else "completed"
            return NexusChatResult(
                reply=decision.message or decision.reason or "Erledigt.",
                session_id=decision.session_id,
                action=decision.action,
                status=status,
            )
        finally:
            close = getattr(orchestrator, "close", None)
            if close:
                close()

    def _implementation_goal(self, orchestrator: NexusTaskOrchestrator, decision: TaskDecision, current_input: str) -> str:
        history = orchestrator.task_store.history(decision.session_id, limit=18, max_chars=9000)
        conversation: list[str] = []
        for event in history:
            role = str(event.get("role", ""))
            if role == "tool":
                continue
            content = str(event.get("content", "")).strip()
            if not content:
                continue
            conversation.append(f"{role}: {content}")
        if not conversation:
            conversation.append(f"user: {current_input}")
        goal = (
            "Implement the change explicitly agreed in this Nexus chat. Preserve the intent and constraints from the conversation.\n\n"
            "Conversation context:\n" + "\n".join(conversation)
        )
        return goal[:12000]

    @staticmethod
    def _start_background(name: str, target: Callable[[], None]) -> None:
        def _run() -> None:
            try:
                target()
            except Exception:
                logger.exception("nexus_chat: background execution failed id=%s", name)

        threading.Thread(target=_run, daemon=True, name=f"nexus-chat-{name[:8]}").start()
