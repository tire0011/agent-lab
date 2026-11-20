"""Planning step of the initiative tick: pick the one issue to work on next (#354).

The tick used to take open issues in list order. This asks a strong model
which single open issue is the most useful next step and why. There is no
fixed plan and no count: every tick that is free to start work asks again.

The model may also suggest one new issue. Such a suggestion is opened with the
``proposal`` label, and the tick never works on it until a human removes the
label.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from modules.measurement_health import health_summary, read_health

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


_ADOPTION_PENDING = ("", "not_started", "queued", "adopting")


def _history_line(r: dict[str, Any]) -> str:
    """One recent experiment; with its merge outcome when that is known."""
    # A free re-verification passed (#718): ready only while its adoption has
    # not finished; afterwards the real adoption outcome is shown below.
    if r.get("reverification_verdict") == "passes_now" and str(
        r.get("adoption_status") or ""
    ) in _ADOPTION_PENDING:
        return f"- ready, no paid start needed: {r.get('title', '')}"
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
    stuck_measurements: list[str] | None = None,
) -> str:
    lines = []
    for issue in issues:
        body = " ".join(str(issue.get("body") or "").split())[:_BODY_CHARS]
        lines.append(f"#{issue['number']} {issue['title']}\n{body}")
    history = [_history_line(r) for r in recent or []]
    health = ""
    if stuck_measurements:
        health = "STUCK MEASUREMENT PIPELINES:\n" + "\n".join(stuck_measurements) + "\n\n"
    return (
        health + (mission.strip() if mission and mission.strip() else DEFAULT_MISSION) + "\n\n"
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


# The compact local shadow prompt (#770): the full prompt above is about 6-9k
# tokens, which the CPU llama-server cannot read within any useful timeout.
COMPACT_PROMPT_VARIANT = "compact_v1"
SHADOW_PROMPT_TOKENS_ENV = "NEXUS_PLANNER_SHADOW_PROMPT_TOKENS"
DEFAULT_SHADOW_PROMPT_TOKENS = 1500
_COMPACT_TITLE_CHARS = 90
_COMPACT_LINE_CHARS = 160
_COMPACT_RECENT = 5
_SHADOW_REASON_CHARS = 160
_SHADOW_MAX_TOKENS = 120
_SHADOW_TIMEOUT_CAP = 240.0
_SHADOW_TIMEOUT_SLACK = 10.0
_SHADOW_JOIN_GRACE = 5.0

# Fixed first, so llama-server's prompt cache (#685) reuses it on every tick.
_COMPACT_HEADER = (
    "You are Nexus, an agent that develops its own code. Pick the ONE open issue "
    "that is the most useful to work on next. Prefer issues that unblock or "
    "simplify later work. Skip issues that need a human decision, a purchase or "
    "setup outside the repository. An issue tagged out-of-attempts cannot be "
    "done now; blocked-by-review means the last attempt passed its tests but the "
    "review stopped it.\n"
    'Answer with exactly one JSON object: {"issue": <number from OPEN ISSUES>, '
    '"reason": "<one short sentence>"}\n\n'
)


def estimate_tokens(text: str) -> int:
    """The bytes/4 estimate used for the shadow prompt budget."""
    return max(1, len(text.encode("utf-8")) // 4)


def _one_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _attempts_left(issue: dict[str, Any]) -> int | None:
    left = issue.get("attempts_left")
    if isinstance(left, bool) or not isinstance(left, (int, float)) or left != left:
        return None
    return max(0, int(min(left, 1_000_000)))


def _compact_issue_line(issue: dict[str, Any]) -> str:
    left = _attempts_left(issue)
    tags = []
    if left is not None and left <= 0:
        tags.append("out-of-attempts")
    if issue.get("review_blocked"):
        tags.append("blocked-by-review")
    line = f"#{int(issue['number'])} {_one_line(issue.get('title'), _COMPACT_TITLE_CHARS)}"
    if left is not None:
        line += f" | attempts left: {left}"
    return line + "".join(f" [{tag}]" for tag in tags)


def _compact_recent_line(r: dict[str, Any]) -> str:
    if "adoption_status" in r:
        reason = str(r.get("adoption_reason") or "")
        outcome = f"{r.get('status', '')} / merge: {_merge_label(str(r['adoption_status']), reason)}"
    else:
        outcome = str(r.get("status", ""))
    return _one_line(f"- {outcome}: {r.get('title', '')}", _COMPACT_LINE_CHARS)


class ShadowPromptBudgetError(ValueError):
    """Not even the fixed header and one selectable issue fit the budget."""

    def __init__(self, needed: int, budget: int) -> None:
        super().__init__(
            f"prompt budget too small: needs at least {needed} tokens, budget {budget}")
        self.needed = needed
        self.budget = budget


@dataclass(frozen=True, slots=True)
class CompactShortlist:
    prompt: str
    selectable: frozenset[int]  # listed in the prompt and allowed as the answer
    listed: tuple[int, ...]  # every issue number in the prompt, oldest first
    tokens: int


def compact_shortlist(
    issues: list[dict[str, Any]],
    recent: list[dict[str, Any]] | None,
    stuck_measurements: list[str] | None,
    budget_left: int | None,
    max_tokens: int = DEFAULT_SHADOW_PROMPT_TOKENS,
    *,
    exhausted: list[dict[str, Any]] | None = None,
    eligible: set[int] | None = None,
) -> CompactShortlist:
    """The compact prompt and the issues it lets the local model choose (#770).

    *issues* are the open issues the planner may pick; *exhausted* are open
    issues out of attempts, shown as context with the ``out-of-attempts`` tag
    and never selectable. Selectable means: listed in the prompt, in
    *eligible* (default: every number in *issues*) and not out of attempts.

    The result never exceeds *max_tokens* (bytes/4). Trimming order: the
    oldest issue lines (the last selectable one is always kept), then the
    oldest experiment outcomes, then the stuck-measurement lines. When the
    fixed header with one selectable issue still does not fit, or nothing is
    selectable, :class:`ShadowPromptBudgetError` is raised.
    """
    max_tokens = int(max_tokens)
    if eligible is None:
        eligible = {int(i["number"]) for i in issues}
    by_number: dict[int, tuple[str, bool]] = {}
    for item in exhausted or []:
        number = int(item["number"])
        by_number[number] = (_compact_issue_line({**item, "attempts_left": 0}), False)
    for item in issues:  # an issue the planner may pick wins over a stale exhausted entry
        number = int(item["number"])
        left = _attempts_left(item)
        by_number[number] = (_compact_issue_line(item),
                             number in eligible and (left is None or left > 0))
    entries = [(n, *by_number[n]) for n in sorted(by_number)]
    recent_lines = [_compact_recent_line(r) for r in (recent or [])[:_COMPACT_RECENT]]
    stuck_lines = [_one_line(line, _COMPACT_LINE_CHARS) for line in stuck_measurements or []]
    budget = ("unknown" if budget_left is None else str(max(0, int(budget_left))))

    def build(kept: list[tuple[int, str, bool]], recent_kept: list[str],
              stuck_kept: list[str]) -> str:
        return (
            _COMPACT_HEADER
            + "OPEN ISSUES:\n" + "\n".join(line for _, line, _ in kept) + "\n\n"
            + "RECENT EXPERIMENTS:\n" + ("\n".join(recent_kept) or "(none yet)") + "\n\n"
            + "STUCK MEASUREMENTS:\n" + ("\n".join(stuck_kept) or "(none)") + "\n\n"
            + f"Paid starts left today: {budget}\n"
        )

    if not any(selectable for _, _, selectable in entries):
        raise ShadowPromptBudgetError(estimate_tokens(build([], [], [])), max_tokens)

    def fits() -> bool:
        return estimate_tokens(build(entries, recent_lines, stuck_lines)) <= max_tokens

    while not fits():
        selectable_left = sum(1 for _, _, sel in entries if sel)
        drop = next((i for i, (_, _, sel) in enumerate(entries)
                     if not sel or selectable_left > 1), None)
        if drop is not None and len(entries) > 1:
            del entries[drop]
        elif recent_lines:
            recent_lines.pop()  # newest first: the oldest outcome goes first
        elif stuck_lines:
            stuck_lines.pop()
        else:
            raise ShadowPromptBudgetError(
                estimate_tokens(build(entries, [], [])), max_tokens)
    prompt = build(entries, recent_lines, stuck_lines)
    return CompactShortlist(
        prompt=prompt,
        selectable=frozenset(n for n, _, sel in entries if sel),
        listed=tuple(n for n, _, _ in entries),
        tokens=estimate_tokens(prompt),
    )


def compact_planning_prompt(
    issues: list[dict[str, Any]],
    recent: list[dict[str, Any]] | None,
    stuck_measurements: list[str] | None,
    budget_left: int | None,
    max_tokens: int = DEFAULT_SHADOW_PROMPT_TOKENS,
    *,
    exhausted: list[dict[str, Any]] | None = None,
) -> str:
    """A short planning prompt for the local model (#770).

    One line per open issue (no bodies, no owner decisions), the last five
    experiment outcomes, the stuck measurement pipelines and the budget. The
    fixed header comes first and the lines that change from tick to tick come
    last, so the server's prompt cache can reuse the prefix. The oldest
    (lowest numbered) issues are dropped first until the bytes/4 estimate fits
    *max_tokens*; see :func:`compact_shortlist` for the strict bound.
    """
    return compact_shortlist(issues, recent, stuck_measurements, budget_left, max_tokens,
                             exhausted=exhausted).prompt


def shadow_response_format(allowed: set[int]) -> dict[str, Any]:
    """The schema the local answer must match: an allowed issue and a short reason."""
    from modules.llama_cpp_client import json_schema_response_format

    return json_schema_response_format("planner_shadow", {
        "type": "object",
        "properties": {
            "issue": {"type": "integer", "enum": sorted(allowed)},
            "reason": {"type": "string", "maxLength": _SHADOW_REASON_CHARS},
        },
        "required": ["issue", "reason"],
        "additionalProperties": False,
    })


def shadow_timeout(prompt_tokens: int, prompt_tps: float | None, gen_tps: float | None,
                   fallback: float) -> float:
    """Seconds the local shadow may take, from the measured local rates (#770).

    ``prompt_tokens / prompt_tps + 120 / gen_tps + 10 s``, at most 240 s.
    Without both measured rates *fallback* is used, with the same cap.
    """
    if (isinstance(prompt_tps, (int, float)) and isinstance(gen_tps, (int, float))
            and prompt_tps > 0 and gen_tps > 0):
        seconds = (prompt_tokens / float(prompt_tps) + _SHADOW_MAX_TOKENS / float(gen_tps)
                   + _SHADOW_TIMEOUT_SLACK)
    else:
        seconds = float(fallback)
    return min(_SHADOW_TIMEOUT_CAP, seconds)


def _shadow_prompt_budget() -> int:
    try:
        value = int(os.environ.get(SHADOW_PROMPT_TOKENS_ENV, "") or DEFAULT_SHADOW_PROMPT_TOKENS)
    except ValueError:
        return DEFAULT_SHADOW_PROMPT_TOKENS
    return value if value > 0 else DEFAULT_SHADOW_PROMPT_TOKENS


def _accepts_keywords(func: Callable[..., Any], *names: str) -> bool:
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return True
    present = {p.name for p in params}
    return all(name in present for name in names)


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


SHADOW_MONITOR_THREAD = "planner-shadow-monitor"
_SHADOW_CALL_THREAD = "planner-shadow-call"
_SQLITE_MAX_INT = 2 ** 63 - 1


def _sqlite_int(value: Any) -> int | None:
    """*value* as a non-negative SQLite integer, or ``None`` when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int) and 0 <= value <= _SQLITE_MAX_INT:
        return value
    return None


def _cached_tokens(value: Any) -> int | None:
    return _sqlite_int(value)


def _finite_rates(rates: Any) -> tuple[float | None, float | None]:
    try:
        prompt_tps, gen_tps = rates
    except (TypeError, ValueError):
        return None, None

    def clean(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None

    return clean(prompt_tps), clean(gen_tps)


def _describe_failure(exc: BaseException) -> str:
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:  # noqa: BLE001 - a broken __str__ must not lose the row
        text = type(exc).__name__
    return text[:500]


_SHADOW_FIELDS = frozenset({"issue", "reason"})


def validate_shadow_answer(answer: Any, allowed: set[int] | frozenset[int]
                           ) -> tuple[int | None, bool, str | None]:
    """``(local_issue, valid, error)`` for one local answer (#770).

    The answer must be exactly ``{"issue": <int>, "reason": <str>}``: the issue
    a JSON integer (not a boolean, not a fraction) in *allowed*, the reason a
    non-empty string of at most 160 characters, no other fields. A disallowed
    integer is kept as ``local_issue`` with ``issue is not allowed``.
    """
    if not isinstance(answer, str):
        return None, False, "invalid JSON"
    data = _json_object(answer)
    if data is None:
        return None, False, "invalid JSON"
    raw_issue = data.get("issue")
    if isinstance(raw_issue, bool) or not isinstance(raw_issue, int):
        return None, False, "invalid issue"
    if not -_SQLITE_MAX_INT - 1 <= raw_issue <= _SQLITE_MAX_INT:  # cannot be stored
        return None, False, "invalid issue"
    issue = raw_issue
    if issue not in allowed:
        return issue, False, "issue is not allowed"
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return issue, False, "invalid reason"
    if len(reason) > _SHADOW_REASON_CHARS:
        return issue, False, "reason too long"
    if set(data) != _SHADOW_FIELDS:
        return issue, False, "unexpected fields"
    return issue, True, None


def _store_shadow(db_path: str, row_id: int | None, values: dict[str, Any]) -> None:
    """Persist one outcome: fill the pending row, or insert when there is none."""
    try:
        from modules.planner_shadow import finish, record

        if row_id is not None:
            outcome = {key: values[key] for key in (
                "local_issue", "local_valid", "prompt_tokens", "seconds", "error",
                "cached_prompt_tokens")}
            if not finish(db_path, row_id, **outcome):
                logger.warning("planner shadow: row %s was already finished", row_id)
        else:
            record(db_path, **values)
    except Exception as exc:  # noqa: BLE001 - shadow persistence is best effort
        logger.warning("planner shadow: could not store result: %s", exc)


def wait_for_shadow(max_seconds: float = _SHADOW_TIMEOUT_CAP + 2 * _SHADOW_JOIN_GRACE) -> int:
    """Keep the process alive until running shadow monitors stored their outcome.

    The tick runs as a oneshot unit whose exit ends daemon threads (#770).
    Each monitor ends within its own timeout, so this waits at most
    *max_seconds*; the planning decision never waits on it. Returns how many
    monitors were still running when the wait gave up.
    """
    deadline = time.monotonic() + max(0.0, max_seconds)
    for thread in threading.enumerate():
        if thread.name == SHADOW_MONITOR_THREAD and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
    return sum(1 for t in threading.enumerate()
               if t.name == SHADOW_MONITOR_THREAD and t.is_alive())


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
        shadow_timeout: float = _SHADOW_TIMEOUT_CAP,
        shadow_rates: Callable[[], tuple[float | None, float | None]] | None = None,
    ) -> None:
        """*shadow* is the local model: ``shadow(prompt)`` or, when it accepts
        them, ``shadow(prompt, response_format=..., timeout=...)``. It returns
        the answer text or ``{"content": ..., "tokens_cached": ...}``.
        *shadow_rates* gives the measured ``(prompt, generation)`` tokens/s that
        size the shadow timeout; *shadow_timeout* is used while they are unknown.
        """
        self._complete = complete
        self._explore = explore
        self._shadow = shadow
        self._db_path = db_path
        self._shadow_timeout = shadow_timeout
        self._shadow_rates = shadow_rates

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
        pipelines = read_health(self._db_path, datetime.now(timezone.utc)) if self._db_path else {}
        stuck_measurements = [
            health_summary(name, result) for name, result in pipelines.items()
            if "count" in result
        ]
        context = dict(
            stuck_measurements=stuck_measurements,
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
        db_path = self._db_path
        budget = _shadow_prompt_budget()
        try:
            shortlist = compact_shortlist(
                issues, recent, context.get("stuck_measurements"), context.get("budget_left"),
                max_tokens=budget, exhausted=context.get("exhausted"), eligible=allowed,
            )
        except ShadowPromptBudgetError as exc:
            # Nothing that fits can be asked: recorded, the model is not called.
            _store_shadow(db_path, None, dict(
                tick_id=tick_id, paid_issue=paid_issue, local_issue=None, local_valid=0,
                prompt_tokens=exc.needed, seconds=0.0, error=str(exc)[:500],
                prompt_variant=COMPACT_PROMPT_VARIANT, cached_prompt_tokens=None))
            return
        prompt = shortlist.prompt
        prompt_tokens = shortlist.tokens
        # The enum, the prompt and the validation all use this one set.
        selectable = set(shortlist.selectable)
        response_format = shadow_response_format(selectable)
        shadow = self._shadow
        row_ids: list[int | None] = [None]

        def monitor() -> None:
            # The pending row is written here, not on the tick's thread, so a
            # locked database never delays the planning decision.
            try:
                from modules.planner_shadow import begin

                row_ids[0] = begin(
                    db_path, tick_id=tick_id, paid_issue=paid_issue,
                    prompt_tokens=prompt_tokens, prompt_variant=COMPACT_PROMPT_VARIANT)
            except Exception as exc:  # noqa: BLE001 - the outcome is stored at the end instead
                logger.warning("planner shadow: could not store the pending row: %s", exc)
            row_id = row_ids[0]
            rates: tuple[float | None, float | None] = (None, None)
            if self._shadow_rates is not None:
                try:
                    rates = self._shadow_rates()
                except Exception as exc:  # noqa: BLE001 - unknown rates use the fallback
                    logger.warning("planner shadow: could not read local rates: %s", exc)
            try:
                timeout = shadow_timeout(prompt_tokens, *_finite_rates(rates), self._shadow_timeout)
            except Exception:  # noqa: BLE001 - malformed rates use the fallback
                timeout = min(_SHADOW_TIMEOUT_CAP, float(self._shadow_timeout))
            result: list[Any] = []
            failure: list[BaseException] = []

            def call() -> None:
                try:
                    if _accepts_keywords(shadow, "response_format", "timeout"):  # type: ignore[arg-type]
                        result.append(shadow(  # type: ignore[misc]
                            prompt, response_format=response_format, timeout=timeout))
                    else:
                        result.append(shadow(prompt))  # type: ignore[misc]
                except BaseException as exc:  # noqa: BLE001 - recorded, never propagated
                    failure.append(exc)

            started = time.monotonic()
            worker = threading.Thread(target=call, name=_SHADOW_CALL_THREAD, daemon=True)
            worker.start()
            # A short grace lets the client's own timeout error (with its rates) arrive.
            worker.join(timeout + min(_SHADOW_JOIN_GRACE, 0.1 * timeout))
            elapsed = time.monotonic() - started
            cached: int | None = None
            if worker.is_alive():
                local_issue, valid, error = None, False, f"timeout after {timeout:g}s"
            elif failure:
                local_issue, valid, error = None, False, _describe_failure(failure[0])
            else:
                answer = result[0] if result else ""
                if isinstance(answer, dict):
                    cached = _cached_tokens(answer.get("tokens_cached"))
                    answer = answer.get("content")
                local_issue, valid, error = validate_shadow_answer(answer, selectable)
            _store_shadow(db_path, row_id, dict(
                tick_id=tick_id, paid_issue=paid_issue, local_issue=local_issue,
                local_valid=int(valid), prompt_tokens=prompt_tokens, seconds=elapsed,
                error=error, prompt_variant=COMPACT_PROMPT_VARIANT,
                cached_prompt_tokens=cached))

        try:
            threading.Thread(target=monitor, name=SHADOW_MONITOR_THREAD, daemon=True).start()
        except Exception as exc:  # noqa: BLE001 - e.g. no thread left: record, never raise
            _store_shadow(db_path, None, dict(
                tick_id=tick_id, paid_issue=paid_issue, local_issue=None, local_valid=0,
                prompt_tokens=prompt_tokens, seconds=0.0, error=_describe_failure(exc),
                prompt_variant=COMPACT_PROMPT_VARIANT, cached_prompt_tokens=None))

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
