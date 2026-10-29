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


_EXPLORE = (
    "Before you decide, explore as much as you need. You are in your own "
    "repository: read and search all of your code, tests and docs (Read, Glob, "
    "Grep). The nexus MCP tools give you everything else, for example "
    "github_issues (open and closed, full text), experiments_list, "
    "experiment_get, experiment_logs, activity_list, problems and git_log. The "
    "lists below are only a summary. Before you suggest a new issue, check the "
    "code and the closed issues that it is not already done. If a read tool you "
    "would need is missing, adding it is a good idea of your own.\n\n"
)


def _merge_label(status: str, reason: str) -> str:
    """How the adoption ended, in the planner's words (#439)."""
    if not status:
        return "none"
    if status in ("adopted", "succeeded"):
        return "merged"
    if status == "failed" and reason.startswith("Review by"):
        return "blocked by review"
    return status


def _history_line(r: dict[str, str]) -> str:
    """One recent experiment; with its merge outcome when that is known."""
    if "adoption_status" not in r:
        detail = r.get("error") or ""
        head = f"- {r.get('status', '')}"
    else:
        reason = r.get("adoption_reason") or ""
        detail = reason or r.get("error") or ""
        head = f"- {r.get('status', '')} / merge: {_merge_label(r['adoption_status'], reason)}"
    return f"{head}: {r.get('title', '')}" + (f" ({detail})" if detail else "")


def planning_prompt(
    issues: list[dict[str, Any]],
    recent: list[dict[str, str]] | None = None,
    *,
    explore: bool = False,
) -> str:
    lines = []
    for issue in issues:
        body = " ".join(str(issue.get("body") or "").split())[:_BODY_CHARS]
        lines.append(f"#{issue['number']} {issue['title']}\n{body}")
    history = [_history_line(r) for r in recent or []]
    return (
        "You are Nexus, an independent agent that develops itself. Your own code "
        "lives in this repository, and you pick and do your own work. Below are "
        "your open GitHub issues and how your most recent experiments ended.\n\n"
        + (_EXPLORE if explore else "") +
        "Pick the ONE issue that is the most useful for you to work on next. Prefer "
        "issues that are concrete and small enough for one change with tests, and "
        "that unblock or simplify later work. Skip issues that need a human "
        "decision, a purchase, or setup outside the repository. The web UI is not "
        "a priority: do not pick web UI work.\n\n"
        "Also suggest exactly ONE new issue of your own: a concrete, testable "
        "improvement that no listed issue covers and that makes you work more "
        "reliably or more autonomously. Base it on your own experience: what went "
        "wrong or slowed you down in your recent experiments. No web UI work.\n\n"
        "Answer with exactly one JSON object and nothing else:\n"
        '{"issue": <number>, "reason": "<one sentence why this is next>", '
        '"proposal": {"title": "...", "body": "what to change, where, how to test"}}\n\n'
        "YOUR RECENT EXPERIMENTS:\n\n" + ("\n".join(history) or "(none yet)") + "\n\n"
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
    """Ask a model which open issue comes next.

    *explore* (prompt -> raw answer) is an agent that can read the code and
    use read-only tools (#384); *complete* is a single model call, used alone
    or when exploring fails.
    """

    def __init__(
        self,
        complete: Callable[[str], str],
        explore: Callable[[str], str] | None = None,
    ) -> None:
        self._complete = complete
        self._explore = explore

    def choose(
        self, issues: list[dict[str, Any]], recent: list[dict[str, str]] | None = None
    ) -> PlanChoice | None:
        if not issues:
            return None
        allowed = {int(issue["number"]) for issue in issues}
        choice = None
        if self._explore is not None:
            choice = self._ask(self._explore, planning_prompt(issues, recent, explore=True), allowed)
        if choice is None:
            choice = self._ask(self._complete, planning_prompt(issues, recent), allowed)
        return choice

    @staticmethod
    def _ask(model: Callable[[str], str], prompt: str, allowed: set[int]) -> PlanChoice | None:
        try:
            raw = model(prompt)
        except Exception as exc:  # noqa: BLE001 - no plan is a valid outcome
            logger.warning("planner: model call failed: %s", exc)
            return None
        choice = parse_choice(raw, allowed)
        if choice is None:
            logger.warning("planner: no usable choice in answer %r", (raw or "")[:300])
        return choice
