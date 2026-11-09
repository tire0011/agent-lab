"""One-time migration of Nexus's local work lists into GitHub Issues (#41).

Sources, all read-only:

- **problems**: experiments in the local SQLite store whose *latest* attempt
  at a goal failed, timed out, was rejected or needs review. A goal that names
  a GitHub Issue is already represented by it; a goal with a later success is
  done; neither is migrated, nor is a goal in ``RESOLVED_LOCAL_ITEMS``.
- **todos**: ideas still ``new``/``exploring``/``selected`` in ``IDEAS.md``
  and the proposed issues in ``docs/ideas-to-issues.md`` (#340).

Every candidate is compared with *all* existing Issues (open and closed) by
normalized title and by a ``nexus-migration-key`` line in the body, so a
second run creates nothing new. Issues are created through the GitHub MCP
server, not through a separate REST client. This is deliberately not a sync
subsystem: after the migration, new todos and problems go to GitHub directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from modules.redaction import redact

PROBLEM_STATUSES = frozenset({"failed", "timed_out", "rejected", "needs_review", "interrupted"})
SUCCESS_STATUS = "succeeded"
OPEN_IDEA_STATES = frozenset({"new", "exploring", "selected"})
KEY_PREFIX = "nexus-migration-key:"
_MAX_TITLE_CHARS = 110
_MAX_GOAL_CHARS = 4000
_MAX_ERROR_CHARS = 1500
_MAX_LIST_PAGES = 50
_PER_PAGE = 100

# "GitHub Issue #41:", "solve github issue #340", "Fix agent-lab issue #57".
_ISSUE_REFERENCE_RE = re.compile(r"\bissue\s*#\d+", re.IGNORECASE)
# A resume of another experiment is that experiment's problem, not a new one.
_RESUME_RE = re.compile(r"^resume\b.*\bexperiment\s+[0-9a-f]{32}\b", re.IGNORECASE | re.DOTALL)
_PROPOSAL_HEADING_RE = re.compile(r"^###\s+P\d+\s+(?P<title>.+?)\s*$")
_MARKDOWN_PREFIX_RE = re.compile(r"^(?:#+\s*|task:\s*|[-*]\s+)", re.IGNORECASE)
_TITLE_LABEL_RE = re.compile(r"^(?:problem|todo|idea)\s*:\s*", re.IGNORECASE)


# Local candidates reviewed against the code on 2026-11-09 and found done or
# obsolete. A failed experiment stays "failed" in the store forever even when
# a later, differently worded task delivered the change, so these cannot be
# told apart automatically. Key -> evidence.
RESOLVED_LOCAL_ITEMS: dict[str, str] = {
    "nexus-problem-bbaf7eead84a": "version feature exists: nexus_version.py, agent.py --version",
    "nexus-problem-af0c1c642aab": "version feature exists: nexus_version.py, agent.py --version",
    "nexus-problem-62b52b48a131": "version feature exists: nexus_version.py, agent.py --version",
    "nexus-problem-363861302226": "version feature exists: nexus_version.py, agent.py --version",
    "nexus-problem-734938bf545a": "one-off end-to-end smoke test, superseded by later experiments",
    "nexus-problem-91171aa86d60": "mailbox approval step removed in #264",
    "nexus-problem-3b4dde4942cd": "one-off read-only evaluation of the local LLM, not a work item",
    "nexus-problem-5e8710758910": "web UI removed in #517",
    "nexus-problem-482be4c51e44": "web UI removed in #517",
    "nexus-problem-8f73a4aedbcb": "web UI removed in #517",
    "nexus-problem-c6a0ad3931e7": "web UI removed in #517; outbox/activity readable via /activity/public and the mailbox repo",
    "nexus-problem-82ac8333d248": "web UI removed in #517; /activity/public and /diagnostics/public exist",
    "nexus-problem-ff31ce8ece36": "verification env isolated in modules/verification_env.py",
    "nexus-todo-a824ba2ab4f0": "web UI removed in #517",
    "nexus-todo-8e69ef9294e9": "web UI removed in #517",
}


@dataclass(slots=True)
class MigrationItem:
    kind: str  # "problem" or "todo"
    title: str
    body: str
    key: str
    source: str


@dataclass(slots=True)
class MigrationDecision:
    item: MigrationItem
    action: str  # "create", "skip_duplicate", "created", "failed"
    detail: str = ""


def normalize_title(text: str) -> str:
    text = _TITLE_LABEL_RE.sub("", (text or "").strip())
    text = re.sub(r"[`*_\"']", "", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.rstrip(".:;!? ")


def migration_key(kind: str, title: str) -> str:
    digest = hashlib.sha1(f"{kind}\n{normalize_title(title)}".encode("utf-8")).hexdigest()
    return f"nexus-{kind}-{digest[:12]}"


def _goal_title(goal: str) -> str:
    for line in (goal or "").splitlines():
        line = _MARKDOWN_PREFIX_RE.sub("", line.strip()).strip()
        if line:
            if len(line) > _MAX_TITLE_CHARS:
                line = line[: _MAX_TITLE_CHARS - 1].rstrip() + "…"
            return line
    return ""


def _footer(key: str, source: str) -> str:
    return (
        f"\n\n---\nMigrated once from {source} (#41). GitHub Issues are now the "
        f"durable list for Nexus todos and problems.\n\n`{KEY_PREFIX} {key}`\n"
    )


# --------------------------------------------------------------------- sources


def experiment_problems(db_path: str | Path) -> list[MigrationItem]:
    """Still-open problem goals from the experiment store, newest first."""
    path = Path(db_path)
    if not path.is_file():
        return []
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute("PRAGMA table_info(experiments)")}
        if not {"id", "goal", "status", "created_at"}.issubset(columns):
            return []
        error_col = "error" if "error" in columns else "NULL AS error"
        ended_col = "ended_at" if "ended_at" in columns else "NULL AS ended_at"
        rows = conn.execute(
            f"SELECT id, goal, status, created_at, {ended_col}, {error_col} "
            "FROM experiments ORDER BY created_at DESC, id DESC"
        ).fetchall()

    groups: dict[str, list[sqlite3.Row]] = {}
    titles: dict[str, str] = {}
    for row in rows:
        goal = row["goal"] or ""
        if _ISSUE_REFERENCE_RE.search(goal) or _RESUME_RE.match(goal.strip()):
            continue
        title = _goal_title(goal)
        if not title:
            continue
        normalized = normalize_title(title)
        groups.setdefault(normalized, []).append(row)
        titles.setdefault(normalized, title)

    items: list[MigrationItem] = []
    for normalized, attempts in groups.items():
        statuses = {row["status"] for row in attempts}
        latest = attempts[0]  # rows are newest first
        if SUCCESS_STATUS in statuses or latest["status"] not in PROBLEM_STATUSES:
            continue
        title = f"Problem: {titles[normalized]}"
        key = migration_key("problem", title)
        attempt_lines = "\n".join(
            f"- `{row['id']}` - {row['status']} ({(row['ended_at'] or row['created_at'] or '')[:10]})"
            for row in attempts[:10]
        )
        error = redact((latest["error"] or "").strip())[:_MAX_ERROR_CHARS] or "(none recorded)"
        goal = redact((latest["goal"] or "").strip())
        if len(goal) > _MAX_GOAL_CHARS:
            goal = goal[:_MAX_GOAL_CHARS] + "\n\n[... truncated]"
        body = (
            f"A Nexus experiment for this goal ended as **{latest['status']}** and no later "
            f"attempt succeeded.\n\n### Attempts\n{attempt_lines}\n\n"
            f"### Last error\n```\n{error}\n```\n\n### Original goal\n\n{goal}"
            + _footer(key, "the local Nexus experiment store")
        )
        items.append(MigrationItem("problem", title, body, key, f"experiment {latest['id']}"))
    return items


def idea_todos(ideas: Iterable[Any]) -> list[MigrationItem]:
    """Open ideas from IDEAS.md (``data_sources.list_ideas()`` objects)."""
    items: list[MigrationItem] = []
    for idea in ideas:
        if getattr(idea, "state", "new") not in OPEN_IDEA_STATES:
            continue
        text = redact(str(getattr(idea, "text", "")).strip())
        if not text:
            continue
        title = _goal_title(text)
        key = migration_key("todo", title)
        body = (
            f"{text}\n\nSection: {getattr(idea, 'section', '')}; state: {idea.state}."
            + _footer(key, "IDEAS.md")
        )
        items.append(MigrationItem("todo", title, body, key, f"IDEAS.md {getattr(idea, 'id', '')}"))
    return items


def proposal_todos(markdown: str) -> list[MigrationItem]:
    """The ``### P<n>`` proposals under ``## Proposed issues`` (#340)."""
    items: list[MigrationItem] = []
    current: tuple[str, list[str]] | None = None
    in_section = False

    def _flush() -> None:
        if current is None:
            return
        title, lines = current
        key = migration_key("todo", title)
        body = redact("\n".join(lines).strip()) + _footer(key, "docs/ideas-to-issues.md")
        items.append(MigrationItem("todo", title, body, key, "docs/ideas-to-issues.md"))

    for line in markdown.splitlines():
        if line.startswith("## "):
            _flush()
            current = None
            in_section = line.strip().lower() == "## proposed issues"
            continue
        if not in_section:
            continue
        match = _PROPOSAL_HEADING_RE.match(line)
        if match:
            _flush()
            current = (match.group("title"), [])
        elif current is not None:
            current[1].append(line)
    _flush()
    return items


