"""Which external coding agent runs the next task.

Nexus has more than one CLI agent available (Codex and Claude Code). Neither
is preferred: the provider is drawn **at random per task**, so the system
keeps alternating instead of depending on a single vendor, and both agents
accumulate comparable runs in the same store.

The choice is narrowed before the draw:

  1. ``AGENT_CODING_PROVIDER`` pins one provider (debugging, or standing down
     an agent during an incident). An unknown value is ignored rather than
     breaking task execution.
  2. ``AGENT_CODING_PROVIDERS`` is the allow list (default: all known
     providers).
  3. Providers whose CLI is not installed on this machine are dropped, so a
     half-provisioned VM never draws an agent it cannot run.

If that leaves nothing (no CLI installed at all), the first allowed provider
is returned anyway: the resulting run then fails with the worker's ordinary
"binary not found" error, which is the same behaviour as before this module
existed — an honest failure beats a silent no-op.

Callers that only need "a worker" use :func:`build_random_worker` or
:func:`random_worker_factory`; the provider that was actually drawn is
reported on every ``WorkerResult.provider``.
"""

from __future__ import annotations

import logging
import os
import random
import shutil
from typing import Any, Callable

from modules.claude_worker import ClaudeWorker
from modules.claude_worker import PROVIDER as PROVIDER_CLAUDE
from modules.claude_worker import _default_claude_bin
from modules.codex_worker import _DEFAULT_CODEX_BIN, CodexWorker
from modules.codex_worker import PROVIDER as PROVIDER_CODEX

logger = logging.getLogger("modules.coding_provider")

KNOWN_PROVIDERS: tuple[str, ...] = (PROVIDER_CODEX, PROVIDER_CLAUDE)

ENV_PROVIDER_PIN = "AGENT_CODING_PROVIDER"
ENV_PROVIDER_ALLOW_LIST = "AGENT_CODING_PROVIDERS"


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
    """Providers permitted by configuration, in a stable order.

    Unknown entries in ``AGENT_CODING_PROVIDERS`` are dropped with a warning;
    an empty or entirely unknown list falls back to all known providers, so a
    typo cannot leave the system unable to run any task at all.
    """
    raw = os.environ.get(ENV_PROVIDER_ALLOW_LIST, "")
    if not raw.strip():
        return list(KNOWN_PROVIDERS)

    allowed: list[str] = []
    for entry in raw.split(","):
        provider = _normalize(entry)
        if not provider:
            continue
        if provider not in KNOWN_PROVIDERS:
            logger.warning(
                "ignoring unknown provider %r in %s", provider, ENV_PROVIDER_ALLOW_LIST
            )
            continue
        if provider not in allowed:
            allowed.append(provider)

    if not allowed:
        logger.warning(
            "%s contained no known provider; falling back to %s",
            ENV_PROVIDER_ALLOW_LIST, ", ".join(KNOWN_PROVIDERS),
        )
        return list(KNOWN_PROVIDERS)
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
    if provider not in KNOWN_PROVIDERS:
        logger.warning(
            "ignoring unknown provider %r in %s; drawing at random instead",
            provider, ENV_PROVIDER_PIN,
        )
        return None
    return provider


def choose_provider(rng: random.Random | None = None) -> str:
    """Draw the provider for one task.

    Uniform over the installed, allowed providers. Pass *rng* to make the
    draw deterministic (tests, or a reproducible experiment run).
    """
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
        return fallback

    chooser = rng or random
    return chooser.choice(candidates)


def provider_of(worker: Any, default: str | None = None) -> str | None:
    """The provider name *worker* reports, or *default*.

    Anything that is not a known provider name — a test double's auto-created
    attribute, a worker predating the ``provider`` attribute — yields
    *default* rather than a value that would be stored or bound to SQL.
    """
    provider = getattr(worker, "provider", None)
    if isinstance(provider, str) and provider in KNOWN_PROVIDERS:
        return provider
    return default


def build_worker(provider: str, *, db_path: str | None = None, **kwargs: Any) -> Any:
    """Construct the worker for *provider*.

    Extra keyword arguments are passed through to the worker class, so a
    caller can still override e.g. ``max_output_bytes`` or the Claude model.
    """
    provider = _normalize(provider)
    if provider == PROVIDER_CODEX:
        return CodexWorker(db_path=db_path, **kwargs)
    if provider == PROVIDER_CLAUDE:
        return ClaudeWorker(db_path=db_path, **kwargs)
    raise UnknownProviderError(
        f"unknown coding provider {provider!r}; known: {KNOWN_PROVIDERS}"
    )


def build_random_worker(
    *, db_path: str | None = None, rng: random.Random | None = None, **kwargs: Any
) -> tuple[str, Any]:
    """Draw a provider and return ``(provider, worker)``."""
    provider = choose_provider(rng)
    return provider, build_worker(provider, db_path=db_path, **kwargs)


def random_worker_factory(
    *,
    db_path: str | None = None,
    rng: random.Random | None = None,
    sticky: bool = True,
    **kwargs: Any,
) -> Callable[..., Any]:
    """A ``(working_dir) -> worker`` factory that draws a provider per call.

    Matches the factory signature ``ExperimentService`` expects. The working
    directory is not used to confine the worker — that is the worker's own job
    (sandbox mode plus the subprocess ``cwd``) — but it does identify the
    piece of work: with *sticky* (the default) the first draw for a given
    working directory is reused for every later call with the same one, so an
    experiment and its repair attempts run on one agent while different
    experiments still draw independently. Pass ``sticky=False`` for an
    independent draw on every call.
    """
    drawn: dict[str, str] = {}

    def factory(working_dir: Any = None, *_args: Any, **_kwargs: Any) -> Any:
        if not sticky:
            _provider, worker = build_random_worker(db_path=db_path, rng=rng, **kwargs)
            return worker
        key = str(working_dir)
        provider = drawn.get(key)
        if provider is None:
            provider = choose_provider(rng)
            drawn[key] = provider
        return build_worker(provider, db_path=db_path, **kwargs)

    return factory
