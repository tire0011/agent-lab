"""Which external coding agent runs the next task.

Providers (Codex, Claude Code) are selected in a durable SQLite-backed
round-robin that survives restarts, so consecutive units of work use different
agents. Before rotating, the selection is narrowed: ``AGENT_CODING_PROVIDER``
pins one (unknown values ignored), ``AGENT_CODING_PROVIDERS`` is the allow list,
and providers whose CLI is not installed are dropped. If nothing is installed,
the first allowed provider is still returned - the run then fails with the
ordinary "binary not found" error, an honest failure over a silent no-op.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Any, Callable

from modules.claude_worker import ClaudeWorker
from modules.claude_worker import PROVIDER as PROVIDER_CLAUDE
from modules.claude_worker import _default_claude_bin
from modules.codex_worker import _DEFAULT_CODEX_BIN, CodexWorker
from modules.codex_worker import PROVIDER as PROVIDER_CODEX
from modules.local_coding_worker import (
    LocalCodingWorker, PROVIDER as PROVIDER_LOCAL, task_split_enabled, test_first_enabled,
    local_samples, sample_temperatures,
)
from modules.run_store import DEFAULT_DB_PATH, init_db

logger = logging.getLogger("modules.coding_provider")

KNOWN_PROVIDERS: tuple[str, ...] = (PROVIDER_CODEX, PROVIDER_CLAUDE)
EXTERNAL_PROVIDERS: tuple[str, ...] = KNOWN_PROVIDERS

ENV_PROVIDER_PIN = "AGENT_CODING_PROVIDER"
ENV_PROVIDER_ALLOW_LIST = "AGENT_CODING_PROVIDERS"
_ROUND_ROBIN_SETTING = "coding_provider.last_selected"


class UnknownProviderError(ValueError):
    """Raised when a worker is requested for a provider that does not exist."""


def _normalize(value: str) -> str:
    return value.strip().lower()


def provider_binary(provider: str) -> str:
    """Path the given provider's CLI is expected at."""
    provider = _normalize(provider)
    if provider == PROVIDER_CODEX:
        return _DEFAULT_CODEX_BIN
    if provider == PROVIDER_CLAUDE:
        return _default_claude_bin()
    raise UnknownProviderError(
        f"unknown coding provider {provider!r}; known: {KNOWN_PROVIDERS}"
    )


def is_available(provider: str) -> bool:
    """Whether this provider's CLI is installed and executable here."""
    try:
        binary = provider_binary(provider)
    except UnknownProviderError:
        return False
    return shutil.which(binary) is not None


def allowed_providers() -> list[str]:
    """Providers permitted by configuration, in a stable order; an empty or
    entirely unknown allow list falls back to all known providers, so a typo
    cannot make the system unable to run any task."""
    raw = os.environ.get(ENV_PROVIDER_ALLOW_LIST, "")
    if not raw.strip():
        return list(EXTERNAL_PROVIDERS)

    allowed: list[str] = []
    for entry in raw.split(","):
        provider = _normalize(entry)
        if not provider:
            continue
        if provider not in EXTERNAL_PROVIDERS:
            logger.warning(
                "ignoring unknown provider %r in %s", provider, ENV_PROVIDER_ALLOW_LIST
            )
            continue
        if provider not in allowed:
            allowed.append(provider)

    if not allowed:
        logger.warning(
            "%s contained no known provider; falling back to %s",
            ENV_PROVIDER_ALLOW_LIST, ", ".join(EXTERNAL_PROVIDERS),
        )
        return list(EXTERNAL_PROVIDERS)
    return allowed


def available_providers() -> list[str]:
    """Allowed providers whose CLI is actually installed here."""
    return [provider for provider in allowed_providers() if is_available(provider)]


def pinned_provider() -> str | None:
    """The provider forced by ``AGENT_CODING_PROVIDER``, if any and valid."""
    raw = os.environ.get(ENV_PROVIDER_PIN, "")
    if not raw.strip():
        return None
    provider = _normalize(raw)
    if provider not in EXTERNAL_PROVIDERS:
        logger.warning(
            "ignoring unknown provider %r in %s; using provider rotation instead",
            provider, ENV_PROVIDER_PIN,
        )
        return None
    return provider


