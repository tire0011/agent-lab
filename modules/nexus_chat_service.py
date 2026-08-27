from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from modules.experiment_resume import resume_experiment
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_COMMAND,
    ACTION_MISSING_CAPABILITY,
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
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
        blocked = {ACTION_COMMAND, ACTION_NEW_EXPERIMENT, ACTION_RESUME_EXPERIMENT}
        return [item for item in capabilities if item.get("name") not in blocked]

    def _prompt(self, session: Any, current_input: str, history: list[dict[str, Any]], observations: list[dict[str, Any]]) -> str:
        base = super()._prompt(session, current_input, history, observations)
        if self.allow_mutation:
            return base + "\nCHAT POLICY: The current user message clearly requests execution. Mutating tools and experiment actions are allowed when useful.\n"
        return base + (
            "\nCHAT POLICY: This is discussion/analysis only. Do not execute shell commands, create experiments, "
            "resume experiments, or make repository changes. Use read-only inspection/memory/delegation and finish with "
            "answer, clarification, or missing_capability.\n"
        )

    async def _execute_tool(self, decision: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_mutation and str(decision.get("tool", "")).strip() == ACTION_COMMAND:
            return {"tool": ACTION_COMMAND, "error": "mutation blocked in discussion-only chat turn"}
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
    ) -> None:
        self.operations_registry = operations_registry
        self.experiment_service = experiment_service
        self.orchestrator_factory = orchestrator_factory
        self.background_starter = background_starter or self._start_background
        self.resume_runner = resume_runner

    def handle(self, message: str, *, session_id: str | None = None) -> NexusChatResult:
        text = (message or "").strip()
        if not text:
            raise ValueError("message must not be empty")
        allow_mutation = is_execution_request(text)
        orchestrator = self.orchestrator_factory(
            operations_registry=self.operations_registry,
            experiment_service=self.experiment_service,
            allow_mutation=allow_mutation,
        )
        try:
            if session_id:
                decision = orchestrator.continue_session(session_id, text)
            else:
                decision = orchestrator.run(text, source_type="chat", title="Nexus chat")

            if not decision.session_id:
                raise TaskOrchestratorError("Nexus chat decision has no session_id")

            if not allow_mutation and decision.action in (ACTION_NEW_EXPERIMENT, ACTION_RESUME_EXPERIMENT):
                return NexusChatResult(
                    reply=decision.message or "Ich kann das weiter diskutieren; ausführen werde ich es erst nach einer eindeutigen Umsetzungsanweisung.",
                    session_id=decision.session_id,
                    action=ACTION_CLARIFICATION,
                    status="discussion",
                )

            if decision.action == ACTION_NEW_EXPERIMENT:
                goal = self._implementation_goal(orchestrator, decision, text)
                experiment = self.experiment_service.create_experiment(goal)
                self.background_starter(
                    experiment.id,
                    lambda: self.experiment_service.run_experiment(experiment.id),
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
                self.background_starter(
                    experiment.id,
                    lambda: self.resume_runner(self.experiment_service, experiment.id),
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
