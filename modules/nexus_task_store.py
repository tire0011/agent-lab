"""Persistent task/dialog state for the Nexus task orchestrator."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from modules.run_store import DEFAULT_DB_PATH, init_db

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Defensive ceiling only: bounds pathological/unbounded growth (e.g. a runaway
# command) while still storing "complete" raw results for realistic tool
# outputs, per the requirement that durable event_ids resolve to the full
# result rather than a value the orchestrator already truncated before
# persistence.
MAX_EVENT_CONTENT_CHARS = 200_000


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

                -- One row per controller turn (#158). Until this existed the
                -- only record of which model actually decided a turn was a log
                -- line, so answering "how often does the local controller
                -- handle a turn, and does it produce a usable decision" meant
                -- grepping the systemd journal - retention-bound, unqueryable
                -- from the API, and impossible to separate from test runs.
                CREATE TABLE IF NOT EXISTS nexus_controller_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    provider_used TEXT NOT NULL,
                    escalated INTEGER NOT NULL,
                    prompt_chars INTEGER NULL,
                    safe_local_budget_chars INTEGER NULL,
                    outcome TEXT NOT NULL,
                    detail TEXT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_nexus_controller_turns_session
                    ON nexus_controller_turns(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_nexus_controller_turns_created
                    ON nexus_controller_turns(created_at);
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
    ) -> int:
        """Persist the full bounded event; prompt history is compacted separately.

        Returns the durable event id so callers can reference the raw stored
        content later (e.g. to compact an observation to a stub in a prompt
        while still being able to retrieve the full result by id).
        """
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO nexus_task_events
                   (session_id, role, kind, content, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role[:32],
                    kind[:64],
                    content[:MAX_EVENT_CONTENT_CHARS],
                    json.dumps(metadata or {}, separators=(",", ":")),
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE nexus_task_sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        """Fetch one full, raw event by its durable id.

        Used to retrieve complete tool results that were compacted to a stub
        reference in a prompt instead of being resent inline.
        """
        with self._lock:
            row = self._conn.execute(
                """SELECT id, session_id, role, kind, content, metadata_json, created_at
                   FROM nexus_task_events WHERE id=?""",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "role": row["role"],
            "kind": row["kind"],
            "content": row["content"],
            "metadata": metadata,
            "created_at": row["created_at"],
        }

    def recent_events(
        self,
        session_id: str,
        *,
        role: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recent raw journal events for durable internal state lookups."""
        limit = max(1, min(limit, 100))
        conditions = ["session_id=?"]
        params: list[Any] = [session_id]
        if role is not None:
            conditions.append("role=?")
            params.append(role[:32])
        if kind is not None:
            conditions.append("kind=?")
            params.append(kind[:64])
        params.append(limit)
        where_clause = " AND ".join(conditions)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT id, session_id, role, kind, content, metadata_json, created_at
                    FROM nexus_task_events
                    WHERE {where_clause}
                    ORDER BY id DESC LIMIT ?""",
                params,
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            events.append(
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "role": row["role"],
                    "kind": row["kind"],
                    "content": row["content"],
                    "metadata": metadata,
                    "created_at": row["created_at"],
                }
            )
        return events

    def history(
        self,
        session_id: str,
        *,
        limit: int = 24,
        max_chars: int = 9000,
        max_event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent history compact enough for a small local context window.

        Storage keeps larger event bodies. Only this prompt-facing view truncates
        individual events and stops once the aggregate character budget is hit.

        ``max_event_id`` excludes any event with a strictly greater id. Callers
        that already track the current run's own tool observations in memory
        (see ``NexusTaskOrchestrator``) pass the id of the event that started
        the current run so those in-memory observations are never duplicated
        by also being re-read back out of persisted history.
        """
        limit = max(1, min(limit, 100))
        max_chars = max(1000, min(max_chars, 32000))
        with self._lock:
            if max_event_id is None:
                rows = self._conn.execute(
                    """SELECT role, kind, content, metadata_json, created_at
                       FROM nexus_task_events WHERE session_id=?
                       ORDER BY id DESC LIMIT ?""",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT role, kind, content, metadata_json, created_at
                       FROM nexus_task_events WHERE session_id=? AND id<=?
                       ORDER BY id DESC LIMIT ?""",
                    (session_id, max_event_id, limit),
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

    def record_controller_turn(
        self,
        session_id: str,
        *,
        provider_used: str,
        outcome: str,
        prompt_chars: int | None = None,
        safe_local_budget_chars: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Persist what decided one controller turn, and what came of it.

        Recording is best-effort on purpose: instrumentation must never be the
        reason a turn fails.
        """
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO nexus_controller_turns (
                        session_id, created_at, provider_used, escalated,
                        prompt_chars, safe_local_budget_chars, outcome, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        _now(),
                        str(provider_used)[:64],
                        0 if provider_used == "local" else 1,
                        prompt_chars,
                        safe_local_budget_chars,
                        str(outcome)[:64],
                        str(detail)[:500] if detail else None,
                    ),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001 - never break a turn to record it
            logger.exception("failed to record controller turn for %s", session_id)

    def controller_turn_stats(self, since: str | None = None) -> dict[str, Any]:
        """Aggregate the question this table exists to answer."""
        where, params = ("WHERE created_at >= ?", (since,)) if since else ("", ())
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT provider_used, escalated, outcome, COUNT(*) AS n
                FROM nexus_controller_turns {where}
                GROUP BY provider_used, escalated, outcome
                """,
                params,
            ).fetchall()
        total = sum(int(r["n"]) for r in rows)
        escalated = sum(int(r["n"]) for r in rows if int(r["escalated"]))
        by_outcome: dict[str, int] = {}
        by_provider: dict[str, int] = {}
        for r in rows:
            by_outcome[str(r["outcome"])] = by_outcome.get(str(r["outcome"]), 0) + int(r["n"])
            by_provider[str(r["provider_used"])] = by_provider.get(str(r["provider_used"]), 0) + int(r["n"])
        return {
            "total_turns": total,
            "escalated_turns": escalated,
            "escalation_rate": round(escalated / total, 4) if total else 0.0,
            "by_provider": by_provider,
            "by_outcome": by_outcome,
        }

    def stale_sessions(self, older_than_seconds: int) -> list[NexusTaskSession]:
        """Sessions left mid-turn with nothing driving them.

        A session only reaches a terminal status through ``_persist_final``. If
        the turn never gets there - the process died, an exception escaped, the
        loop hit a limit - the row simply stays ``active`` forever. On the live
        database 17 of 41 sessions ever started were in that state, untouched
        for 8 to 19 days, with nothing reaping or reporting them.

        Liveness is inferred from ``updated_at`` because there is no heartbeat
        (see #158). The threshold therefore has to sit well clear of a slow but
        healthy turn.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(60, older_than_seconds))
        ).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM nexus_task_sessions
                WHERE status IN ('active', 'queued') AND updated_at < ?
                ORDER BY updated_at
                """,
                (cutoff,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def abandon_stale_sessions(self, older_than_seconds: int) -> list[NexusTaskSession]:
        """Give up on stale sessions explicitly instead of leaving them ``active``.

        Deliberately does *not* resume them: re-running a controller turn costs
        a model call and may repeat whatever killed it. Marking the state
        honestly is the useful part - it makes the count visible and stops a
        dead session from looking like work in progress.
        """
        stale = self.stale_sessions(older_than_seconds)
        for session in stale:
            self.set_status(session.id, "abandoned")
            self.append_event(
                session.id,
                role="system",
                kind="abandoned",
                content=(
                    "Session was left mid-turn and had no activity for over "
                    f"{max(60, older_than_seconds)}s; marked abandoned."
                ),
            )
        return stale

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
