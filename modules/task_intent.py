"""Intent classification for free-form mailbox task requests.

Before a mailbox ``task`` message is turned into a brand-new experiment,
Nexus must first decide *what kind of request this actually is*. Today the
only supported (heuristic) decision is:

- ``new_experiment``    — create a brand-new experiment from the task body
                          (the historical/default behavior).
- ``resume_experiment`` — the task body explicitly references an existing
                          experiment id together with resume/continue
                          language; reuse that experiment's persisted id
                          and worktree instead of starting a new one.

This module intentionally starts small: the classifier below is a
deterministic, dependency-free heuristic (regex + keyword matching against
``ExperimentService``'s own store) rather than a live LLM call, so the
common "resume experiment <id>" regression case is fixed immediately and
can be exercised in tests without network/model access. The
``TaskIntentDecision`` shape is deliberately generic (``action`` +
``experiment_id`` + ``reason``) so a future local-LLM-backed classifier can
be swapped in behind :func:`classify_task_intent` without changing any
caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.experiment_service import ExperimentService

ACTION_NEW_EXPERIMENT = "new_experiment"
ACTION_RESUME_EXPERIMENT = "resume_experiment"

# Candidate experiment ids: hex tokens of at least 8 characters (matches the
# ``uuid.uuid4().hex`` ids ExperimentService assigns, and the >=8-char
# prefixes it already accepts via resolve_experiment/_resolve_by_prefix).
_HEX_ID_RE = re.compile(r"\b[0-9a-f]{8,64}\b", re.IGNORECASE)

# Language that signals "don't start something new, continue what already
# exists". Kept intentionally small/explicit rather than a fuzzy NLP model.
_RESUME_KEYWORDS_RE = re.compile(
    r"\bresum(?:e|ing)\b|\bcontinu(?:e|ing)\b|\bexisting experiment\b|"
    r"\bdo not (?:re-?implement|create (?:a )?new experiment)\b|"
    r"\breuse (?:its |the )?(?:existing )?worktree\b",
    re.IGNORECASE,
)


@dataclass
class TaskIntentDecision:
    """Result of classifying a free-form mailbox task body."""

    action: str
    experiment_id: str | None = None
    reason: str = ""


def classify_task_intent(
    task_body: str, experiment_service: "ExperimentService | None"
) -> TaskIntentDecision:
    """Classify a free-form task body before it is turned into a pipeline step.

    Returns ``ACTION_RESUME_EXPERIMENT`` (with the resolved existing
    experiment id) only when the task body both (a) mentions resume/continue
    language and (b) contains an id/prefix that resolves to a real,
    previously-created experiment. Any other case — including a mention of
    an id that does not resolve, or resume language without an id — falls
    back to ``ACTION_NEW_EXPERIMENT`` so existing behavior is preserved
    whenever intent is ambiguous or the referenced experiment can't be found.
    """
    body = task_body or ""

    if experiment_service is not None and _RESUME_KEYWORDS_RE.search(body):
        # Try candidate hex tokens longest-first: a full 32-char uuid hex is
        # less ambiguous than a short prefix, so prefer it if both appear.
        candidates = sorted(set(_HEX_ID_RE.findall(body)), key=len, reverse=True)
        for candidate in candidates:
            try:
                experiment = experiment_service.resolve_experiment(candidate.lower())
            except Exception:
                continue
            return TaskIntentDecision(
                action=ACTION_RESUME_EXPERIMENT,
                experiment_id=experiment.id,
                reason=(
                    f"task body references resume/continue language and "
                    f"existing experiment id {candidate!r}"
                ),
            )

    return TaskIntentDecision(
        action=ACTION_NEW_EXPERIMENT,
        reason="no resolvable existing-experiment reference found; treating as a new request",
    )
