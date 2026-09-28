"""Generic execution entry point for task, issue, idea and discussion work."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from modules.experiment_handover import with_handover
from modules.experiment_wait import resume_after_experiment, run_then_resume
from modules.experiment_resume import resume_experiment
from modules.github_work_tracking import GitHubWorkTrackingError, GitHubWorkTrackingService
from modules.mailbox.development import process_command
from modules.nexus_sources import NexusSource, idea_source, issue_source, task_source
from modules.run_store import DEFAULT_DB_PATH
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

    GitHub work tracking is the mandatory outer lifecycle: a
    durable Issue and human-readable remote branch are resolved/created before
    a new implementation experiment starts. The isolated experiment worktree
    remains an internal execution detail.
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
        self._execute(decision, source.content, source=source)
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

    def resume_available_capabilities(self, *, limit: int = 1) -> list[TaskDecision]:
        """Continue blocked sessions when their required capability is now present."""
        decisions = self.orchestrator.resume_available_capabilities(limit=limit)
        completed: list[TaskDecision] = []
        for decision in decisions:
            if not decision.session_id:
                continue
            try:
                self._execute(
                    decision,
                    self._execution_goal(decision.session_id, ""),
                )
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
                self._execute(decision, self._execution_goal(decision.session_id, ""))
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

    def _execute(
        self,
        decision: TaskDecision,
        source_content: str,
        *,
        source: NexusSource | None = None,
    ) -> None:
        if decision.action == ACTION_NEW_EXPERIMENT:
            lifecycle_source = source or task_source(
                source_content,
                source_id=decision.session_id,
                title="Nexus controller task",
            )
            try:
                work_ref = self.github_work_tracking.ensure_for_source(lifecycle_source)
            except GitHubWorkTrackingError as exc:
                self._record_execution(
                    decision, "github_work_tracking_unavailable", str(exc)
                )
                raise

            experiment = self.experiment_service.create_experiment(
                with_handover(source_content, decision.experiment_handover)
            )
            work_ref = self.github_work_tracking.bind_experiment(work_ref, experiment.id)
            self._record_execution(
                decision,
                "github_work_linked",
                (
                    f"issue=#{work_ref.issue_number} "
                    f"branch={work_ref.branch_name} experiment_id={experiment.id}"
                ),
            )
            self._record_execution(
                decision,
                "experiment_started",
                f"experiment_id={experiment.id}",
            )
            # Nexus pauses on the experiment and continues the moment it ends (#263).
            if decision.session_id:
                self.orchestrator.task_store.wait_for_experiment(
                    decision.session_id, experiment.id
                )
            self._start_experiment_thread(
                experiment.id,
                lambda: run_then_resume(
                    lambda: self.experiment_service.run_experiment(
                        experiment.id,
                        github_work_tracking=self.github_work_tracking,
                    ),
                    lambda: self._resume_after_experiment(experiment.id),
                    experiment.id,
                ),
            )
            return

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
            name=f"nexus-controller-exp-{experiment_id[:8]}",
        ).start()
