"""Which external coding agent runs the next task.

Providers (Codex, Claude Code, Mistral) are selected in a durable SQLite-backed
round-robin that survives restarts, so consecutive units of work use different
agents. Before rotating, the selection is narrowed: ``AGENT_CODING_PROVIDER``
pins one (unknown values ignored), ``AGENT_CODING_PROVIDERS`` is the allow list,
and providers that are not available are dropped - for Codex/Claude that means
the CLI is not installed, for Mistral a coding-role model in
``config/models.json`` with its API key set. If nothing is available, the first
allowed provider is still returned - the run then fails with the ordinary
"binary not found" / "no available model" error, an honest failure over a
silent no-op.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
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
from modules.mistral_worker import PROVIDER as PROVIDER_MISTRAL, MistralWorker, coding_model
from modules.vibe_worker import VibeWorker, vibe_available
from modules.run_store import DEFAULT_DB_PATH, init_db

logger = logging.getLogger("modules.coding_provider")

KNOWN_PROVIDERS: tuple[str, ...] = (PROVIDER_CODEX, PROVIDER_CLAUDE, PROVIDER_MISTRAL)
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
    """Whether this provider can run here: its CLI is installed and executable
    (Codex, Claude). Mistral runs through the ``vibe`` CLI on the owner's Vibe
    subscription when it is installed, else through the API with a coding-role
    registry model whose key is configured."""
    if _normalize(provider) == PROVIDER_MISTRAL:
        return vibe_available() or coding_model() is not None
    try:
        binary = provider_binary(provider)
    except UnknownProviderError:
        return False
    return shutil.which(binary) is not None


def allowed_providers(*, db_path: str | None = None) -> list[str]:
    """Providers permitted by configuration, in a stable order; an empty or
    entirely unknown allow list falls back to all known providers, so a typo
    cannot make the system unable to run any task. With *db_path*, providers
    cooling down after a usage limit (#753) are left out - the result may then
    be empty."""
    configured = _configured_providers()
    if db_path is None:
        return configured
    cooled = provider_cooldowns(db_path=db_path)
    return [provider for provider in configured if provider not in cooled]


def _configured_providers() -> list[str]:
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


def available_providers(*, db_path: str | None = None) -> list[str]:
    """Allowed providers whose CLI is actually installed here (and, with
    *db_path*, that are not cooling down after a usage limit)."""
    return [provider for provider in allowed_providers(db_path=db_path) if is_available(provider)]


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


#: One-shot pin for the next selection, set by the initiative tick when it
#: spends a provider-specific extra budget (``extra_budget_<provider>``).
NEXT_PIN_SETTING = "coding_provider.pinned_next"
#: A pin nobody used within this time is dropped (the dispatch never reached
#: a coding run, e.g. a local-only retry).
NEXT_PIN_TTL = timedelta(hours=3)


def pin_next_provider(provider: str, *, db_path: str = DEFAULT_DB_PATH) -> None:
    """Make the next :func:`choose_provider` call return *provider* once."""
    # Only the settings table is touched; no full schema migration needed here.
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS service_settings "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                """
                INSERT INTO service_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (NEXT_PIN_SETTING, _normalize(provider), datetime.now(timezone.utc).isoformat()),
            )
    finally:
        conn.close()


def _take_next_pin(conn: Any, candidates: list[str]) -> str | None:
    """Consume a fresh one-shot pin inside the caller's transaction."""
    row = conn.execute(
        "SELECT value, updated_at FROM service_settings WHERE key = ?", (NEXT_PIN_SETTING,)
    ).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM service_settings WHERE key = ?", (NEXT_PIN_SETTING,))
    try:
        set_at = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
        if set_at.tzinfo is None:
            set_at = set_at.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if datetime.now(timezone.utc) - set_at > NEXT_PIN_TTL:
        return None
    provider = str(row[0])
    return provider if provider in candidates else None


