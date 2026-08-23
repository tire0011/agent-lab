"""Cheap, transparent memory search for the Nexus task orchestrator.

The first implementation deliberately reuses data Nexus already has:
- structured ``knowledge`` rows in SQLite,
- prior Nexus task/dialog events,
- a small set of project memory documents.

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


def _chunks(text: str, max_chars: int = 1400) -> list[str]:
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

    def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        terms = _terms(query)
        if not terms:
            return []
        limit = max(1, min(limit, 20))
        candidates: list[tuple[int, dict[str, Any]]] = []
        self._search_knowledge(terms, candidates)
        self._search_task_events(terms, candidates)
        self._search_docs(terms, candidates)
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [item for score, item in candidates[:limit] if score > 0]

    def _search_knowledge(
        self, terms: list[str], candidates: list[tuple[int, dict[str, Any]]]
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
            score = _score(text, terms)
            if score:
                candidates.append(
                    (
                        score,
                        {
                            "source": "knowledge",
                            "category": row["category"],
                            "key": row["key"],
                            "content": str(row["value"])[:1800],
                            "timestamp": row["timestamp"],
                        },
                    )
                )

    def _search_task_events(
        self, terms: list[str], candidates: list[tuple[int, dict[str, Any]]]
    ) -> None:
        try:
            rows = self._conn.execute(
                """SELECT e.session_id, e.role, e.kind, e.content, e.created_at,
                          s.source_type, s.source_id, s.title
                   FROM nexus_task_events e
                   JOIN nexus_task_sessions s ON s.id=e.session_id
                   ORDER BY e.id DESC LIMIT 500"""
            ).fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            text = f"{row['title'] or ''} {row['kind']} {row['content']}"
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
                            "content": str(row["content"])[:1800],
                            "timestamp": row["created_at"],
                        },
                    )
                )

    def _search_docs(
        self, terms: list[str], candidates: list[tuple[int, dict[str, Any]]]
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
                score = _score(chunk, terms)
                if score:
                    candidates.append(
                        (
                            score,
                            {
                                "source": "project_document",
                                "document": filename,
                                "chunk": index,
                                "content": chunk[:1800],
                            },
                        )
                    )
