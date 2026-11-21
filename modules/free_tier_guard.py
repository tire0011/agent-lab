"""Free-tier coders that fail on output format (#763).

A free coder (Mistral) that returns no usable SEARCH/REPLACE edit hands the
same run to the next coding provider instead of spending one of the issue's
attempts. Each run that starts on a free-tier coder is recorded on its
experiment row (``free_tier_provider``, ``free_tier_format_failures`` 0/1), so
the share of format failures is visible per day.

The guard: when more than ``NEXUS_FREE_TIER_GUARD_MAX_SHARE`` (default 0.5) of
the last ``NEXUS_FREE_TIER_GUARD_WINDOW`` (default 10) free-tier runs failed on
format, work with ``complexity != small`` is no longer sent to the free tier.
``NEXUS_FREE_TIER_GUARD=0`` turns the guard off. The status issue shows the
setting and the current share.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from modules.mistral_worker import PROVIDER as PROVIDER_MISTRAL

# Coding providers that run on a free tier; everything else external is paid.
FREE_TIER_CODERS: frozenset[str] = frozenset({PROVIDER_MISTRAL})

FORMAT_FAILURE_PREFIX = "patch rejected ("
FORMAT_FALLBACK_NOTE = "A free coder failed on output format; its answer is not used."

ENV_GUARD = "NEXUS_FREE_TIER_GUARD"
ENV_WINDOW = "NEXUS_FREE_TIER_GUARD_WINDOW"
ENV_MAX_SHARE = "NEXUS_FREE_TIER_GUARD_MAX_SHARE"
DEFAULT_WINDOW = 10
DEFAULT_MAX_SHARE = 0.5


def is_free_tier(provider: str | None) -> bool:
    return bool(provider) and provider in FREE_TIER_CODERS


def is_paid(provider: str | None) -> bool:
    """An external coder that is not on a free tier (the local model is free)."""
    return bool(provider) and provider != "local" and not is_free_tier(provider)


def is_format_failure(error: Any) -> bool:
    """The free coder answered, but no edit could be applied from it."""
    return isinstance(error, str) and error.startswith(FORMAT_FAILURE_PREFIX)


@dataclass(frozen=True)
class GuardSetting:
    enabled: bool
    window: int
    max_share: float


def guard_setting() -> GuardSetting:
    raw = os.environ.get(ENV_GUARD, "").strip().lower()
    enabled = raw not in {"0", "false", "no", "off"}
    try:
        window = max(1, int(os.environ.get(ENV_WINDOW, "") or DEFAULT_WINDOW))
    except ValueError:
        window = DEFAULT_WINDOW
    try:
        max_share = float(os.environ.get(ENV_MAX_SHARE, "") or DEFAULT_MAX_SHARE)
    except ValueError:
        max_share = DEFAULT_MAX_SHARE
    return GuardSetting(enabled=enabled, window=window, max_share=max_share)


def _recent_outcomes(conn: sqlite3.Connection, window: int) -> list[int]:
    rows = conn.execute(
        "SELECT COALESCE(free_tier_format_failures, 0) FROM experiments "
        "WHERE free_tier_provider IS NOT NULL "
        "ORDER BY COALESCE(implementation_started_at, created_at) DESC LIMIT ?",
        (window,),
    ).fetchall()
    return [1 if row[0] else 0 for row in rows]


def daily_stats(conn: sqlite3.Connection, *, days: int = 7) -> list[dict[str, Any]]:
    """Per day: free-tier runs, format failures and their share (newest first)."""
    rows = conn.execute(
        "SELECT substr(COALESCE(implementation_started_at, created_at), 1, 10) AS day, "
        "COUNT(*), SUM(COALESCE(free_tier_format_failures, 0)) FROM experiments "
        "WHERE free_tier_provider IS NOT NULL GROUP BY day ORDER BY day DESC LIMIT ?",
        (days,),
    ).fetchall()
    return [
        {"day": day, "runs": int(runs), "free_tier_format_failures": int(failures or 0),
         "share": (int(failures or 0) / int(runs)) if runs else 0.0}
        for day, runs, failures in rows
    ]


@dataclass(frozen=True)
class GuardState:
    setting: GuardSetting
    runs: int
    failures: int

    @property
    def share(self) -> float:
        return self.failures / self.runs if self.runs else 0.0

    @property
    def tripped(self) -> bool:
        """Enough runs seen and their failure share is above the limit."""
        return (
            self.setting.enabled
            and self.runs >= self.setting.window
            and self.share > self.setting.max_share
        )


def guard_state(conn: sqlite3.Connection, setting: GuardSetting | None = None) -> GuardState:
    setting = setting or guard_setting()
    try:
        outcomes = _recent_outcomes(conn, setting.window)
    except sqlite3.Error:
        outcomes = []
    return GuardState(setting=setting, runs=len(outcomes), failures=sum(outcomes))


def blocks_free_tier(conn: sqlite3.Connection, complexity: str | None) -> bool:
    """Whether work of *complexity* must not start on a free-tier coder now."""
    if complexity == "small":
        return False
    return guard_state(conn).tripped


def status_line(conn: sqlite3.Connection) -> str:
    """One line for the status output: the setting and the current share."""
    state = guard_state(conn)
    setting = state.setting
    if not setting.enabled:
        mode = "off"
    elif state.tripped:
        mode = "active: complexity != small skips the free tier"
    else:
        mode = "on"
    return (
        f"**Free-tier guard:** {mode} - {state.failures} of the last {state.runs} free-tier runs "
        f"failed on output format (limit: more than {setting.max_share:.0%} over {setting.window} runs)"
    )