def choose_provider(
    rng: Any | None = None, *, db_path: str = DEFAULT_DB_PATH, use_pin: bool = True
) -> str:
    """Select the next provider in a persistent, concurrency-safe rotation. ``rng``
    is an unused compatibility argument; pins bypass the rotation; the allow list
    and installed-CLI filter apply first, then providers cooling down after a
    usage limit are skipped (#753). If every installed provider is cooling
    down, the rotation runs over them anyway - the initiative tick does not
    start paid work then (``providers_cooling_down``)."""
    pinned = pinned_provider()
    if pinned is not None:
        return pinned

    installed = available_providers()
    cooled = provider_cooldowns(db_path=db_path)
    candidates = [provider for provider in installed if provider not in cooled] or installed
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
        # use_pin=False: a helper worker (e.g. ExperimentService's selection
        # worker, built eagerly) must not use up an extra start meant for the
        # implementation (2026-11-21: the Mistral start ran on Claude).
        once = _take_next_pin(conn, candidates) if use_pin else None
        if once is not None:
            # A provider-specific extra start: this run uses that provider and
            # the rotation position stays where it was.
            conn.commit()
            return once
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
# other agent takes over and the attempt does not count. So does an expired or
# missing login (2026-11-12: "Failed to authenticate: this login reached its
# time limit") - the provider could not run, the work was never tried.
# 502/503/504 and Vibe's infrastructure_failure: the provider's server failed,
# not the work (2026-11-27: a Mistral run for #842 died on a 502 and was
# counted as a failed attempt with no fallback).
_OVERLOAD_RE = re.compile(
    r"\b(?:529|429)\b|overloaded|bad gateway|service unavailable|gateway timeout"
    r"|infrastructure_failure|raw_status_code\W{0,4}50[234]\b"
    r"|rate[ _-]?limit|usage[ _-]?limit|limit (?:reached|exceeded)"
    r"|failed to authenticate|authentication (?:failed|error)|not logged in|invalid api key"
    r"|\b401\b[^\n]{0,40}unauthorized|claude auth login|claude setup-token",
    re.IGNORECASE,
)


def is_provider_overload(error: Any) -> bool:
    """The provider was busy or could not log in, not the work wrong
    ("API Error: 529 Overloaded", "Failed to authenticate"); a usage limit
    (:func:`is_provider_limit`) is an overload too."""
    text = str(error or "")
    return bool(_OVERLOAD_RE.search(text)) or is_provider_limit(text)


# A usage limit with a reset time (#753). 2026-11-18: "You've hit your weekly
# limit · resets 12pm (UTC)" matched nothing above, so 15 runs started on Claude.
# Codex/OpenAI say "usage limit", "quota exceeded" or "insufficient_quota".
# 2026-11-20: the Mistral API answered "http_402" (payment required, no credit
# left), matched nothing and kept Mistral in the rotation.
_LIMIT_RE = re.compile(
    r"hit your (?:(?:weekly|daily|session|5[ -]?hour|monthly|usage) )?limit"
    r"|\blimit\b.{0,20}\bresets?\b"
    r"|usage[ _-]?limit|quota[ _-]?exceeded|exceeded (?:your|the) (?:current )?quota"
    r"|insufficient[ _-]quota"
    r"|\bhttp[ _-]?402\b|\b402\b[^\n]{0,20}payment|payment[ _-]required"
    r"|insufficient[ _-](?:credits?|balance|funds)|out of credits",
    re.IGNORECASE,
)

DEFAULT_LIMIT_COOLDOWN = timedelta(hours=1)
COOLDOWN_SETTING_PREFIX = "provider_cooldown_until."

