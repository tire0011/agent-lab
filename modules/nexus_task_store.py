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

from modules.heartbeat import (
    heartbeat_age_seconds,
    is_stale,
    resolve_interval,
    resolve_stale_timeout,
)
from modules.run_store import DEFAULT_DB_PATH, init_db

logger = logging.getLogger(__name__)

# Statuses in which a live worker should be driving the session; only these
# can go stale. Every status a worker sets while waiting on a model call
# belongs here, or a worker dying in that window leaves an invisible session.
WORKING_STATUSES: tuple[str, ...] = (
    "active",
    "queued",
    "waiting_for_llm",
    "waiting_for_tool_resolver",
    "waiting_for_knowledge",
)

# Intentional resting states. A session waiting for a human reply, or blocked
# on a capability nobody has built yet, has no worker by design - silence there
# is correct, not death (#162).
RESTING_STATUSES: tuple[str, ...] = (
    "waiting_for_user",
    "blocked_missing_capability",
    # Paused while an experiment it started runs; resumed with the result (#263).
    "waiting_for_experiment",
)
STATUS_WAITING_FOR_EXPERIMENT = "waiting_for_experiment"

STATUS_ABANDONED = "abandoned"
STATUS_FAILED = "failed"
TERMINAL_REASON_HEARTBEAT_TIMEOUT = "heartbeat_timeout"