def choose_provider(
    rng: Any | None = None, *, db_path: str = DEFAULT_DB_PATH
) -> str:
    """Select the next provider in a persistent, concurrency-safe rotation. ``rng``
    is an unused compatibility argument; pins bypass the rotation; the allow list
    and installed-CLI filter apply first."""
    pinned = pinned_provider()
    if pinned is not None:
        return pinned

    candidates = available_providers()
    if not candidates:
        fallback = allowed_providers()[0]
        logger.warning(
            "no coding-agent CLI found for any allowed provider; using %r anyway",
            fallback,
        )
        candidates = [fallback]

    # BEGIN IMMEDIATE makes read-next-write atomic across the processes sharing
    # the database, so two simultaneous tasks cannot pick the same provider.
    conn = init_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM service_settings WHERE key = ?",
            (_ROUND_ROBIN_SETTING,),
        ).fetchone()
        last = row[0] if row is not None else None
        if last in candidates:
            selected = candidates[(candidates.index(last) + 1) % len(candidates)]
        else:
            # Stable order: deterministic first selection, and a previously
            # selected but now-disallowed provider falls back safely.
            selected = candidates[0]
        conn.execute(
            """
            INSERT INTO service_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE
            SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (_ROUND_ROBIN_SETTING, selected, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return selected
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# A provider's usage limit (e.g. Claude's weekly limit) counts as overload: the
# other agent takes over and the attempt does not count.
_OVERLOAD_RE = re.compile(
    r"\b(?:529|429)\b|overloaded|rate[ _-]?limit|usage[ _-]?limit|limit (?:reached|exceeded)",
    re.IGNORECASE,
)


def is_provider_overload(error: Any) -> bool:
    """The provider was busy, not the work wrong ("API Error: 529 Overloaded")."""
    return bool(_OVERLOAD_RE.search(str(error or "")))


def other_provider(provider: str | None) -> str | None:
    """Another allowed provider with an installed CLI, for when *provider* is overloaded."""
    for candidate in allowed_providers():
        if candidate != provider and is_available(candidate):
            return candidate
    return None


def provider_of(worker: Any, default: str | None = None) -> str | None:
    """The provider name *worker* reports, or *default*; anything not a known
    provider name (test doubles, workers predating the attribute) yields
    *default* rather than a value that would be stored or bound to SQL."""
    provider = getattr(worker, "provider", None)
    if isinstance(provider, str) and provider in (*KNOWN_PROVIDERS, PROVIDER_LOCAL):
        return provider
    return default


def build_worker(provider: str, *, db_path: str | None = None, **kwargs: Any) -> Any:
    """Construct the worker for *provider*; extra kwargs pass through to the
    worker class (e.g. max_output_bytes, the Claude model)."""
    provider = _normalize(provider)
    if provider == PROVIDER_CODEX:
        return CodexWorker(db_path=db_path, **kwargs)
    if provider == PROVIDER_CLAUDE:
        return ClaudeWorker(db_path=db_path, **kwargs)
    if provider == PROVIDER_LOCAL:
        # #598: split into small subtasks first unless LOCAL_TASK_SPLIT turns it off.
        kwargs.setdefault("split_tasks", task_split_enabled())
        kwargs.setdefault("test_first", test_first_enabled())
        # #639: best-of-N patch sampling, NEXUS_LOCAL_SAMPLES (default 1).
        kwargs.setdefault("samples", local_samples())
        kwargs.setdefault("sample_temperatures", sample_temperatures())
        return LocalCodingWorker(**kwargs)
    raise UnknownProviderError(
        f"unknown coding provider {provider!r}; known: {KNOWN_PROVIDERS}"
    )


def build_random_worker(
    *, db_path: str | None = None, rng: Any | None = None, **kwargs: Any
) -> tuple[str, Any]:
    """Select a provider and return ``(provider, worker)``."""
    provider = choose_provider(rng, db_path=db_path or DEFAULT_DB_PATH)
    return provider, build_worker(provider, db_path=db_path, **kwargs)


def random_worker_factory(
    *,
    db_path: str | None = None,
    rng: Any | None = None,
    sticky: bool = True,
    **kwargs: Any,
) -> Callable[..., Any]:
    """A ``(working_dir) -> worker`` factory matching ``ExperimentService``. The
    working directory does not confine the worker (sandbox + cwd do) but
    identifies the piece of work: with *sticky* (default) one selection per
    working directory is reused, so an experiment and its repair attempts share
    one agent; ``sticky=False`` selects independently per call."""
    drawn: dict[str, str] = {}

    def factory(working_dir: Any = None, *_args: Any, **_kwargs: Any) -> Any:
        if not sticky:
            _provider, worker = build_random_worker(db_path=db_path, rng=rng, **kwargs)
            return worker
        key = str(working_dir)
        provider = drawn.get(key)
        if provider is None:
            provider = choose_provider(rng, db_path=db_path or DEFAULT_DB_PATH)
            drawn[key] = provider
        return build_worker(provider, db_path=db_path, **kwargs)

    return factory
