"""Built-in default rows for the ``model_prices`` table (#810).

The built-in rows provide public list prices for known models. Operator price
rows take precedence for the same key. New prices can be added with a later
``valid_from``. Prices are resolved by exact model or a supported snapshot alias.
"""

from __future__ import annotations

import sqlite3
from typing import Any

#: Historical wildcard key. Exact model pricing never uses this key.
WILDCARD_MODEL = "*"

#: (provider, model, input USD per 1M tokens, output USD per 1M tokens,
#:  currency, valid_from, source). List prices as published by the providers;
#: the ``source`` column says where each number comes from.
DEFAULT_MODEL_PRICES: tuple[tuple[str, str, float, float, str, str, str], ...] = (
    # Azure OpenAI deployment in use (AGENTS.md): gpt-4o 2024-05-13, GlobalStandard.
    ("azure_openai", "gpt-4o", 2.50, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Azure OpenAI gpt-4o (2024-05-13) GlobalStandard list price"),
    # Older Codex models still appear in historical runs.
    ("codex", "gpt-5", 1.25, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: OpenAI gpt-5 list price"),
    ("codex", "gpt-5-codex", 1.25, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: OpenAI gpt-5-codex list price"),
    ("codex", "gpt-5.1", 1.25, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: OpenAI gpt-5.1 list price"),
    ("codex", "gpt-5.1-codex", 1.25, 10.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: OpenAI gpt-5.1-codex list price"),
    # Claude model IDs reported in modelUsage.
    ("claude", "claude-sonnet-4-5", 3.00, 15.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Anthropic claude-sonnet-4-5 list price"),
    ("claude", "claude-sonnet-5", 2.00, 10.00, "USD", "2026-06-30T00:00:00Z",
     "builtin: Anthropic claude-sonnet-5 list price"),
    ("claude", "claude-sonnet-4-6", 3.00, 15.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Anthropic claude-sonnet-4-6 list price"),
    ("claude", "claude-opus-4-1", 15.00, 75.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Anthropic claude-opus-4-1 list price"),
    ("claude", "claude-opus-4-5", 5.00, 25.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Anthropic claude-opus-4-5 list price"),
    ("claude", "claude-haiku-4-5", 1.00, 5.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: Anthropic claude-haiku-4-5 list price"),
    ("codex", "gpt-5.2-codex", 1.75, 14.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: OpenAI gpt-5.2-codex list price"),
    ("codex", "gpt-5.3-codex", 1.75, 14.00, "USD", "2026-02-05T00:00:00Z",
     "builtin: OpenAI gpt-5.3-codex list price"),
    ("codex", "gpt-5.4", 2.50, 15.00, "USD", "2026-03-05T00:00:00Z",
     "builtin: OpenAI gpt-5.4 standard short-context list price"),
    ("codex", "gpt-5.5", 5.00, 30.00, "USD", "2026-04-23T00:00:00Z",
     "builtin: OpenAI gpt-5.5 standard short-context list price"),
    ("claude", "claude-opus-4-6", 5.00, 25.00, "USD", "2026-02-05T00:00:00Z",
     "builtin: Anthropic claude-opus-4-6 list price"),
    # Mistral API worker models are on the La Plateforme free tier
    # (config/models.json marks them cost "free"), so their money cost is 0.
    ("mistral", "codestral-latest", 0.00, 0.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: La Plateforme free tier (config/models.json cost=free)"),
    ("mistral", "mistral-small-latest", 0.00, 0.00, "USD", "2026-01-01T00:00:00Z",
     "builtin: La Plateforme free tier (config/models.json cost=free)"),
)


def seed_default_model_prices(conn: sqlite3.Connection) -> int:
    """Seed list prices and upgrade matching legacy built-in rows."""
    conn.execute(
        """UPDATE model_prices SET input_price_per_million_tokens=5,
                  output_price_per_million_tokens=25, cache_read_rate=.5, cache_write_rate=6.25
           WHERE provider='claude' AND model='claude-opus-4-5' AND valid_from='2026-01-01T00:00:00Z'
             AND source='builtin: Anthropic claude-opus-4-5 list price'
             AND currency='USD' AND input_price_per_million_tokens=15 AND output_price_per_million_tokens=75"""
    )
    inserted = 0
    for provider, model, input_price, output_price, currency, valid_from, source in DEFAULT_MODEL_PRICES:
        cursor = conn.execute(
            """
            INSERT INTO model_prices (
                provider, model,
                input_price_per_million_tokens, output_price_per_million_tokens,
                currency, valid_from, source, cache_read_rate, cache_write_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, model, valid_from) DO UPDATE SET
                cache_read_rate = excluded.cache_read_rate,
                cache_write_rate = excluded.cache_write_rate
            WHERE model_prices.source = excluded.source
              AND model_prices.cache_read_rate IS NULL AND excluded.cache_read_rate IS NOT NULL
            """,
            (provider, model, input_price, output_price, currency, valid_from, source,
             input_price / 10 if provider in {"codex", "claude"} else None,
             input_price * 1.25 if provider == "claude" else None),
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