# A turn that died in the open records how, instead of staying `active` with a
# frozen heartbeat until the reaper finds it.
TERMINAL_REASON_TURN_FAILED = "turn_failed"
TERMINAL_REASON_TURN_CANCELLED = "turn_cancelled"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Defensive ceiling against runaway output; realistic tool results are stored
# complete, so event ids resolve to the full result.
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
    worker_id: str | None = None
    claimed_at: str | None = None
    last_heartbeat_at: str | None = None
    terminal_reason: str | None = None
    requires_completion_evidence: bool = False
    # The implementation request in force and the requests with observed
    # evidence: comparing keys keeps one task's evidence from completing another.
    completion_intent_key: str | None = None
    completion_evidence_key: str | None = None
    verification_evidence_key: str | None = None
    waiting_experiment_id: str | None = None


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
            self._migrate_session_columns()

    def _migrate_session_columns(self) -> None:
        """Add the explicit ownership/liveness columns (#162).

        ``updated_at`` moves on any change, so it was never a liveness signal.
        """
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(nexus_task_sessions)")
        }
        for column in ("worker_id", "claimed_at", "last_heartbeat_at", "terminal_reason"):
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE nexus_task_sessions ADD COLUMN {column} TEXT NULL"
                )
        if "requires_completion_evidence" not in existing:
            self._conn.execute(
                "ALTER TABLE nexus_task_sessions "
                "ADD COLUMN requires_completion_evidence INTEGER NOT NULL DEFAULT 0"
            )
        for column in (
            "completion_intent_key",
            "completion_evidence_key",
            "verification_evidence_key",
            "waiting_experiment_id",
        ):
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE nexus_task_sessions ADD COLUMN {column} TEXT NULL"
                )
        self._conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_nexus_task_heartbeat
                   ON nexus_task_sessions(status, last_heartbeat_at)"""
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
        """Persist the full bounded event and return its durable id.

        Prompt history is compacted separately; the id retrieves the full result.
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

    def tool_result_content(
        self, session_id: str, event_id: int, kinds: frozenset[str] | set[str]
    ) -> str | None:
        """The stored content of one tool-call result of this session, else ``None``.

        The access rule of ``read_tool_result`` (#562) is the WHERE clause: same
        session, ``role='tool'``, and a kind from ``kinds``. Only ``content`` is
        selected - never metadata - so nothing else can leak through a read.
        """
        allowed = sorted(str(kind)[:64] for kind in kinds)
        if not session_id or not allowed:
            return None
        placeholders = ",".join("?" for _ in allowed)
        with self._lock:
            row = self._conn.execute(
                f"""SELECT content FROM nexus_task_events
                    WHERE id=? AND session_id=? AND role='tool'
                      AND kind IN ({placeholders})""",
                (int(event_id), session_id, *allowed),
            ).fetchone()
        return None if row is None else str(row["content"])

    def user_inputs(self, session_id: str) -> list[str]:
        """Complete user input events in order: a durable read, no history limit."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT content, metadata_json
                   FROM nexus_task_events
                   WHERE session_id=? AND role='user' AND kind='input'
                   ORDER BY id ASC""",
                (session_id,),
            ).fetchall()

        inputs: list[str] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            if metadata.get("knowledge_collector_internal"):
                continue
            inputs.append(str(row["content"] or ""))
        return inputs

    def history(
        self,
        session_id: str,
        *,
        limit: int = 24,
        max_chars: int = 9000,
        max_event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Recent history, compact enough for a small local context window.

        Only this prompt view truncates events and stops at the character
        budget. ``max_event_id`` excludes newer events, so observations the
        current run holds in memory are not read back a second time.
        """
        limit = max(1, min(limit, 100))
        max_chars = max(1000, min(max_chars, 32000))
        # Collector-internal lookups stay in the journal but not in prompt
        # history; over-fetch so hidden rows do not crowd out visible ones.
        fetch_limit = min(limit * 3, 300)
        with self._lock:
            if max_event_id is None:
                rows = self._conn.execute(
                    """SELECT role, kind, content, metadata_json, created_at
                       FROM nexus_task_events WHERE session_id=?
                       ORDER BY id DESC LIMIT ?""",
                    (session_id, fetch_limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT role, kind, content, metadata_json, created_at
                       FROM nexus_task_events WHERE session_id=? AND id<=?
                       ORDER BY id DESC LIMIT ?""",
                    (session_id, max_event_id, fetch_limit),
                ).fetchall()
        newest_first: list[dict[str, Any]] = []
        used = 0
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            if metadata.get("knowledge_collector_internal"):
                continue
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
            if len(newest_first) >= limit:
                break
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

    def stale_sessions(self, older_than_seconds: int) -> list[NexusTaskSession]:
        """Sessions left mid-turn with nothing driving them.

        Legacy safety net for sessions that never reported a heartbeat
        (pre-#162 rows, unclaimed work): liveness can only come from
        ``updated_at``, so the threshold sits well clear of a slow healthy
        turn. Claimed work is handled by ``reconcile_stale_heartbeats``.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(60, older_than_seconds))
        ).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM nexus_task_sessions
                WHERE status IN ('active', 'queued')
                  AND last_heartbeat_at IS NULL
                  AND updated_at < ?
                ORDER BY updated_at
                """,
                (cutoff,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def abandon_stale_sessions(self, older_than_seconds: int) -> list[NexusTaskSession]:
        """Mark stale sessions as given up instead of leaving them ``active``.

        Never resumes them: re-running a turn costs a model call and may
        repeat what killed it.
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

    # Ownership and liveness (#162): a worker claims a session, refreshes
    # ``last_heartbeat_at`` while alive and releases the claim with a terminal
    # reason. A claim whose heartbeats stopped is reconciled to ``abandoned``.

    def claim_blocked_missing_capability(
        self,
        session_id: str,
        *,
        worker_id: str,
    ) -> bool:
        """Atomically claim a resting capability-blocked session exactly once."""
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE nexus_task_sessions
                   SET worker_id=?, claimed_at=?, last_heartbeat_at=?,
                       terminal_reason=NULL, status='active', updated_at=?
                   WHERE id=? AND status='blocked_missing_capability'
                     AND last_heartbeat_at IS NULL""",
                (str(worker_id)[:128], now, now, now, session_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def claim_session(self, session_id: str, *, worker_id: str) -> bool:
        """Record who owns this session now, and start its heartbeat.

        Unconditional, so a restarted worker can take over its own abandoned
        session; the claim identity makes the takeover visible, and
        ``heartbeat`` stops the previous owner from keeping the claim alive.
        """
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE nexus_task_sessions
                   SET worker_id=?, claimed_at=?, last_heartbeat_at=?,
                       terminal_reason=NULL, updated_at=?
                   WHERE id=?""",
                (str(worker_id)[:128], now, now, now, session_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def heartbeat(self, session_id: str, *, worker_id: str) -> bool:
        """Report the owning worker alive; False if the claim is gone.

        Only ``last_heartbeat_at`` moves, and the ``worker_id`` guard keeps a
        reaped or replaced worker from resurrecting its claim.
        """
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE nexus_task_sessions
                   SET last_heartbeat_at=?
                   WHERE id=? AND worker_id=?""",
                (_now(), session_id, str(worker_id)[:128]),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def finalize_turn(
        self,
        session_id: str,
        *,
        worker_id: str | None,
        status: str,
        reason: str,
        event_role: str = "assistant",
        event_kind: str | None = None,
        event_content: str = "",
        event_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """End a turn atomically: journal it, set its status, release the claim.

        One transaction with one ownership check, so a turn cannot end half in
        one worker's claim and half in another's. Returns False, writing
        nothing, when the claim has moved on.
        """
        now = _now()
        clause = " AND worker_id=?" if worker_id is not None else ""
        params: list[Any] = [status[:64], str(reason)[:200], now, session_id]
        if worker_id is not None:
            params.append(str(worker_id)[:128])
        with self._lock:
            cursor = self._conn.execute(
                f"""UPDATE nexus_task_sessions
                    SET status=?, terminal_reason=?, last_heartbeat_at=NULL, updated_at=?
                    WHERE id=?{clause}""",
                params,
            )
            if cursor.rowcount == 0:
                self._conn.rollback()
                return False
            if event_kind:
                self._conn.execute(
                    """INSERT INTO nexus_task_events
                       (session_id, role, kind, content, metadata_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        event_role[:32],
                        event_kind[:64],
                        event_content[:MAX_EVENT_CONTENT_CHARS],
                        json.dumps(event_metadata or {}, separators=(",", ":")),
                        now,
                    ),
                )
            self._conn.commit()
        return True

    def stale_heartbeat_sessions(
        self, stale_after_seconds: object = None
    ) -> list[NexusTaskSession]:
        """Claimed working sessions whose worker stopped reporting itself alive.

        Resting, terminal and never-heartbeat sessions are excluded.
        """
        timeout = resolve_stale_timeout(stale_after_seconds)
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=timeout)
        ).isoformat()
        placeholders = ",".join("?" for _ in WORKING_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM nexus_task_sessions
                WHERE status IN ({placeholders})
                  AND worker_id IS NOT NULL
                  AND last_heartbeat_at IS NOT NULL
                  AND last_heartbeat_at < ?
                ORDER BY last_heartbeat_at
                """,
                (*WORKING_STATUSES, cutoff),
            ).fetchall()
        return [self._row(r) for r in rows]

    def reconcile_stale_heartbeats(
        self, stale_after_seconds: object = None
    ) -> list[NexusTaskSession]:
        """Give abandoned claims an explicit terminal outcome, exactly once.

        Idempotent: reconciling clears ``last_heartbeat_at``, which the selection
        depends on. Never re-runs the work.
        """
        timeout = resolve_stale_timeout(stale_after_seconds)
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout)).isoformat()
        stale = self.stale_heartbeat_sessions(timeout)
        placeholders = ",".join("?" for _ in WORKING_STATUSES)
        reconciled: list[NexusTaskSession] = []
        for session in stale:
            with self._lock:
                # Re-check every candidate condition inside the write, matching
                # the exact selected heartbeat: a compare-and-set, so a worker
                # that beat or took over in between is left alone.
                cursor = self._conn.execute(
                    f"""UPDATE nexus_task_sessions
                        SET status=?, terminal_reason=?, last_heartbeat_at=NULL,
                            updated_at=?
                        WHERE id=?
                          AND status IN ({placeholders})
                          AND worker_id IS ?
                          AND last_heartbeat_at IS ?
                          AND last_heartbeat_at < ?""",
                    (
                        STATUS_ABANDONED,
                        TERMINAL_REASON_HEARTBEAT_TIMEOUT,
                        _now(),
                        session.id,
                        *WORKING_STATUSES,
                        session.worker_id,
                        session.last_heartbeat_at,
                        cutoff,
                    ),
                )
                self._conn.commit()
            if cursor.rowcount == 0:
                # The claim moved on between selection and now - a fresh
                # heartbeat, a takeover, or another reaper. Either way it is
                # not ours to end, and not ours to report.
                continue
            age = heartbeat_age_seconds(session.last_heartbeat_at)
            self.append_event(
                session.id,
                role="system",
                kind=TERMINAL_REASON_HEARTBEAT_TIMEOUT,
                content=(
                    f"Worker {session.worker_id or 'unknown'} stopped sending "
                    f"heartbeats (last seen {session.last_heartbeat_at}, "
                    f"{int(age or 0)}s ago, timeout {timeout}s); claim released "
                    "and session marked abandoned."
                ),
                metadata={
                    "worker_id": session.worker_id,
                    "last_heartbeat_at": session.last_heartbeat_at,
                    "stale_after_seconds": timeout,
                    "terminal_reason": TERMINAL_REASON_HEARTBEAT_TIMEOUT,
                },
            )
            refreshed = self.get(session.id)
            reconciled.append(refreshed or session)
        return reconciled

    def liveness(self, stale_after_seconds: object = None) -> dict[str, Any]:
        """Every claim with its heartbeat age, plus working sessions with no claim (counted, not hidden)."""
        timeout = resolve_stale_timeout(stale_after_seconds)
        now = datetime.now(timezone.utc)
        placeholders = ",".join("?" for _ in WORKING_STATUSES)
        with self._lock:
            count_rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM nexus_task_sessions GROUP BY status"
            ).fetchall()
            # Only working rows can be claimed, so only they are listed: the
            # journal grows without bound and a diagnostic must not.
            rows = self._conn.execute(
                f"""SELECT * FROM nexus_task_sessions
                    WHERE status IN ({placeholders})
                    ORDER BY last_heartbeat_at IS NULL, last_heartbeat_at""",
                WORKING_STATUSES,
            ).fetchall()
        counts = {str(row["status"]): int(row["n"]) for row in count_rows}
        claims: list[dict[str, Any]] = []
        unclaimed = 0
        for row in rows:
            session = self._row(row)
            if session.last_heartbeat_at is None:
                unclaimed += 1
                continue
            age = heartbeat_age_seconds(session.last_heartbeat_at, now=now)
            claims.append(
                {
                    "session_id": session.id,
                    "source_type": session.source_type,
                    "source_id": session.source_id,
                    "title": session.title,
                    "status": session.status,
                    "worker_id": session.worker_id,
                    "claimed_at": session.claimed_at,
                    "last_heartbeat_at": session.last_heartbeat_at,
                    "heartbeat_age_seconds": round(age, 3) if age is not None else None,
                    "stale": is_stale(session.last_heartbeat_at, timeout, now=now),
                }
            )
        stale_count = sum(1 for claim in claims if claim["stale"])
        return {
            "generated_at": now.isoformat(),
            "stale_after_seconds": timeout,
            "heartbeat_interval_seconds": resolve_interval(),
            "live_claims": len(claims) - stale_count,
            "stale_claims": stale_count,
            "unclaimed_working_sessions": unclaimed,
            "resting_statuses": list(RESTING_STATUSES),
            "counts_by_status": counts,
            "claims": claims,
        }

    def blocked_missing_capabilities(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return blocked sessions with their latest structured capability requirement."""
        limit = max(1, min(limit, 500))
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM nexus_task_sessions
                   WHERE status='blocked_missing_capability'
                   ORDER BY updated_at"""
            ).fetchall()

        blocked: list[dict[str, Any]] = []
        for row in rows:
            session = self._row(row)
            events = self.recent_events(
                session.id,
                role="assistant",
                kind="missing_capability",
                limit=1,
            )
            if not events:
                continue
            metadata = events[0].get("metadata") or {}
            capability = str(metadata.get("capability") or "").strip()
            trigger_task = str(metadata.get("trigger_task") or "").strip()
            if not capability or not trigger_task:
                continue
            blocked.append(
                {
                    "session": session,
                    "capability": capability,
                    "required_inputs": list(metadata.get("required_inputs") or []),
                    "catalog_at_block": list(metadata.get("catalog_at_block") or []),
                    "trigger_task": trigger_task,
                    "blocker_event_id": events[0]["id"],
                }
            )
            if len(blocked) >= limit:
                break
        return blocked

    def wait_for_experiment(self, session_id: str, experiment_id: str) -> bool:
        """Park a session whose turn ended in new_experiment until it finishes.

        Called once the experiment exists. The session holds no claim while it
        waits, so the local model stays free for other work (#263).
        """
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE nexus_task_sessions
                   SET status=?, waiting_experiment_id=?, last_heartbeat_at=NULL,
                       updated_at=?
                   WHERE id=?""",
                (STATUS_WAITING_FOR_EXPERIMENT, str(experiment_id), _now(), session_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def find_by_source(self, source_type: str, source_id: str) -> NexusTaskSession | None:
        """The session for a source, without creating one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nexus_task_sessions WHERE source_type=? AND source_id=?",
                (source_type, str(source_id)),
            ).fetchone()
        return self._row(row) if row else None

    def sessions_waiting_for_experiments(
        self, experiment_id: str | None = None
    ) -> list[NexusTaskSession]:
        """Sessions parked on an experiment, optionally on one experiment."""
        query = "SELECT * FROM nexus_task_sessions WHERE status=?"
        params: list[Any] = [STATUS_WAITING_FOR_EXPERIMENT]
        if experiment_id is not None:
            query += " AND waiting_experiment_id=?"
            params.append(str(experiment_id))
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY updated_at", params).fetchall()
        return [self._row(row) for row in rows]

    def claim_waiting_for_experiment(
        self, session_id: str, experiment_id: str, *, worker_id: str
    ) -> bool:
        """Atomically claim a session parked on *experiment_id* exactly once.

        The event path and the polling safety net can both see the same
        finished experiment; only one of them resumes the session.
        """
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE nexus_task_sessions
                   SET worker_id=?, claimed_at=?, last_heartbeat_at=?,
                       terminal_reason=NULL, status='active',
                       waiting_experiment_id=NULL, updated_at=?
                   WHERE id=? AND status=? AND waiting_experiment_id=?""",
                (
                    str(worker_id)[:128], now, now, now, session_id,
                    STATUS_WAITING_FOR_EXPERIMENT, str(experiment_id),
                ),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def set_requires_completion_evidence(
        self,
        session_id: str,
        required: bool,
        *,
        intent_key: str | None = None,
    ) -> None:
        """Persist that this session owes completion evidence for *intent_key*.

        A new request replaces the key, so evidence for the previous one cannot
        satisfy it.
        """
        with self._lock:
            self._conn.execute(
                """UPDATE nexus_task_sessions
                   SET requires_completion_evidence=?, completion_intent_key=?, updated_at=?
                   WHERE id=?""",
                (1 if required else 0, intent_key, _now(), session_id),
            )
            self._conn.commit()

    def record_completion_evidence(self, session_id: str, intent_key: str, kind: str) -> None:
        """Record that evidence of *kind* was observed for *intent_key*.

        Stored on the session row, so evidence does not age out of a bounded
        event window.
        """
        column = {
            "change": "completion_evidence_key",
            "verify": "verification_evidence_key",
        }.get(kind)
        if column is None:
            raise ValueError(f"unknown evidence kind: {kind!r}")
        with self._lock:
            self._conn.execute(
                f"""UPDATE nexus_task_sessions
                   SET {column}=?, updated_at=?
                   WHERE id=?""",
                (intent_key, _now(), session_id),
            )
            self._conn.commit()

    def set_status(self, session_id: str, status: str, *, worker_id: str | None = None) -> bool:
        """Move a session's status, optionally only while ``worker_id`` owns it.

        With the owner given, a superseded worker cannot change a session
        another worker holds; recovery paths omit it.
        """
        clause = " AND worker_id=?" if worker_id is not None else ""
        params: list[Any] = [status[:64], _now(), session_id]
        if worker_id is not None:
            params.append(str(worker_id)[:128])
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE nexus_task_sessions SET status=?, updated_at=? WHERE id=?{clause}",
                params,
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def get(self, session_id: str) -> NexusTaskSession | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nexus_task_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return self._row(row) if row is not None else None

    @staticmethod
    def _row(row: sqlite3.Row) -> NexusTaskSession:
        keys = set(row.keys())
        return NexusTaskSession(
            id=row["id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            title=row["title"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            worker_id=row["worker_id"] if "worker_id" in keys else None,
            claimed_at=row["claimed_at"] if "claimed_at" in keys else None,
            last_heartbeat_at=row["last_heartbeat_at"] if "last_heartbeat_at" in keys else None,
            terminal_reason=row["terminal_reason"] if "terminal_reason" in keys else None,
            requires_completion_evidence=(
                bool(row["requires_completion_evidence"])
                if "requires_completion_evidence" in keys
                else False
            ),
            completion_intent_key=(
                row["completion_intent_key"] if "completion_intent_key" in keys else None
            ),
            completion_evidence_key=(
                row["completion_evidence_key"] if "completion_evidence_key" in keys else None
            ),
            waiting_experiment_id=(
                row["waiting_experiment_id"] if "waiting_experiment_id" in keys else None
            ),
            verification_evidence_key=(
                row["verification_evidence_key"]
                if "verification_evidence_key" in keys
                else None
            ),
        )
