"""Persistent task/dialog state for the Nexus task orchestrator."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from modules.run_store import DEFAULT_DB_PATH, init_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class NexusTaskSession:
    id: str
    source_type: str
    source_id: str | None
    title: str | None
    status: str
    created_at: str
    updated_at: str


class NexusTaskStore:
    """Small durable journal shared by task, issue, idea and chat inputs."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._lock = threading.RLock()
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS nexus_task_sessions (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NULL,
                    title TEXT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_nexus_task_source
                    ON nexus_task_sessions(source_type, source_id)
                    WHERE source_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_nexus_task_status
                    ON nexus_task_sessions(status, updated_at);

                CREATE TABLE IF NOT EXISTS nexus_task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES nexus_task_sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_nexus_task_events_session
                    ON nexus_task_events(session_id, id);
                """
            )

    def close(self) -> None:
        self._conn.close()

    def get_or_create(
        self,
        *,
        source_type: str,
        source_id: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> NexusTaskSession:
        source_type = (source_type or "task").strip()[:64]
        source_id = (source_id or "").strip()[:256] or None
        with self._lock:
            if session_id:
                row = self._conn.execute(
                    "SELECT * FROM nexus_task_sessions WHERE id=?", (session_id,)
                ).fetchone()
                if row is not None:
                    return self._row(row)
            if source_id is not None:
                row = self._conn.execute(
                    "SELECT * FROM nexus_task_sessions WHERE source_type=? AND source_id=?",
                    (source_type, source_id),
                ).fetchone()
                if row is not None:
                    return self._row(row)
            now = _now()
            sid = session_id or uuid.uuid4().hex
            self._conn.execute(
                """INSERT INTO nexus_task_sessions
                   (id, source_type, source_id, title, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (sid, source_type, source_id, (title or "")[:500] or None, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM nexus_task_sessions WHERE id=?", (sid,)
            ).fetchone()
            return self._row(row)

    def append_event(
        self,
        session_id: str,
        *,
        role: str,
        kind: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist the full bounded event; prompt history is compacted separately."""
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO nexus_task_events
                   (session_id, role, kind, content, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role[:32],
                    kind[:64],
                    content[:16000],
                    json.dumps(metadata or {}, separators=(",", ":")),
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE nexus_task_sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )
            self._conn.commit()

    def history(
        self,
        session_id: str,
        *,
        limit: int = 24,
        max_chars: int = 9000,
    ) -> list[dict[str, Any]]:
        """Return recent history compact enough for a small local context window.

        Storage keeps larger event bodies. Only this prompt-facing view truncates
        individual events and stops once the aggregate character budget is hit.
        """
        limit = max(1, min(limit, 100))
        max_chars = max(1000, min(max_chars, 32000))
        with self._lock:
            rows = self._conn.execute(
                """SELECT role, kind, content, metadata_json, created_at
                   FROM nexus_task_events WHERE session_id=?
                   ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        newest_first: list[dict[str, Any]] = []
        used = 0
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            content = str(row["content"] or "")[:1800]
            event_cost = len(content) + 300
            if newest_first and used + event_cost > max_chars:
                break
            newest_first.append(
                {
                    "role": row["role"],
                    "kind": row["kind"],
                    "content": content,
                    "metadata": metadata,
                    "created_at": row["created_at"],
                }
            )
            used += event_cost
        return list(reversed(newest_first))

    def set_status(self, session_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE nexus_task_sessions SET status=?, updated_at=? WHERE id=?",
                (status[:64], _now(), session_id),
            )
            self._conn.commit()

    def get(self, session_id: str) -> NexusTaskSession | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nexus_task_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return self._row(row) if row is not None else None

    @staticmethod
    def _row(row: sqlite3.Row) -> NexusTaskSession:
        return NexusTaskSession(
            id=row["id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            title=row["title"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
