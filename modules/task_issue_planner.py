"""Turn one free-form task into a GitHub issue using one selected provider.

Selection and fallback deliberately live outside this module.  In particular, an
unusable answer must not cause a second provider call: the caller can retain its
deterministic ``Mailbox task <id>`` placeholder instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_MAX_TOKENS = 1500
_REQUIRED_SECTIONS = ("Goal", "Current state", "Plan", "Acceptance criteria")
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.IGNORECASE | re.DOTALL)


def _prompt(task_text: str, facts: Any) -> str:
    if isinstance(facts, str):
        fact_text = facts
    else:
        try:
            fact_text = json.dumps(facts, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            fact_text = str(facts)
    return (
        "Write a GitHub issue for the task below. Return exactly one JSON object "
        'with string fields "title" and "body"; no commentary. The body must use '
        "Markdown headings named Goal, Current state, Plan, and Acceptance criteria. "
        "Use only the supplied task and facts; do not invent facts.\n\n"
        f"Task:\n{task_text}\n\nFacts:\n{fact_text}"
    )


def _call_once(provider: Any, prompt: str) -> Any:
    """Make the sole external call to the already selected provider."""
    if callable(provider):
        return provider(prompt)
    chat = getattr(provider, "chat", None)
    if callable(chat):
        return chat(prompt)

    # Registry model specifications carry configuration, not a bound method.
    from modules import model_registry

    return model_registry.chat(provider, prompt, max_tokens=_MAX_TOKENS)


def _content(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, (str, Mapping)):
        return result
    if not bool(getattr(result, "success", False)):
        return None
    return getattr(result, "content", None)


def _parse(result: Any) -> dict[str, str] | None:
    content = _content(result)
    if isinstance(content, Mapping):
        data = content
    elif isinstance(content, str):
        match = _FENCE.match(content)
        raw = match.group(1) if match else content
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
    else:
        return None

    if not isinstance(data, Mapping):
        return None
    title, body = data.get("title"), data.get("body")
    if not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not body.strip():
        return None
    for section in _REQUIRED_SECTIONS:
        if re.search(rf"(?im)^\s*#{{1,6}}\s+{re.escape(section)}\s*$", body) is None:
            return None
    return {"title": title.strip(), "body": body.strip()}


def plan_issue(task_text: str, facts: Any, provider: Any) -> dict[str, str] | None:
    """Return a usable issue draft after at most one call to *provider*.

    ``provider`` is selected by the caller.  An unavailable provider, exhausted
    provider budget, failed call, or unusable response all produce ``None`` so the
    existing placeholder remains authoritative.
    """
    if provider is None or getattr(provider, "available", True) is False:
        return None
    if getattr(provider, "budget_exhausted", False) is True:
        return None
    remaining = getattr(provider, "budget_remaining", None)
    if remaining is not None and remaining <= 0:
        return None
    try:
        result = _call_once(provider, _prompt(str(task_text), facts))
    except Exception:
        return None
    return _parse(result)


__all__ = ["plan_issue"]
