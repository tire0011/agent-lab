"""Input adapters for the generic Nexus task controller."""

from __future__ import annotations

from dataclasses import dataclass

from data_sources import list_ideas


@dataclass(slots=True)
class NexusSource:
    source_type: str
    source_id: str | None
    title: str | None
    content: str
    metadata: dict


class NexusSourceError(RuntimeError):
    pass


def task_source(content: str, *, source_id: str | None = None, title: str | None = None) -> NexusSource:
    if not content or not content.strip():
        raise NexusSourceError("task content must not be empty")
    return NexusSource("task", source_id, title, content.strip(), {})


def idea_source(idea_id: str) -> NexusSource:
    for idea in list_ideas():
        if idea.id == idea_id:
            return NexusSource(
                source_type="idea",
                source_id=idea.id,
                title=f"Idea: {idea.section}",
                content=idea.text,
                metadata={"section": idea.section, "state": idea.state},
            )
    raise NexusSourceError(f"unknown idea {idea_id!r}")


def issue_source(
    issue_number: int,
    *,
    title: str,
    body: str,
    state: str | None = None,
    url: str | None = None,
) -> NexusSource:
    """Wrap issue data supplied by a GitHub/MCP adapter for Nexus; fetching stays outside the
    controller so the same source works with the official GitHub MCP, a future global MCP gateway
    or a test adapter."""
    if issue_number <= 0:
        raise NexusSourceError("issue number must be positive")
    clean_title = (title or f"Issue #{issue_number}").strip()[:500]
    clean_body = (body or "").strip()[:16000]
    return NexusSource(
        source_type="issue",
        source_id=str(issue_number),
        title=clean_title,
        content=f"GitHub Issue #{issue_number}: {clean_title}\n\n{clean_body}".strip(),
        metadata={"state": state, "url": url},
    )
