"""Built-in default rows for the ``model_prices`` table (#810).

Nothing else in the repository writes ``model_prices``, so without these rows
every price lookup missed and every run was stored with a NULL cost. The rows
below are the public list prices of the models the coding agents actually use,
seeded once with ``ON CONFLICT DO NOTHING``: an operator-inserted row for the
same ``(provider, model, valid_from)`` always wins, and price changes over time
are added as new rows with a later ``valid_from`` (see
:func:`modules.run_store.records.get_applicable_price`).

``model = '*'`` is the provider-wide fallback used when no row matches the
exact model name (:func:`modules.run_store.records.get_effective_price`).
"""

from __future__ import annotations

import sqlite3
from typing import Any

#: Wildcard model name: the fallback price row of a provider.
WILDCARD_MODEL = "*"

#: (provider, model, input USD per 1M tokens, output USD per 1M tokens,
#:  currency, valid_from, source). List prices as published by the providers;
#: the ``source`` column says where each number comes from.
DEFAULT_MODEL_PRICES: tuple[tuple[str, str, float, float, str, str, str], ...] = (
    # Azure OpenAI deployment in use (AGENTS.md): gpt-4o 2024-05-13, GlobalStandard.
    ("azure_openai", "gpt-4o", 2.50, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Azure OpenAI gpt-4o (2024-05-13) GlobalStandard list price"),
    # Codex CLI default model family (gpt-5 / gpt-5-codex) at OpenAI list price.
    ("codex", WILDCARD_MODEL, 1.25, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: OpenAI gpt-5 family list price (Codex CLI default)"),
    ("codex", "gpt-5", 1.25, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: OpenAI gpt-5 list price"),
    ("codex", "gpt-5-codex", 1.25, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: OpenAI gpt-5-codex list price"),
    ("codex", "gpt-5.1", 1.25, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: OpenAI gpt-5.1 list price"),
    ("codex", "gpt-5.1-codex", 1.25, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: OpenAI gpt-5.1-codex list price"),
    # Claude CLI default (Sonnet) and the model families it reports in modelUsage.
    ("claude", WILDCARD_MODEL, 3.00, 15.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Claude Sonnet 4.x list price (Claude CLI default)"),
    ("claude", "claude-sonnet-4-5", 3.00, 15.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Anthropic claude-sonnet-4-5 list price"),
    ("claude", "claude-sonnet-4-6", 3.00, 15.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Anthropic claude-sonnet-4-6 list price"),
    ("claude", "claude-opus-4-1", 15.00, 75.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Anthropic claude-opus-4-1 list price"),
    ("claude", "claude-opus-4-5", 15.00, 75.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Anthropic claude-opus-4-5 list price"),
    ("claude", "claude-haiku-4-5", 1.00, 5.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Anthropic claude-haiku-4-5 list price"),
    # Mistral API worker models are on the La Plateforme free tier
    # (config/models.json marks them cost "free"), so their money cost is 0.
    ("mistral", WILDCARD_MODEL, 0.00, 0.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: La Plateforme free tier (config/models.json cost=free)"),
    ("mistral", "codestral-latest", 0.00, 0.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: La Plateforme free tier (config/models.json cost=free)"),
    ("mistral", "mistral-small-latest", 0.00, 0.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: La Plateforme free tier (config/models.json cost=free)"),
)


def seed_default_model_prices(conn: sqlite3.Connection) -> int:
    """Insert the built-in price rows that are not present yet; returns how many
    were inserted. Idempotent: existing rows (operator overrides, later
    ``valid_from`` history) are never touched."""
    inserted = 0
    for provider, model, input_price, output_price, currency, valid_from, source in DEFAULT_MODEL_PRICES:
        cursor = conn.execute(
            """
            INSERT INTO model_prices (
                provider, model,
                input_price_per_million_tokens, output_price_per_million_tokens,
                currency, valid_from, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, model, valid_from) DO NOTHING
            """,
            (provider, model, input_price, output_price, currency, valid_from, source),
        )
        inserted += cursor.rowcount or 0
    conn.commit()
    return inserted


def default_price_rows() -> list[dict[str, Any]]:
    """The built-in rows as dicts, e.g. for reports; mirrors the table columns."""
    return [
        {
            "provider": provider,
            "model": model,
            "input_price_per_million_tokens": input_price,
            "output_price_per_million_tokens": output_price,
            "currency": currency,
            "valid_from": valid_from,
            "source": source,
        }
        for provider, model, input_price, output_price, currency, valid_from, source in DEFAULT_MODEL_PRICES
    ]