_RESET_IN_RE = re.compile(
    r"(?:resets?|try again|retry)\s+in\s+((?:\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE,
)
_DURATION_PART_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])", re.IGNORECASE
)
_RESET_AT_RE = re.compile(
    r"(?:resets?|try again|retry)(?:\s+(?:at|after|on))?\s+"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"(?:\s*\(?(?P<tz>utc|gmt|z)\)?)?",
    re.IGNORECASE,
)
_RESET_ISO_RE = re.compile(
    r"(?:resets?|try again|retry)(?:\s+(?:at|after|on))?\s+"
    r"(?P<iso>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)
# Claude's older "Claude AI usage limit reached|1762300800": the reset as epoch seconds.
_RESET_EPOCH_RE = re.compile(r"limit reached\|(\d{10})\b", re.IGNORECASE)


def is_provider_limit(error: Any) -> bool:
    """The provider refused because a usage limit or quota is used up (#753)."""
    return bool(_LIMIT_RE.search(str(error or "")))


def _duration(text: str) -> timedelta:
    total = timedelta()
    for amount, unit in _DURATION_PART_RE.findall(text):
        value = float(amount)
        unit = unit.lower()
        if unit.startswith("d"):
            total += timedelta(days=value)
        elif unit.startswith("h"):
            total += timedelta(hours=value)
        elif unit.startswith("m"):
            total += timedelta(minutes=value)
        else:
            total += timedelta(seconds=value)
    return total


def parse_limit_reset(error: Any, now: datetime | None = None) -> datetime | None:
    """When a limit message says the limit resets, in UTC; ``None`` if it does not.

    Understands ``resets 12pm (UTC)``, ``resets 9:30am``, ``resets in 3h``,
    ``try again in 2 hours 5 minutes``, ``try again at 2026-11-18T12:00:00Z``
    and Claude's ``limit reached|<epoch>``. A clock time without a date is the
    next such time after *now*; times without a zone are taken as UTC.
    """
    text = str(error or "")
    now = now or datetime.now(timezone.utc)
    match = _RESET_IN_RE.search(text)
    if match:
        delta = _duration(match.group(1))
        if delta > timedelta():
            return now + delta
    match = _RESET_ISO_RE.search(text)
    if match:
        try:
            when = datetime.fromisoformat(match.group("iso").replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            when = None
        if when is not None:
            return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    match = _RESET_EPOCH_RE.search(text)
    if match:
        return datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
    match = _RESET_AT_RE.search(text)
    if match and (match.group("ampm") or match.group("minute")):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        ampm = (match.group("ampm") or "").lower()
        if ampm:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if ampm == "pm" else 0)
        if hour > 23 or minute > 59:
            return None
        when = now.astimezone(timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        return when
    return None


def _settings_conn(db_path: str) -> Any:
    return init_db(db_path)


def note_provider_limit(
    provider: str | None, error: Any, *, db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> datetime | None:
    """Cool *provider* down until its limit resets (default: one hour).

    Persists ``provider_cooldown_until.<provider>`` in ``service_settings`` and
    returns the time; ``None`` when *error* is no limit or *provider* is not an
    external provider. An existing later cooldown is kept.
    """
    if not provider or provider not in EXTERNAL_PROVIDERS or not is_provider_limit(error):
        return None
    now = now or datetime.now(timezone.utc)
    until = parse_limit_reset(error, now)
    if until is None or until <= now:
        until = now + DEFAULT_LIMIT_COOLDOWN
    key = COOLDOWN_SETTING_PREFIX + provider
    conn = _settings_conn(db_path)
    try:
        row = conn.execute("SELECT value FROM service_settings WHERE key = ?", (key,)).fetchone()
        current = _parse_utc(row[0]) if row is not None else None
        if current is not None and current > until:
            until = current
        conn.execute(
            """
            INSERT INTO service_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE
            SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, until.isoformat(), now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    logger.warning("coding provider %s hit its usage limit; cooling down until %s", provider, until.isoformat())
    return until


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def provider_cooldowns(
    *, db_path: str = DEFAULT_DB_PATH, now: datetime | None = None
) -> dict[str, datetime]:
    """External providers still cooling down after a limit, with their reset time."""
    now = now or datetime.now(timezone.utc)
    if not os.path.exists(db_path):
        return {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT key, value FROM service_settings WHERE key LIKE ?",
                (COOLDOWN_SETTING_PREFIX + "%",),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:  # an unreadable store must not stop provider selection
        if "no such table" not in str(exc):
            logger.warning("cannot read provider cooldowns: %s", exc)
        return {}
    found: dict[str, datetime] = {}
    for key, value in rows:
        provider = str(key)[len(COOLDOWN_SETTING_PREFIX):]
        until = _parse_utc(value)
        if provider in EXTERNAL_PROVIDERS and until is not None and until > now:
            found[provider] = until
    return found


def cooling_down(provider: str, *, db_path: str = DEFAULT_DB_PATH, now: datetime | None = None) -> bool:
    return provider in provider_cooldowns(db_path=db_path, now=now)


def other_provider(provider: str | None, *, db_path: str | None = None) -> str | None:
    """Another allowed provider with an installed CLI, for when *provider* is
    overloaded; with *db_path*, providers cooling down after a limit are skipped."""
    cooled = provider_cooldowns(db_path=db_path) if db_path else {}
    for candidate in allowed_providers():
        if candidate != provider and candidate not in cooled and is_available(candidate):
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
    if provider == PROVIDER_MISTRAL:
        # The Vibe CLI bills the Vibe Code subscription; the API worker needs
        # credit in the separate API workspace (HTTP 402 on 2026-11-20).
        if vibe_available():
            return VibeWorker(db_path=db_path, **kwargs)
        return MistralWorker(db_path=db_path, **kwargs)
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
    *, db_path: str | None = None, rng: Any | None = None, use_pin: bool = True, **kwargs: Any
) -> tuple[str, Any]:
    """Select a provider and return ``(provider, worker)``."""
    provider = choose_provider(rng, db_path=db_path or DEFAULT_DB_PATH, use_pin=use_pin)
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
