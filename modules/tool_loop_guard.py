"""Persistent duplicate and non-progressing tool-call detection."""

from __future__ import annotations

import hashlib
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


def canonical_json(value: Any) -> str:
    """Return a stable JSON representation for nested tool arguments."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def call_signature(tool: str, arguments: dict[str, Any]) -> str:
    payload = canonical_json({"tool": tool.strip().lower(), "arguments": arguments})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ToolCallCheck:
    allowed: bool
    call_id: str
    signature: str
    observation: dict[str, Any] | None = None
    stop: bool = False


class PersistentToolLoopGuard:
    """Reserve tool calls before execution and reject unchanged repetitions."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        *,
        max_total_steps: int = 20,
        max_identical_calls: int = 3,
        max_non_progressing_cycles: int = 2,
    ) -> None:
        self.max_total_steps = max(1, max_total_steps)
        self.max_identical_calls = max(2, max_identical_calls)
        self.max_non_progressing_cycles = max(1, max_non_progressing_cycles)
        self._conn = init_db(db_path)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS nexus_tool_calls (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    state_token TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_ref TEXT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_nexus_tool_calls_lookup
                    ON nexus_tool_calls(session_id, signature, state_token, created_at);
                CREATE INDEX IF NOT EXISTS idx_nexus_tool_calls_recent
                    ON nexus_tool_calls(session_id, created_at);

                CREATE TABLE IF NOT EXISTS nexus_orchestration_budgets (
                    session_id TEXT NOT NULL,
                    state_token TEXT NOT NULL,
                    steps INTEGER NOT NULL DEFAULT 0,
                    duplicate_calls INTEGER NOT NULL DEFAULT 0,
                    cycles INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(session_id, state_token)
                );
                """
            )

    def close(self) -> None:
        self._conn.close()

    def claim_step(self, session_id: str, state_token: str) -> tuple[bool, int]:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO nexus_orchestration_budgets(session_id, state_token, steps)
                   VALUES (?, ?, 1)
                   ON CONFLICT(session_id, state_token)
                   DO UPDATE SET steps=steps+1""",
                (session_id, state_token),
            )
            steps = int(
                self._conn.execute(
                    "SELECT steps FROM nexus_orchestration_budgets WHERE session_id=? AND state_token=?",
                    (session_id, state_token),
                ).fetchone()["steps"]
            )
        return steps <= self.max_total_steps, steps

    def check(
        self,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        state_token: str,
        reason: str = "",
    ) -> ToolCallCheck:
        signature = call_signature(tool, arguments)
        call_id = uuid.uuid4().hex
        reason = reason.strip()[:1000]
        arguments_json = canonical_json(arguments)

        with self._lock, self._conn:
            previous_any_state = self._conn.execute(
                """SELECT id, state_token, reason, result_ref FROM nexus_tool_calls
                   WHERE session_id=? AND signature=? AND status IN ('pending', 'executed')
                   ORDER BY rowid DESC LIMIT 1""",
                (session_id, signature),
            ).fetchone()
            previous = self._conn.execute(
                """SELECT id, result_ref FROM nexus_tool_calls
                   WHERE session_id=? AND signature=? AND state_token=?
                     AND status IN ('pending', 'executed')
                   ORDER BY rowid ASC LIMIT 1""",
                (session_id, signature, state_token),
            ).fetchone()

            if previous is None and previous_any_state is not None and not reason:
                previous = previous_any_state
                event = "retry_reason_required"
            elif previous is not None:
                recent = self._conn.execute(
                    """SELECT signature FROM nexus_tool_calls
                       WHERE session_id=? AND state_token=? AND status IN ('pending', 'executed')
                       ORDER BY rowid DESC LIMIT 2""",
                    (session_id, state_token),
                ).fetchall()
                is_cycle = bool(recent and recent[0]["signature"] != signature)
                event = "non_progressing_cycle" if is_cycle else "duplicate_tool_call"
            else:
                self._conn.execute(
                    """INSERT INTO nexus_tool_calls
                       (id, session_id, signature, tool, arguments_json, state_token,
                        reason, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (call_id, session_id, signature, tool, arguments_json, state_token, reason, _now()),
                )
                return ToolCallCheck(True, call_id, signature)

            field = "cycles" if event == "non_progressing_cycle" else "duplicate_calls"
            self._conn.execute(
                f"""INSERT INTO nexus_orchestration_budgets(session_id, state_token, {field})
                    VALUES (?, ?, 1)
                    ON CONFLICT(session_id, state_token)
                    DO UPDATE SET {field}={field}+1""",
                (session_id, state_token),
            )
            budget = self._conn.execute(
                """SELECT duplicate_calls, cycles FROM nexus_orchestration_budgets
                   WHERE session_id=? AND state_token=?""",
                (session_id, state_token),
            ).fetchone()
            attempts = int(
                self._conn.execute(
                    """SELECT COUNT(*) AS count FROM nexus_tool_calls
                       WHERE session_id=? AND signature=? AND state_token=?""",
                    (session_id, signature, state_token),
                ).fetchone()["count"]
            ) + 1
            stop = (
                attempts >= self.max_identical_calls
                or int(budget["cycles"]) >= self.max_non_progressing_cycles
            )
            previous_id = str(previous["id"])
            result_ref = str(previous["result_ref"] or previous_id)
            observation = {
                "tool": tool,
                "event": event,
                "signature": signature,
                "previous_call_id": previous_id,
                "result_ref": result_ref,
                "recovery": "choose_different_tool_or_controlled_final_action",
            }
            self._conn.execute(
                """INSERT INTO nexus_tool_calls
                   (id, session_id, signature, tool, arguments_json, state_token,
                    reason, status, result_ref, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    call_id, session_id, signature, tool, arguments_json, state_token,
                    reason, event, result_ref, _now(),
                ),
            )
            return ToolCallCheck(False, call_id, signature, observation, stop)

    def record_result(self, call_id: str, observation: dict[str, Any]) -> str:
        digest = hashlib.sha256(canonical_json(observation).encode("utf-8")).hexdigest()[:20]
        result_ref = f"result:{digest}"
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE nexus_tool_calls SET status='executed', result_ref=? WHERE id=?",
                (result_ref, call_id),
            )
        return result_ref
