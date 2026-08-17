"""Llama 2 13B Decision Router.

Uses the local llama-cpp-python server to parse an incoming task and decide
which execution strategy to use: SELF, AZURE_OPENAI, CODEX, or CONSULT.

The router decision is persisted to the ``router_decisions`` SQLite table so
costs and routing patterns can be analysed over time.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError
from modules.prompt_library import render_prompt


VALID_DECISIONS = frozenset({"SELF", "AZURE_OPENAI", "CODEX", "CONSULT"})


@dataclass
class RouterDecision:
    """Parsed output of a single router call."""

    decision: str
    confidence: float
    reasoning: str
    consult_query: str = ""
    sub_tasks: list[str] = field(default_factory=list)
    raw_response: str = ""

    def is_valid(self) -> bool:
        return self.decision in VALID_DECISIONS


def _parse_router_response(raw: str) -> RouterDecision:
    """Extract a :class:`RouterDecision` from the model's raw text output."""
    # LlamaCppClient.extract_json handles prose wrappers around the JSON block.
    client = LlamaCppClient()
    data: dict[str, Any] = client.extract_json(raw)

    decision = str(data.get("decision", "")).upper()
    if decision not in VALID_DECISIONS:
        raise LlamaCppClientError(
            f"Router returned unknown decision {decision!r}. Raw: {raw[:300]!r}"
        )

    return RouterDecision(
        decision=decision,
        confidence=float(data.get("confidence", 0.0)),
        reasoning=str(data.get("reasoning", "")),
        consult_query=str(data.get("consult_query", "")),
        sub_tasks=list(data.get("sub_tasks") or []),
        raw_response=raw,
    )


class LlamaRouter:
    """Uses Llama 2 13B to route tasks to the appropriate execution provider.

    Parameters
    ----------
    client:
        A :class:`~modules.llama_cpp_client.LlamaCppClient` instance.  If
        *None*, a default instance is constructed from environment variables.
    db:
        Open SQLite connection used to persist decisions.  The
        ``router_decisions`` table is created if it does not already exist.
    prompts_dir:
        Override the default prompts directory (useful in tests).
    """

    def __init__(
        self,
        client: LlamaCppClient | None = None,
        db: sqlite3.Connection | None = None,
        prompts_dir: Any = None,
    ) -> None:
        self._client = client or LlamaCppClient()
        self._db = db
        self._prompts_dir = prompts_dir
        if db is not None:
            _ensure_router_tables(db)

    async def route(self, task: str) -> RouterDecision:
        """Ask Llama 2 to decide how to handle *task*.

        Returns
        -------
        RouterDecision
            The routing decision with metadata.

        Raises
        ------
        LlamaCppClientError
            If the model is unavailable or returns an unparseable response.
        """
        prompt = render_prompt(
            "router",
            {"task": task},
            prompts_dir=self._prompts_dir,
        )

        raw = await self._client.complete(
            prompt=prompt,
            temperature=0.0,
            max_tokens=512,
        )

        decision = _parse_router_response(raw)

        if self._db is not None:
            _persist_decision(self._db, task, decision)

        return decision

    async def is_available(self) -> bool:
        """Return True if the local llama-cpp server is reachable."""
        return await self._client.is_available()


# ── SQLite helpers ────────────────────────────────────────────────────────────

def _ensure_router_tables(conn: sqlite3.Connection) -> None:
    """Create the router_decisions table if it does not exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS router_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            task TEXT NOT NULL,
            decision TEXT NOT NULL,
            confidence REAL NOT NULL,
            reasoning TEXT NOT NULL,
            consult_query TEXT NULL,
            sub_tasks_json TEXT NULL,
            raw_response TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_router_decisions_timestamp
            ON router_decisions(timestamp);
        CREATE INDEX IF NOT EXISTS idx_router_decisions_decision
            ON router_decisions(decision);
        """
    )
    conn.commit()


def _persist_decision(
    conn: sqlite3.Connection, task: str, decision: RouterDecision
) -> None:
    """Insert a router decision row into the database."""
    conn.execute(
        """
        INSERT INTO router_decisions
            (timestamp, task, decision, confidence, reasoning,
             consult_query, sub_tasks_json, raw_response)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            task,
            decision.decision,
            decision.confidence,
            decision.reasoning,
            decision.consult_query or None,
            json.dumps(decision.sub_tasks) if decision.sub_tasks else None,
            decision.raw_response or None,
        ),
    )
    conn.commit()
