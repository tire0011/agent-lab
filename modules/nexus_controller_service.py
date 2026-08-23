"""Generic execution entry point for task, issue, idea and discussion work."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from modules.experiment_resume import resume_experiment
from modules.mailbox_development import process_command
from modules.nexus_sources import NexusSource, idea_source, issue_source, task_source
from modules.task_orchestrator import (
    ACTION_COMMAND,
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
    NexusTaskOrchestrator,
    TaskDecision,
)


class NexusControllerService:
    """Feed heterogeneous work into one persistent local-LLM controller.

    The orchestrator chooses the action; this service executes the selected
    primitive. Tool calls performed during reasoning still happen inside the
    orchestrator and always return control to the local model first.
    """

    def __init__(self, orchestrator: NexusTaskOrchestrator) -> None:
        self.orchestrator = orchestrator

    @property
    def experiment_service(self):
        return self.orchestrator.experiment_service

    @property
    def repo_root(self) -> Path:
        return Path(getattr(self.orchestrator.operations_registry, "repo_root", "/agent"))

    def handle_source(self, source: NexusSource) -> TaskDecision:
        decision = self.orchestrator.run(
            source.content,
            source_type=source.source_type,
            source_id=source.source_id,
            title=source.title,
        )
        self._execute(decision, source.content)
        return decision

    def handle_task(
        self,
        content: str,
        *,
        source_id: str | None = None,
        title: str | None = None,
    ) -> TaskDecision:
        return self.handle_source(task_source(content, source_id=source_id, title=title))

    def handle_idea(self, idea_id: str) -> TaskDecision:
        return self.handle_source(idea_source(idea_id))

    def handle_issue(
        self,
        issue_number: int,
        *,
        title: str,
        body: str,
        state: str | None = None,
        url: str | None = None,
    ) -> TaskDecision:
        return self.handle_source(
            issue_source(
                issue_number,
                title=title,
                body=body,
                state=state,
                url=url,
            )
        )

    def reply(self, session_id: str, message: str) -> TaskDecision:
        decision = self.orchestrator.continue_session(session_id, message)
        self._execute(decision, self._execution_goal(session_id, message))
        return decision

    def _execution_goal(self, session_id: str, fallback: str) -> str:
        """Reconstruct the user-visible task across clarification replies.

        Tool observations remain available to the controller through persistent
        history/memory but are not copied verbatim into an implementation goal.
        """
        try:
            history = self.orchestrator.task_store.history(
                session_id,
                limit=50,
                max_chars=20000,
            )
        except Exception:
            return fallback
        user_inputs = [
            str(event.get("content", "")).strip()
            for event in history
            if event.get("role") == "user" and event.get("kind") == "input"
        ]
        user_inputs = [item for item in user_inputs if item]
        if not user_inputs:
            return fallback
        return "\n\nUser follow-up:\n".join(user_inputs)[:24000]

    def _execute(self, decision: TaskDecision, source_content: str) -> None:
        if decision.action == ACTION_NEW_EXPERIMENT:
            experiment = self.experiment_service.create_experiment(source_content)
            self._record_execution(
                decision,
                "experiment_started",
                f"experiment_id={experiment.id}",
            )
            self._start_experiment_thread(
                experiment.id,
                lambda: self.experiment_service.run_experiment(experiment.id),
            )
            return

        if decision.action == ACTION_RESUME_EXPERIMENT:
            if not decision.experiment_id:
                raise RuntimeError("resume decision has no experiment id")
            experiment_id = decision.experiment_id
            self._record_execution(
                decision,
                "experiment_resume_started",
                f"experiment_id={experiment_id}",
            )
            self._start_experiment_thread(
                experiment_id,
                lambda: resume_experiment(self.experiment_service, experiment_id),
            )
            return

        if decision.action == ACTION_COMMAND:
            command = (decision.command or "").strip()
            if not command:
                raise RuntimeError("command decision has no command")
            result, _ = process_command(
                SimpleNamespace(body=command, meta={}),
                self.repo_root,
                None,
                0,
            )
            self._record_execution(decision, "command_result", result[:4000])

    def _record_execution(self, decision: TaskDecision, kind: str, content: str) -> None:
        if not decision.session_id:
            return
        try:
            self.orchestrator.task_store.append_event(
                decision.session_id,
                role="tool",
                kind=kind,
                content=content,
            )
        except Exception:
            return

    @staticmethod
    def _start_experiment_thread(experiment_id: str, target) -> None:
        def run() -> None:
            target()

        threading.Thread(
            target=run,
            daemon=True,
            name=f"nexus-controller-exp-{experiment_id[:8]}",
        ).start()
