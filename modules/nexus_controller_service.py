"""Generic execution entry point for task, issue, idea and discussion work."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from modules.experiment_wait import resume_after_experiment
from modules.experiment_resume import resume_experiment
from modules.github_work_tracking import GitHubWorkTrackingError, GitHubWorkTrackingService
from modules.mailbox.development import process_command
from modules.nexus_experiment_tool import (
    GOAL_CHAR_CAP,
    GOAL_TITLE_CAP,
    GOAL_TRUNCATION_MARKER,
    NexusExperimentTool,
    session_goal,
)
from modules.nexus_sources import NexusSource, idea_source, issue_source
from modules.run_store import DEFAULT_DB_PATH
from modules.task_orchestrator import (
    ACTION_COMMAND,
    ACTION_RESUME_EXPERIMENT,
    NexusTaskOrchestrator,
    TaskDecision,
)

# Experiments run in threads of this process; a short-lived caller such as
# the initiative tick must wait for them before it exits (#358).
EXPERIMENT_THREAD_PREFIX = "nexus-controller-exp-"

__all__ = [
    "EXPERIMENT_THREAD_PREFIX",
    "GOAL_CHAR_CAP",
    "GOAL_TITLE_CAP",
    "GOAL_TRUNCATION_MARKER",
    "NexusControllerService",
]


class NexusControllerService:
    """Feed heterogeneous work into one persistent local-LLM controller.

    The orchestrator chooses the action and executes ``new_experiment``
    itself through its experiment tool (#263): GitHub work tracking, the
    experiment with the handover, the pause and the resume. This service only
    configures that tool - experiment threads, continuation after the result -
    and carries out the remaining primitives (resume, command).
    """

    def __init__(
        self,
        orchestrator: NexusTaskOrchestrator,
        github_work_tracking: GitHubWorkTrackingService | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        db_path = getattr(self.experiment_service, "db_path", DEFAULT_DB_PATH)
        if not isinstance(db_path, (str, Path)):
            db_path = DEFAULT_DB_PATH
        self.github_work_tracking = github_work_tracking or GitHubWorkTrackingService(
            db_path=str(db_path),
            repo_root=str(self.repo_root),
        )
        if getattr(self.orchestrator, "experiment_tool", None) is None:
            self.orchestrator.experiment_tool = NexusExperimentTool(
                self.experiment_service,
                self.github_work_tracking,
                background=self._start_experiment_thread,
                on_finished=self._resume_after_experiment,
            )

    @property
    def experiment_service(self):
        return self.orchestrator.experiment_service

    @property
    def repo_root(self) -> Path:
        return Path(getattr(self.orchestrator.operations_registry, "repo_root", "/agent"))

    def handle_source(self, source: NexusSource) -> TaskDecision | dict[str, str]:
        decision = self.orchestrator.run(
            source.content,
            source_type=source.source_type,
            source_id=source.source_id,
            title=source.title,
        )
        execution_result = self._execute(decision)
        return execution_result or decision

    def handle_idea(self, idea_id: str) -> TaskDecision | dict[str, str]:
        return self.handle_source(idea_source(idea_id))

    def handle_issue(
        self,
        issue_number: int,
        *,
        title: str,
        body: str,
        state: str | None = None,
        url: str | None = None,
    ) -> TaskDecision | dict[str, str]:
        return self.handle_source(
            issue_source(
                issue_number,
                title=title,
                body=body,
                state=state,
                url=url,
            )
        )

    def reply(self, session_id: str, message: str) -> TaskDecision | dict[str, str]:
        decision = self.orchestrator.continue_session(session_id, message)
        execution_result = self._execute(decision)
        return execution_result or decision

    def resume_available_capabilities(self, *, limit: int = 1) -> list[TaskDecision]:
        """Continue blocked sessions when their required capability is now present."""
        decisions = self.orchestrator.resume_available_capabilities(limit=limit)
        completed: list[TaskDecision] = []
        for decision in decisions:
            if not decision.session_id:
                continue
            try:
                self._execute(decision)
            except Exception as exc:
                self._record_execution(
                    decision,
                    "capability_resume_execution_failed",
                    f"{type(exc).__name__}: {exc}",
                )
                self.orchestrator.task_store.set_status(decision.session_id, "failed")
                continue
            completed.append(decision)
        return completed

    def _resume_after_experiment(self, experiment_id: str) -> list[TaskDecision]:
        return self._continue_after_experiment(
            resume_after_experiment(
                self.orchestrator,
                self.experiment_service,
                self.github_work_tracking,
                experiment_id,
            )
        )

    def _continue_after_experiment(self, decisions: list[TaskDecision]) -> list[TaskDecision]:
        """Carry out what Nexus decided after seeing its experiment's result."""
        completed: list[TaskDecision] = []
        for decision in decisions:
            if not decision.session_id:
                continue
            try:
                self._execute(decision)
            except Exception as exc:
                self._record_execution(
                    decision,
                    "experiment_resume_execution_failed",
                    f"{type(exc).__name__}: {exc}",
                )
                self.orchestrator.task_store.set_status(decision.session_id, "failed")
                continue
            completed.append(decision)
        return completed

    def _execution_goal(self, session_id: str, fallback: str) -> str:
        """The user-visible task across clarification replies (see ``session_goal``)."""
        return session_goal(self.orchestrator.task_store, session_id, fallback)

    def _execute(self, decision: TaskDecision) -> dict[str, str] | None:
        launch = getattr(decision, "experiment_launch", None)
        if launch is not None:
            # Nexus already ran its new_experiment tool; report what it did.
            kind = "experiment_started" if launch.started else f"experiment_{launch.status}"
            self._record_execution(decision, kind, str(launch.as_dict()))
            return None if launch.started else launch.as_dict()

        if decision.action == ACTION_RESUME_EXPERIMENT:
            if not decision.experiment_id:
                raise RuntimeError("resume decision has no experiment id")
            experiment_id = decision.experiment_id
            experiment = self.experiment_service.resolve_experiment(experiment_id)
            try:
                work_ref = self.github_work_tracking.recover_experiment(experiment)
            except GitHubWorkTrackingError as exc:
                self._record_execution(
                    decision, "github_work_tracking_unavailable", str(exc)
                )
                raise
            self._record_execution(
                decision,
                "github_work_linked",
                f"issue=#{work_ref.issue_number} branch={work_ref.branch_name} experiment_id={experiment_id}",
            )
            self._record_execution(
                decision,
                "experiment_resume_started",
                f"experiment_id={experiment_id}",
            )
            self._start_experiment_thread(
                experiment_id,
                lambda: resume_experiment(
                    self.experiment_service,
                    experiment_id,
                    github_work_tracking=self.github_work_tracking,
                ),
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
            name=f"{EXPERIMENT_THREAD_PREFIX}{experiment_id[:8]}",
        ).start()
