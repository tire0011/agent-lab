"""Generic entry point for task, issue, idea and discussion work."""

from __future__ import annotations

from typing import Any

from modules.nexus_sources import NexusSource, idea_source, issue_source, task_source
from modules.task_orchestrator import NexusTaskOrchestrator, TaskDecision


class NexusControllerService:
    """Feed heterogeneous work into one persistent local-LLM controller."""

    def __init__(self, orchestrator: NexusTaskOrchestrator) -> None:
        self.orchestrator = orchestrator

    def handle_source(self, source: NexusSource) -> TaskDecision:
        return self.orchestrator.run(
            source.content,
            source_type=source.source_type,
            source_id=source.source_id,
            title=source.title,
        )

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
        return self.orchestrator.continue_session(session_id, message)