def collect_items(
    *,
    db_path: str | Path,
    ideas: Iterable[Any] = (),
    proposals_markdown: str = "",
) -> list[MigrationItem]:
    """All candidates, with duplicates inside the local sources folded."""
    seen: set[str] = set()
    result: list[MigrationItem] = []
    for item in [
        *experiment_problems(db_path),
        *idea_todos(ideas),
        *proposal_todos(proposals_markdown),
    ]:
        if item.key in RESOLVED_LOCAL_ITEMS:
            continue
        normalized = normalize_title(item.title)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(item)
    return result


# ---------------------------------------------------------------- GitHub (MCP)


def _issues_from(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Issues and a next cursor from a list_issues result of any server version."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], None
    if not isinstance(payload, dict):
        return [], None
    issues = payload.get("issues") or payload.get("items") or []
    page_info = payload.get("pageInfo") or payload.get("page_info") or {}
    cursor = page_info.get("endCursor") if page_info.get("hasNextPage") else None
    return [item for item in issues if isinstance(item, dict)], cursor


def _list_states(schema: dict[str, Any]) -> list[str]:
    enum = [str(value) for value in (schema.get("properties", {}).get("state", {}).get("enum") or [])]
    if "all" in enum:
        return ["all"]
    if "OPEN" in enum or "CLOSED" in enum:
        return ["OPEN", "CLOSED"]
    return ["open", "closed"]


async def existing_issues(session: Any, tools: dict[str, Any]) -> list[dict[str, Any]]:
    """Every Issue (open and closed) of the configured repository."""
    if "list_issues" not in tools:
        raise RuntimeError("the GitHub MCP server offers no list_issues tool")
    schema = getattr(tools["list_issues"], "input_schema", None) or {}
    cursor_mode = "after" in schema.get("properties", {})
    config = session.config
    found: list[dict[str, Any]] = []
    for state in _list_states(schema):
        cursor: str | None = None
        for page in range(1, _MAX_LIST_PAGES + 1):
            args: dict[str, Any] = {
                "owner": config.owner,
                "repo": config.repo,
                "state": state,
                "perPage": _PER_PAGE,
            }
            if cursor_mode and cursor:
                args["after"] = cursor
            elif not cursor_mode:
                args["page"] = page
            result = await session.call_tool("list_issues", args)
            if result.is_error:
                raise RuntimeError(f"list_issues failed: {result.text[:300]}")
            issues, cursor = _issues_from(result.json())
            found.extend(issues)
            if cursor_mode and not cursor:
                break
            if not cursor_mode and len(issues) < _PER_PAGE:
                break
    return found


def plan(items: list[MigrationItem], issues: list[dict[str, Any]]) -> list[MigrationDecision]:
    by_title = {
        normalize_title(str(issue.get("title") or "")): issue for issue in issues
    }
    bodies = [str(issue.get("body") or "") for issue in issues]
    decisions: list[MigrationDecision] = []
    for item in items:
        match = by_title.get(normalize_title(item.title))
        if match is not None:
            decisions.append(
                MigrationDecision(item, "skip_duplicate", f"#{match.get('number')} has the same title")
            )
        elif any(f"{KEY_PREFIX} {item.key}" in body for body in bodies):
            decisions.append(MigrationDecision(item, "skip_duplicate", "already migrated (key found)"))
        else:
            decisions.append(MigrationDecision(item, "create"))
    return decisions


def _create_call(tools: dict[str, Any], config: Any, item: MigrationItem) -> tuple[str, dict[str, Any]]:
    args = {"owner": config.owner, "repo": config.repo, "title": item.title, "body": item.body}
    if "issue_write" in tools:
        return "issue_write", {"method": "create", **args}
    if "create_issue" in tools:
        return "create_issue", args
    raise RuntimeError("the GitHub MCP server offers neither issue_write nor create_issue")


async def apply(session: Any, tools: dict[str, Any], decisions: list[MigrationDecision]) -> None:
    for decision in decisions:
        if decision.action != "create":
            continue
        name, args = _create_call(tools, session.config, decision.item)
        result = await session.call_tool(name, args)
        if result.is_error:
            decision.action = "failed"
            decision.detail = redact(result.text[:300])
            continue
        payload = result.json()
        reference = ""
        if isinstance(payload, dict):
            reference = str(payload.get("html_url") or payload.get("url") or payload.get("number") or "")
        decision.action = "created"
        decision.detail = reference


def report(decisions: list[MigrationDecision]) -> str:
    lines = []
    for decision in decisions:
        detail = f" - {decision.detail}" if decision.detail else ""
        lines.append(f"[{decision.action}] ({decision.item.kind}) {decision.item.title}{detail}")
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.action] = counts.get(decision.action, 0) + 1
    lines.append("summary: " + json.dumps(counts, sort_keys=True))
    return "\n".join(lines)
