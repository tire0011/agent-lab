"""Planning step of the initiative tick: pick the one issue to work on next (#354).

The tick used to take open issues in list order. This asks a strong model
which single open issue is the most useful next step and why. There is no
fixed plan and no count: every tick that is free to start work asks again.

The model may also suggest one new issue. Such a suggestion is opened with the
``proposal`` label, and the tick never works on it until a human removes the
label.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

PROPOSAL_LABEL = "proposal"

_BODY_CHARS = 600
_REASON_CHARS = 300
_PROPOSAL_TITLE_CHARS = 120
_PROPOSAL_BODY_CHARS = 3000


@dataclass(slots=True)
class PlanChoice:
    issue: int
    reason: str
    proposal_title: str = ""
    proposal_body: str = ""


def planning_prompt(issues: list[dict[str, Any]]) -> str:
    lines = []
    for issue in issues:
        body = " ".join(str(issue.get("body") or "").split())[:_BODY_CHARS]
        lines.append(f"#{issue['number']} {issue['title']}\n{body}")
    return (
        "You plan the next piece of work for Nexus, a self-developing agent whose "
        "code lives in this repository. Below are its open GitHub issues.\n\n"
        "Pick the ONE issue that is the most useful to work on next. Prefer issues "
        "that are concrete and small enough for one change with tests, and that "
        "unblock or simplify later work. Skip issues that need a human decision, a "
        "purchase, or setup outside the repository.\n\n"
        "Optionally suggest ONE new issue, but only for a clearly missing, concrete "
        "improvement that no listed issue covers.\n\n"
        "Answer with exactly one JSON object and nothing else:\n"
        '{"issue": <number>, "reason": "<one sentence why this is next>", '
        '"proposal": null or {"title": "...", "body": "what to change, where, how to test"}}\n\n'
        "OPEN ISSUES:\n\n" + "\n\n".join(lines)
    )


def _json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def parse_choice(raw: str, allowed: set[int]) -> PlanChoice | None:
    """The model's answer, or ``None`` when it does not name an allowed issue."""
    data = _json_object(raw or "")
    if data is None:
        return None
    try:
        issue = int(data.get("issue"))
    except (TypeError, ValueError):
        return None
    reason = " ".join(str(data.get("reason") or "").split())[:_REASON_CHARS]
    if issue not in allowed or not reason:
        return None
    choice = PlanChoice(issue=issue, reason=reason)
    proposal = data.get("proposal")
    if isinstance(proposal, dict):
        title = " ".join(str(proposal.get("title") or "").split())[:_PROPOSAL_TITLE_CHARS]
        body = str(proposal.get("body") or "").strip()[:_PROPOSAL_BODY_CHARS]
        if title and body:
            choice.proposal_title, choice.proposal_body = title, body
    return choice


class IssuePlanner:
    """Ask *complete* (prompt -> raw answer) which open issue comes next."""

    def __init__(self, complete: Callable[[str], str]) -> None:
        self._complete = complete

    def choose(self, issues: list[dict[str, Any]]) -> PlanChoice | None:
        if not issues:
            return None
        try:
            raw = self._complete(planning_prompt(issues))
        except Exception as exc:  # noqa: BLE001 - no plan is a valid outcome
            logger.warning("planner: model call failed: %s", exc)
            return None
        choice = parse_choice(raw, {int(issue["number"]) for issue in issues})
        if choice is None:
            logger.warning("planner: no usable choice in answer %r", (raw or "")[:300])
        return choice
