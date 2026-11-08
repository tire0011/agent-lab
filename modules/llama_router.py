"""Local-model decision router: parses a task and decides which provider runs it.
Roles (what step runs next: router, context_gatherer, planner, developer, reviewer)
are separate from providers (who executes: local, azure_openai, codex, copilot).
The legacy ``decision`` token (SELF/AZURE_OPENAI/CODEX/CONSULT) is kept for backward
compatibility and derived from ``provider`` when unset. Decisions are persisted to
the ``router_decisions`` SQLite table for cost/routing analysis.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json
from modules.prompt_library import render_prompt


# Valid values

# Legacy decision tokens kept for backward compatibility.
VALID_DECISIONS = frozenset({"SELF", "AZURE_OPENAI", "CODEX", "CONSULT"})

# New role/provider vocabulary.
VALID_PROVIDERS = frozenset({"local", "azure_openai", "codex", "copilot"})
VALID_ROLES = frozenset({"router", "context_gatherer", "planner", "developer", "reviewer"})

# Mapping from legacy decision tokens to provider strings.
_DECISION_TO_PROVIDER: dict[str, str] = {
    "SELF": "local",
    "AZURE_OPENAI": "azure_openai",
    "CODEX": "codex",
    "CONSULT": "local",  # context_gatherer is always local
}

# Mapping from provider strings to legacy decision tokens.
_PROVIDER_TO_DECISION: dict[str, str] = {
    "local": "SELF",
    "azure_openai": "AZURE_OPENAI",
    "codex": "CODEX",
    "copilot": "AZURE_OPENAI",
}


@dataclass
class RouterDecision:
    """Parsed output of one router call: ``provider``/``role`` plus the legacy
    ``decision`` token, always set (derived from ``provider`` when unset)."""

    # New fields (preferred)
    provider: str = "local"   # local | azure_openai | codex | copilot
    role: str = "developer"   # context_gatherer | planner | developer | reviewer

    # Legacy / derived fields (kept for backward compat)
    decision: str = ""        # SELF | AZURE_OPENAI | CODEX | CONSULT (derived)
    confidence: float = 0.0
    reasoning: str = ""
    consult_query: str = ""
    sub_tasks: list[str] = field(default_factory=list)
    raw_response: str = ""

    def __post_init__(self) -> None:
        # Derive ``decision`` from ``provider`` when not explicitly set.
        if not self.decision:
            self.decision = _PROVIDER_TO_DECISION.get(self.provider, "SELF")


def _parse_router_response(raw: str) -> RouterDecision:
    """Extract a :class:`RouterDecision` from raw model text, accepting both the
    ``provider``/``role`` format and the legacy ``decision`` format."""
    data: dict[str, Any] = extract_json(raw)

    # New format: provider + role
    provider_raw = str(data.get("provider", "")).lower()
    role_raw = str(data.get("role", "")).lower()

    # Legacy format: decision
    decision_raw = str(data.get("decision", "")).upper()

    if provider_raw and provider_raw in VALID_PROVIDERS:
        provider = provider_raw
    elif decision_raw in VALID_DECISIONS:
        provider = _DECISION_TO_PROVIDER[decision_raw]
    else:
        # Accept unknown decision token if provider is known.
        if decision_raw and decision_raw not in VALID_DECISIONS:
            raise LlamaCppClientError(
                f"Router returned unknown decision {decision_raw!r}. Raw: {raw[:300]!r}"
            )
        provider = "local"

    if role_raw and role_raw in VALID_ROLES:
        role = role_raw
    else:
        # Default role based on provider: if delegating externally, use developer.
        role = "developer"

    # Derive legacy decision from provider for backward compat.
    decision = decision_raw if decision_raw in VALID_DECISIONS else _PROVIDER_TO_DECISION.get(provider, "SELF")

    return RouterDecision(
        provider=provider,
        role=role,
        decision=decision,
        confidence=float(data.get("confidence", 0.0)),
        reasoning=str(data.get("reasoning", "")),
        consult_query=str(data.get("consult_query", "")),
        sub_tasks=list(data.get("sub_tasks") or []),
        raw_response=raw,
    )


class LlamaRouter:
    """Routes tasks to a provider using a local model (``LOCAL_MODEL_URL`` /
    ``LOCAL_MODEL_NAME`` env vars, so GGUF/server swaps need no code change).
    client defaults to one built from env; db persists decisions (table created if
    missing); prompts_dir overrides the prompt directory (useful in tests)."""

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
        """Ask the local model to decide how to handle *task*; returns the
        RouterDecision (provider, role, legacy decision token). Raises
        :class:`LlamaCppClientError` when the model is unavailable or unparseable."""
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


# SQLite helpers

def _ensure_router_tables(conn: sqlite3.Connection) -> None:
    """Create ``router_decisions`` if missing; provider/role columns are added
    additively for older schemas."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS router_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            task TEXT NOT NULL,
            decision TEXT NOT NULL,
            provider TEXT NULL,
            role TEXT NULL,
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
        CREATE INDEX IF NOT EXISTS idx_router_decisions_provider
            ON router_decisions(provider);
        """
    )
    # Additive migration: add provider/role if missing (older schema).
    existing = {row[1] for row in conn.execute("PRAGMA table_info(router_decisions)")}
    for col, coltype in [("provider", "TEXT"), ("role", "TEXT")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE router_decisions ADD COLUMN {col} {coltype} NULL")
    conn.commit()


def _persist_decision(
    conn: sqlite3.Connection, task: str, decision: RouterDecision
) -> None:
    """Insert a router decision row into the database."""
    conn.execute(
        """
        INSERT INTO router_decisions
            (timestamp, task, decision, provider, role, confidence, reasoning,
             consult_query, sub_tasks_json, raw_response)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            task,
            decision.decision,
            decision.provider,
            decision.role,
            decision.confidence,
            decision.reasoning,
            decision.consult_query or None,
            json.dumps(decision.sub_tasks) if decision.sub_tasks else None,
            decision.raw_response or None,
        ),
    )
    conn.commit()
