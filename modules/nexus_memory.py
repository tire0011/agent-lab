"""Cheap, transparent memory search for the Nexus task orchestrator.

The first implementation deliberately reuses data Nexus already has:
- structured ``knowledge`` rows in SQLite,
- prior Nexus task/dialog events,
- a small set of project memory documents.

Event journal vs. memory (#248): ``nexus_task_events`` is the event journal.
It keeps every event, including orchestration bookkeeping, for audit, replay
and failure analysis. This module is the read side of curated memory: it only
treats an event as knowledge when :func:`is_memory_eligible_event` says so.
Orchestration kinds (``ORCHESTRATION_EVENT_KINDS``) stay in the journal and are
never returned by :meth:`NexusMemory.search`. A later MemoryStore with explicit
knowledge promotion can replace that predicate without changing ``search()``.

The tool surface is intentionally stable so this lexical backend can later be
replaced by embeddings/RAG without changing orchestration prompts or callers.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from modules.nexus_task_store import NexusTaskStore
from modules.run_store import DEFAULT_DB_PATH, init_db

_MEMORY_DOCS = (
    "PROJECT_HISTORY.md",
    "DECISIONS.md",
    "ARCHITECTURE.md",
    "IDEAS.md",
)
_WORD_RE = re.compile(r"[A-Za-z0-9_./:-]{2,}")
_ISSUE_QUERY_RE = re.compile(r"\bissue\s+#?(\d+)\b", re.IGNORECASE)

# Orchestrator bookkeeping must never become project knowledge. In particular,
# indexing memory_search results makes later memory_search calls recursively
# contain earlier serialized searches (#238). The journal still stores these
# events; memory just does not read them (#248).
ORCHESTRATION_EVENT_KINDS: tuple[str, ...] = (
    "memory_search",
    "controller_decision",
    "find_tool",
    "find_tool_redirected",
    "redundant_tool_request",
    "terminal_decision_rejected",
    "knowledge_collection",
    "preflight_plan",
)


def is_memory_eligible_event(kind: str) -> bool:
    """Whether a journal event of ``kind`` may be returned as knowledge.

    This is the single place that decides which journal events count as
    memory. Today it is a deny-list of orchestration kinds; an explicit
    promotion step can replace it later.
    """
    return kind not in ORCHESTRATION_EVENT_KINDS


def _terms(query: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for term in _WORD_RE.findall(query.lower()):
        if term in seen:
            continue
        seen.add(term)
        result.append(term)
    return result[:12]


def _score(text: str, terms: list[str]) -> int:
    low = text.lower()
    return sum(3 if low.startswith(term) else low.count(term) for term in terms)


def _explicit_issue_number(query: str) -> int | None:
    match = _ISSUE_QUERY_RE.search(query)
    return int(match.group(1)) if match else None


def _mentions_issue(text: str, issue_number: int, *, source_id: str | None = None) -> bool:
    if source_id and source_id.strip() == str(issue_number):
        return True
    escaped = re.escape(str(issue_number))
    patterns = (
        rf"(?<!\d)#{escaped}(?!\d)",
        rf"\bissue\s+#?{escaped}\b",
        rf"/issues/{escaped}(?:\b|/)",
        rf'["\']number["\']\s*:\s*{escaped}\b',
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _chunks(text: str, max_chars: int = 800) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(current) + len(paragraph) + 2 > max_chars and current:
            chunks.append(current)
            current = ""
        current = (current + "\n\n" + paragraph).strip()
    if current:
        chunks.append(current)
    return chunks


class NexusMemory:
    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        task_store: NexusTaskStore | None = None,
    ) -> None:
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.task_store = task_store or NexusTaskStore(db_path=db_path)
        self._owns_task_store = task_store is None
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn

    def close(self) -> None:
        self._conn.close()
        if self._owns_task_store:
            self.task_store.close()

    def search(self, query: str, *, limit: int = 6) -> list[dict[str, Any]]:
        terms = _terms(query)
        if not terms:
            return []
        issue_number = _explicit_issue_number(query)
        limit = max(1, min(limit, 10))
        candidates: list[tuple[int, dict[str, Any]]] = []
        self._search_knowledge(terms, candidates, issue_number=issue_number)
        self._search_task_events(terms, candidates, issue_number=issue_number)
        self._search_docs(terms, candidates, issue_number=issue_number)
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [item for score, item in candidates[:limit] if score > 0]

    def _search_knowledge(
        self,
        terms: list[str],
        candidates: list[tuple[int, dict[str, Any]]],
        *,
        issue_number: int | None = None,
    ) -> None:
        try:
            rows = self._conn.execute(
                """SELECT timestamp, category, key, value, source
                   FROM knowledge ORDER BY id DESC LIMIT 500"""
            ).fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            text = f"{row['category']} {row['key']} {row['value']} {row['source']}"
            if issue_number is not None and not _mentions_issue(text, issue_number):
                continue
            score = _score(text, terms)
            if score:
                candidates.append(
                    (
                        score,
                        {
                            "source": "knowledge",
                            "category": row["category"],
                            "key": row["key"],
                            "content": str(row["value"])[:900],
                            "timestamp": row["timestamp"],
                        },
                    )
                )

    def _search_task_events(
        self,
        terms: list[str],
        candidates: list[tuple[int, dict[str, Any]]],
        *,
        issue_number: int | None = None,
    ) -> None:
        excluded_kind_placeholders = ", ".join(
            "?" for _ in ORCHESTRATION_EVENT_KINDS
        )
        try:
            rows = self._conn.execute(
                f"""SELECT e.session_id, e.role, e.kind, e.content, e.created_at,
                           s.source_type, s.source_id, s.title
                    FROM nexus_task_events e
                    JOIN nexus_task_sessions s ON s.id=e.session_id
                    WHERE e.kind NOT IN ({excluded_kind_placeholders})
                    ORDER BY e.id DESC LIMIT 500""",
                ORCHESTRATION_EVENT_KINDS,
            ).fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            if not is_memory_eligible_event(str(row["kind"])):
                continue
            text = f"{row['title'] or ''} {row['kind']} {row['content']}"
            if issue_number is not None and not _mentions_issue(
                text,
                issue_number,
                source_id=str(row["source_id"] or ""),
            ):
                continue
            score = _score(text, terms)
            if score:
                candidates.append(
                    (
                        score + 1,
                        {
                            "source": "task_history",
                            "session_id": row["session_id"],
                            "source_type": row["source_type"],
                            "source_id": row["source_id"],
                            "role": row["role"],
                            "kind": row["kind"],
                            "content": str(row["content"])[:900],
                            "timestamp": row["created_at"],
                        },
                    )
                )

    def _search_docs(
        self,
        terms: list[str],
        candidates: list[tuple[int, dict[str, Any]]],
        *,
        issue_number: int | None = None,
    ) -> None:
        for filename in _MEMORY_DOCS:
            path = self.repo_root / filename
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for index, chunk in enumerate(_chunks(text)):
                if issue_number is not None and not _mentions_issue(chunk, issue_number):
                    continue
                score = _score(chunk, terms)
                if score:
                    candidates.append(
                        (
                            score,
                            {
                                "source": "project_document",
                                "document": filename,
                                "chunk": index,
                                "content": chunk[:900],
                            },
                        )
                    )
