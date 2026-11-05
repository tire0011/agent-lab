"""Local-model Decision Router.

Uses a local OpenAI-compatible model server (llama.cpp / llama-server or any
compatible endpoint) to parse an incoming task and decide which execution
strategy to use.

Routing separates **role** from **provider**:

* **Roles** describe *what* step runs next:
  ``router``, ``context_gatherer``, ``planner``, ``developer``, ``reviewer``.
* **Providers** describe *who* executes a role:
  ``local``, ``azure_openai``, ``codex``, ``copilot``.

The legacy ``decision`` field (``SELF / AZURE_OPENAI / CODEX / CONSULT``) is
retained for backward compatibility and is derived automatically from
``provider`` when not explicitly set by the model.

Router decisions are persisted to the ``router_decisions`` SQLite table so
costs and routing patterns can be analysed over time.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json
from modules.prompt_library import render_prompt


# ── Valid values ──────────────────────────────────────────────────────────────

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
    """Parsed output of a single router call.

    The ``provider`` and ``role`` fields carry the new role/provider
    vocabulary.  The legacy ``decision`` field is retained for backward
    compatibility and is always set (derived from ``provider`` if not
    provided explicitly by the model).
    """

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
    """Extract a :class:`RouterDecision` from the model's raw text output.

    Accepts both the new ``provider``/``role`` format and the legacy
    ``decision`` format, so old prompt templates continue to work.
    """
    data: dict[str, Any] = extract_json(raw)

    # ── New format: provider + role ───────────────────────────────────────
    provider_raw = str(data.get("provider", "")).lower()
    role_raw = str(data.get("role", "")).lower()

    # ── Legacy format: decision ───────────────────────────────────────────
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
    """Routes tasks to the appropriate execution provider using a local model.

    The local model server endpoint is configured via the ``LOCAL_MODEL_URL``
    environment variable (default: ``http://127.0.0.1:8080/v1``).  The model
    itself is configured via ``LOCAL_MODEL_NAME``
    (default: ``qwen2.5-coder-7b-instruct``).
    This makes the router server/model-agnostic: swap GGUF files or switch to
    a different llama.cpp build without changing orchestration code.

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
        """Ask the local model to decide how to handle *task*.

        Returns
        -------
        RouterDecision
            The routing decision with metadata, including the new ``provider``
            and ``role`` fields and the legacy ``decision`` token.

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
    """Create the router_decisions table if it does not exist.

    The ``provider`` and ``role`` columns are added additively if the table
    already exists from an earlier schema version.
    """
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
