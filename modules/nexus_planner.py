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
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

PROPOSAL_LABEL = "proposal"

_BODY_CHARS = 600
_REASON_CHARS = 300
_PROPOSAL_TITLE_CHARS = 120
_PROPOSAL_BODY_CHARS = 3000
# Ideas from stored reflections shown to the planner (#630).
MAX_REFLECTION_IDEAS = 5
_REFLECTION_FIELD_CHARS = 200


# Splitting (#626): a stuck or too large issue becomes 2-4 small parts.
SPLIT_MIN_PARTS = 2
SPLIT_MAX_PARTS = 4
_SPLIT_TITLE_CHARS = 120
_SPLIT_BODY_CHARS = 3000


@dataclass(slots=True)
class PlanChoice:
    issue: int  # 0 when the answer only splits an issue
    reason: str
    proposal_title: str = ""
    proposal_body: str = ""
    split_issue: int = 0
    split_parts: list[tuple[str, str]] | None = None


_EXPLORE = (
    "Before you decide, explore as much as you need. You are in your own "
    "repository: read and search all of your code, tests and docs (Read, Glob, "
    "Grep). The nexus MCP tools give you everything else, for example "
    "github_issues (open and closed, body previews), github_issue (one issue's "
    "full text and comments), experiments_list, "
    "experiment_get, experiment_logs, activity_list, problems, git_log and "
    "local_replays_list (how well the local model redoes merged work). The "
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


def _history_line(r: dict[str, Any]) -> str:
    """One recent experiment; with its merge outcome when that is known."""
    if "adoption_status" not in r:
        detail = r.get("error") or ""
        head = f"- {r.get('status', '')}"
    else:
        reason = r.get("adoption_reason") or ""
        detail = reason or r.get("error") or ""
        head = f"- {r.get('status', '')} / merge: {_merge_label(r['adoption_status'], reason)}"
    drift = (
        f" [commits_behind={r.get('commits_behind', 'unknown')}, "
        f"overlap_files={r.get('overlap_files', 'unknown')}]"
    )
    return f"{head}: {r.get('title', '')}" + (f" ({detail})" if detail else "") + drift


def _exhausted_section(exhausted: list[dict[str, Any]] | None) -> str:
    """Issues Nexus stopped trying (#457): visible, but not a valid answer."""
    if not exhausted:
        return ""
    lines = [
        f"#{i['number']} {i['title']}: {' '.join(str(i['last_reason']).split())[:_REASON_CHARS]}"
        for i in exhausted
    ]
    return (
        "\n\nISSUES OUT OF ATTEMPTS:\n\n"
        "You cannot pick these. Each shows the last failure. If that failure points "
        "at a fault in your own pipeline, your suggested new issue may fix the cause. "
        "You may split one of them into smaller parts (see SPLIT).\n\n"
        + "\n".join(lines)
    )


# The owner can replace this with a mailbox setting `planner_mission` (#541).
DEFAULT_MISSION = (
    "You are Nexus, an independent agent that develops itself. Your own code "
    "lives in this repository, and you pick and do your own work. Below are "
    "your open GitHub issues and how your most recent experiments ended.\n\n"
    "Pick the ONE issue that is the most useful for you to work on next. Prefer "
    "issues that unblock or simplify later work. A larger issue is fine when it "
    "is worth it. Skip issues that need a human decision, a purchase, or setup "
    "outside the repository. The web UI is gone: do not pick web UI work.\n\n"
    "Your local model is free and has no limit. Paid models (Claude, Codex, "
    "Azure) are limited by your daily budget. A long-term goal of yours is to "
    "do more and more of your work with the local model and to make it more "
    "capable. How you get there is up to you: be inventive and go as far as you "
    "can. When you suggest your own new issue, prefer one that moves you "
    "towards that goal, based on what you learned from your recent experiments."
)


def _budget_line(budget_left: int | None) -> str:
    if budget_left is None:
        return ""
    return f"Paid starts left in your daily budget: {max(0, budget_left)}.\n\n"


def planning_prompt(
    issues: list[dict[str, Any]],
    recent: list[dict[str, Any]] | None = None,
    *,
    explore: bool = False,
    exhausted: list[dict[str, Any]] | None = None,
    proposals: list[str] | None = None,
    decisions: list[dict[str, str]] | None = None,
    mission: str | None = None,
    budget_left: int | None = None,
    reflections: list[dict[str, str]] | None = None,
) -> str:
    lines = []
    for issue in issues:
        body = " ".join(str(issue.get("body") or "").split())[:_BODY_CHARS]
        lines.append(f"#{issue['number']} {issue['title']}\n{body}")
    history = [_history_line(r) for r in recent or []]
    return (
        (mission.strip() if mission and mission.strip() else DEFAULT_MISSION) + "\n\n"
        + _budget_line(budget_left)
        + (_EXPLORE if explore else "") +
        "Also suggest exactly ONE new issue of your own: a concrete, testable "
        "improvement that no listed issue covers and that makes you work more "
        "reliably or more autonomously. Base it on your own experience: what went "
        "wrong or slowed you down in your recent experiments. No web UI work.\n\n"
        "You may also SPLIT one issue that is out of attempts or too large to "
        "do in one experiment into 2 to 4 smaller parts. Each part is one "
        "concrete change with its own test that one experiment can finish. "
        "A part may also be a probe: try one approach, measure it, and write "
        "the result as a comment, so that you can decide the next step on "
        "evidence. Parts become issues you can pick on later ticks; the "
        "split issue stays open until all of its parts are closed. Do not "
        "split an issue you can still pick and do in one go.\n\n"
        "Answer with exactly one JSON object and nothing else:\n"
        '{"issue": <number or null>, "reason": "<one sentence why this is next>", '
        '"proposal": {"title": "...", "body": "what to change, where, how to test"}, '
        '"split": {"issue": <number>, "parts": [{"title": "...", "body": "what to '
        'change, where, how to test"}, ...]}}\n'
        '"split" is optional. "issue" may be null only when you split.\n\n'
        "YOUR RECENT EXPERIMENTS:\n\n" + ("\n".join(history) or "(none yet)") + "\n\n"
        "OPEN ISSUES:\n\n" + "\n\n".join(lines) + _exhausted_section(exhausted)
        + _proposals_section(proposals) + _decisions_section(decisions)
        + _reflections_section(reflections)
    )


def _reflections_section(reflections: list[dict[str, str]] | None) -> str:
    """Ideas from your own post-experiment reflections (#630), with their evidence."""
    if not reflections:
        return ""
    lines = []
    for r in reflections[:MAX_REFLECTION_IDEAS]:
        parts = [f"benefit: {r.get('benefit', '')}", f"cost: {r.get('cost', '')}",
                 f"risk: {r.get('risk', '')}", f"measure: {r.get('measure', '')}"]
        detail = "; ".join(" ".join(str(p).split())[:_REFLECTION_FIELD_CHARS] for p in parts)
        source = f" (from experiment {r['experiment_id']})" if r.get("experiment_id") else ""
        title = " ".join(str(r.get("title", "")).split())[:_PROPOSAL_TITLE_CHARS]
        lines.append(f"- {title}{source}: {detail}")
    return (
        "\n\nIDEAS FROM YOUR OWN REFLECTIONS (what you suggested after recent "
        "experiments; none duplicates an open issue). Your own new issue may build "
        "on one of these:\n\n" + "\n".join(lines)
    )


def _decisions_section(decisions: list[dict[str, str]] | None) -> str:
    """Proposals the owner closed and why, so the planner respects those decisions."""
    if not decisions:
        return ""
    return (
        "\n\nOWNER DECISIONS (proposals the owner closed, and why). Do not suggest "
        "anything that contradicts these unless you have new evidence, and then say "
        "what is new:\n\n"
        + "\n".join(f"- {d.get('title', '')}: {d.get('reason', '')}" for d in decisions)
    )


def _proposals_section(proposals: list[str] | None) -> str:
    """Your own suggestions still waiting for the owner, so you do not repeat them."""
    if not proposals:
        return ""
    return (
        "\n\nYOUR OPEN PROPOSALS (waiting for the owner; do not suggest these or "
        "near-duplicates again):\n\n" + "\n".join(f"- {title}" for title in proposals)
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


def _parse_split(data: dict[str, Any], splittable: set[int]) -> tuple[int, list[tuple[str, str]]] | None:
    split = data.get("split")
    if not isinstance(split, dict):
        return None
    try:
        number = int(split.get("issue"))
    except (TypeError, ValueError):
        return None
    parts = split.get("parts")
    if number not in splittable or not isinstance(parts, list):
        return None
    cleaned = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        title = " ".join(str(part.get("title") or "").split())[:_SPLIT_TITLE_CHARS]
        body = str(part.get("body") or "").strip()[:_SPLIT_BODY_CHARS]
        if title and body:
            cleaned.append((title, body))
    if not SPLIT_MIN_PARTS <= len(cleaned) <= SPLIT_MAX_PARTS:
        return None
    return number, cleaned


def parse_choice(raw: str, allowed: set[int], splittable: set[int] | None = None) -> PlanChoice | None:
    """The model's answer, or ``None`` when it neither names an allowed issue
    nor splits one in *splittable*."""
    data = _json_object(raw or "")
    if data is None:
        return None
    reason = " ".join(str(data.get("reason") or "").split())[:_REASON_CHARS]
    split = _parse_split(data, splittable or set())
    try:
        issue = int(data.get("issue"))
    except (TypeError, ValueError):
        issue = 0
    if issue not in allowed:
        issue = 0
    if not reason or (issue == 0 and split is None):
        return None
    choice = PlanChoice(issue=issue, reason=reason)
    if split is not None:
        choice.split_issue, choice.split_parts = split
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
        shadow: Callable[[str], str] | None = None,
        *,
        db_path: str | None = None,
        shadow_timeout: float = 60.0,
    ) -> None:
        self._complete = complete
        self._explore = explore
        self._shadow = shadow
        self._db_path = db_path
        self._shadow_timeout = shadow_timeout

    def choose(
        self,
        issues: list[dict[str, Any]],
        recent: list[dict[str, Any]] | None = None,
        exhausted: list[dict[str, Any]] | None = None,
        proposals: list[str] | None = None,
        decisions: list[dict[str, str]] | None = None,
        mission: str | None = None,
        budget_left: int | None = None,
        reflections: list[dict[str, str]] | None = None,
        tick_id: str | None = None,
    ) -> PlanChoice | None:
        allowed = {int(issue["number"]) for issue in issues}
        splittable = allowed | {int(i["number"]) for i in exhausted or []}
        if not splittable:
            return None
        context = dict(
            exhausted=exhausted, proposals=proposals, decisions=decisions,
            mission=mission, budget_left=budget_left, reflections=reflections,
        )
        choice = None
        if self._explore is not None:
            choice = self._ask(self._explore, planning_prompt(issues, recent, explore=True, **context),
                               allowed, splittable)
        if choice is None:
            choice = self._ask(self._complete, planning_prompt(issues, recent, **context), allowed, splittable)
        if choice is not None and choice.issue:
            self._start_shadow(
                choice.issue, allowed, issues, recent, context,
                tick_id or uuid.uuid4().hex,
            )
        return choice

    def _start_shadow(self, paid_issue: int, allowed: set[int], issues: list[dict[str, Any]],
                      recent: list[dict[str, Any]] | None, context: dict[str, Any],
                      tick_id: str) -> None:
        if (self._shadow is None or not self._db_path or
                os.environ.get("NEXUS_PLANNER_SHADOW", "1").strip() == "0"):
            return
        prompt = planning_prompt(issues, recent, explore=False, **context) + (
            '\n\nLOCAL SHADOW ANSWER: Pick only from OPEN ISSUES. Return exactly a short JSON '
            'object with only {"issue": <number>, "reason": "<short reason>"}.'
        )
        prompt_tokens = max(1, len(prompt.encode("utf-8")) // 4)

        def monitor() -> None:
            result: list[str] = []
            failure: list[BaseException] = []

            def call() -> None:
                try:
                    result.append(self._shadow(prompt))  # type: ignore[misc]
                except BaseException as exc:  # noqa: BLE001 - recorded, never propagated
                    failure.append(exc)

            started = time.monotonic()
            worker = threading.Thread(target=call, name="planner-shadow-call", daemon=True)
            worker.start()
            worker.join(self._shadow_timeout)
            elapsed = time.monotonic() - started
            local_issue: int | None = None
            valid = False
            error: str | None = None
            if worker.is_alive():
                error = f"timeout after {self._shadow_timeout:g}s"
            elif failure:
                error = f"{type(failure[0]).__name__}: {failure[0]}"[:500]
            else:
                data = _json_object(result[0] if result else "")
                if data is None:
                    error = "invalid JSON"
                else:
                    try:
                        local_issue = int(data.get("issue"))
                    except (TypeError, ValueError):
                        error = "invalid issue"
                    else:
                        valid = local_issue in allowed
                        if not valid:
                            error = "issue is not allowed"
            try:
                from modules.planner_shadow import record
                record(self._db_path, tick_id=tick_id, paid_issue=paid_issue,
                       local_issue=local_issue, local_valid=int(valid),
                       prompt_tokens=prompt_tokens, seconds=elapsed, error=error)
            except Exception as exc:  # noqa: BLE001 - shadow persistence is best effort
                logger.warning("planner shadow: could not store result: %s", exc)

        threading.Thread(target=monitor, name="planner-shadow", daemon=True).start()

    @staticmethod
    def _ask(model: Callable[[str], str], prompt: str, allowed: set[int],
             splittable: set[int] | None = None) -> PlanChoice | None:
        try:
            raw = model(prompt)
        except Exception as exc:  # noqa: BLE001 - no plan is a valid outcome
            logger.warning("planner: model call failed: %s", exc)
            return None
        choice = parse_choice(raw, allowed, splittable)
        if choice is None:
            logger.warning("planner: no usable choice in answer %r", (raw or "")[:300])
        return choice
