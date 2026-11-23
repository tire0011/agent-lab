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
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from modules.local_model_rates import LocalModelRates
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
    verdict = r.get("reverification_verdict")
    if verdict in {"hung_test", "reverify_timeout"}:
        reason = r.get("reverification_reason")
        detail = f"{verdict} — {reason}" if reason else verdict
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


# The compact local shadow prompt (#770, part 1 in #781): the full prompt
# above is about 6-9k tokens, which the CPU llama-server cannot read within
# any useful timeout. _start_shadow sends only this compact prompt (#790).
DEFAULT_SHADOW_PROMPT_TOKENS = 1500
_COMPACT_TITLE_CHARS = 90
_COMPACT_TITLE_CHARS_TRIMMED = 40
_COMPACT_LINE_CHARS = 160
_COMPACT_RECENT = 5

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
    """The bytes/4 token estimate, rounded up: ceil(utf-8 bytes / 4) (#789).

    Rounding down let a prompt of up to 4 * max_tokens + 3 bytes pass as
    within budget (the codex finding on #787).
    """
    return -(-len(text.encode("utf-8")) // 4)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _one_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _compact_issue_line(issue: dict[str, Any], title_chars: int = _COMPACT_TITLE_CHARS) -> str:
    left = issue.get("attempts_left")
    tags = []
    if left is not None and int(left) <= 0:
        tags.append("out-of-attempts")
    if issue.get("review_blocked"):
        tags.append("blocked-by-review")
    line = f"#{issue['number']} {_one_line(issue.get('title'), title_chars)}"
    if left is not None:
        line += f" | attempts left: {max(0, int(left))}"
    return line + "".join(f" [{tag}]" for tag in tags)


def _compact_recent_line(r: dict[str, Any]) -> str:
    if "adoption_status" in r:
        reason = str(r.get("adoption_reason") or "")
        outcome = f"{r.get('status', '')} / merge: {_merge_label(str(r['adoption_status']), reason)}"
    else:
        outcome = str(r.get("status", ""))
    return _one_line(f"- {outcome}: {r.get('title', '')}", _COMPACT_LINE_CHARS)


def compact_planning_prompt(
    issues: list[dict[str, Any]],
    recent: list[dict[str, Any]] | None,
    stuck_measurements: list[str] | None,
    budget_left: int | None,
    max_tokens: int = DEFAULT_SHADOW_PROMPT_TOKENS,
) -> str | None:
    """A short planning prompt for the local model (#770, #781).

    One line per open issue (number, title, attempts left, blocked/out-of-
    attempts tag - no bodies), the last five experiment outcomes, the stuck
    measurement pipelines and the budget last. The fixed header comes first
    and is byte-stable across calls, so the server's prompt cache can reuse
    the prefix.

    The result never has more than ``4 * max_tokens`` utf-8 bytes, so
    ``estimate_tokens(result) <= max_tokens``. Trimming order:
    the oldest outcomes first, then the oldest (lowest numbered) issues - the
    newest issue is always kept - then the titles cut to 40 characters. When
    even that does not fit (tiny budgets), ``None`` is returned instead of an
    over-budget prompt (#773's codex finding).
    """
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        max_tokens = DEFAULT_SHADOW_PROMPT_TOKENS
    ordered = sorted(issues, key=lambda i: int(i["number"]))
    recent_lines = [_compact_recent_line(r) for r in (recent or [])[:_COMPACT_RECENT]]
    stuck_lines = [_one_line(line, _COMPACT_LINE_CHARS) for line in stuck_measurements or []]
    budget = ("unknown" if budget_left is None else str(max(0, int(budget_left))))

    def build(kept: list[dict[str, Any]], outcomes: list[str], title_chars: int) -> str:
        return (
            _COMPACT_HEADER
            + "OPEN ISSUES:\n" + "\n".join(_compact_issue_line(i, title_chars) for i in kept) + "\n\n"
            + "RECENT EXPERIMENTS:\n" + ("\n".join(outcomes) or "(none yet)") + "\n\n"
            + "STUCK MEASUREMENTS:\n" + ("\n".join(stuck_lines) or "(none)") + "\n\n"
            + f"Paid starts left today: {budget}\n"
        )

    # Compare bytes with the byte cap directly, never a rounded token count.
    cap = 4 * max_tokens
    if _utf8_len(_COMPACT_HEADER) > cap:
        return None  # even the fixed header does not fit
    kept = list(ordered)
    outcomes = list(recent_lines)
    prompt = build(kept, outcomes, _COMPACT_TITLE_CHARS)
    while _utf8_len(prompt) > cap and outcomes:
        outcomes.pop()  # newest first: the oldest outcome goes first
        prompt = build(kept, outcomes, _COMPACT_TITLE_CHARS)
    while _utf8_len(prompt) > cap and len(kept) > 1:
        kept.pop(0)  # oldest issue first; the newest one stays
        prompt = build(kept, outcomes, _COMPACT_TITLE_CHARS)
    if _utf8_len(prompt) > cap:
        prompt = build(kept, outcomes, _COMPACT_TITLE_CHARS_TRIMMED)
    if _utf8_len(prompt) > cap:
        return None
    return prompt


# Part 2a of #782 (#796): ONE rate-based timeout drives both the shadow HTTP
# client and the monitor that joins it. The hard-coded 55 s in nexus_tick.py
# made all 48 planner_shadow rows end in 'local model call timed out after 55s'.
SHADOW_GEN_TOKENS = 120  # max_tokens of the shadow call in nexus_tick.py
SHADOW_TIMEOUT_OVERHEAD_SECONDS = 10.0
SHADOW_TIMEOUT_CAP_SECONDS = 240.0
SHADOW_TIMEOUT_FALLBACK_SECONDS = 120.0  # when no rate is known
# The monitor joins the shadow worker on T + this grace, so a request the
# client bounds by T can never outlive the monitor.
SHADOW_MONITOR_GRACE_SECONDS = 5.0


def _positive_rate(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def shadow_timeout_seconds(prompt_tokens: Any, rates: Any) -> float:
    """The one shadow timeout, in seconds (#796): read the prompt, generate the
    answer, plus overhead - ``prompt_tokens/prompt_tps + 120/gen_tps + 10``,
    capped at 240 s.

    *rates* is a :class:`~modules.local_model_rates.LocalModelRates` (anything
    with a ``medians()`` method) or the ``(prompt_tps, gen_tps)`` pair itself.
    When ``medians()`` returns ``None`` for either rate (no samples yet) or a
    non-positive rate, the fallback of 120 s is returned.
    """
    if callable(getattr(rates, "medians", None)):
        try:
            rates = rates.medians()
        except Exception:  # noqa: BLE001 - a broken rates store means "no samples"
            rates = None
    try:
        prompt_tps, gen_tps = rates if rates is not None else (None, None)
    except (TypeError, ValueError):
        prompt_tps = gen_tps = None
    if not _positive_rate(prompt_tps) or not _positive_rate(gen_tps):
        return SHADOW_TIMEOUT_FALLBACK_SECONDS
    try:
        tokens = float(prompt_tokens)
    except (TypeError, ValueError):
        tokens = 0.0
    if not math.isfinite(tokens) or tokens < 0:
        tokens = 0.0
    seconds = (tokens / float(prompt_tps)
               + SHADOW_GEN_TOKENS / float(gen_tps)
               + SHADOW_TIMEOUT_OVERHEAD_SECONDS)
    return min(SHADOW_TIMEOUT_CAP_SECONDS, seconds)


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
        shadow: Callable[[str, float], str] | None = None,
        *,
        db_path: str | None = None,
        rates: Any = None,
    ) -> None:
        self._complete = complete
        self._explore = explore
        self._shadow = shadow
        self._db_path = db_path
        # #796: the rate store the shadow timeout is computed from; a
        # LocalModelRates (or anything with medians()), None reads the default.
        self._rates = rates

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
        shadow_instructions = (
            '\n\nLOCAL SHADOW ANSWER: Pick only from OPEN ISSUES. Return exactly a short JSON '
            'object with only {"issue": <number>, "reason": "<short reason>"}.'
        )
        # Reserve the suffix's UTF-8 bytes, rounding up to whole estimated tokens.
        try:
            shadow_max_tokens = int(os.environ.get("NEXUS_PLANNER_SHADOW_PROMPT_TOKENS")
                                    or DEFAULT_SHADOW_PROMPT_TOKENS)
        except ValueError:
            shadow_max_tokens = DEFAULT_SHADOW_PROMPT_TOKENS
        compact = compact_planning_prompt(
            issues, recent, context.get("stuck_measurements"), context.get("budget_left"),
            max_tokens=shadow_max_tokens - estimate_tokens(shadow_instructions),
        )
        if compact is None:
            # Over budget even after trimming: record the skip synchronously and
            # never call the model (#790).
            try:
                from modules.planner_shadow import record
                record(self._db_path, tick_id=tick_id, paid_issue=paid_issue,
                       local_issue=None, local_valid=0,
                       prompt_tokens=0, seconds=0.0,
                       error="skipped: prompt_over_budget")
            except Exception as exc:  # noqa: BLE001 - shadow persistence is best effort
                logger.warning("planner shadow: could not store skip: %s", exc)
            return
        prompt = compact + shadow_instructions
        prompt_tokens = max(1, estimate_tokens(prompt))
        # #796: one rate-based timeout drives both the HTTP client (handed to
        # the shadow callable) and this monitor's join below.
        rates = self._rates if self._rates is not None else LocalModelRates()
        timeout = shadow_timeout_seconds(prompt_tokens, rates)
        join_timeout = timeout + SHADOW_MONITOR_GRACE_SECONDS

        def monitor() -> None:
            result: list[str] = []
            failure: list[BaseException] = []

            def call() -> None:
                try:
                    result.append(self._shadow(prompt, timeout))
                except BaseException as exc:  # noqa: BLE001 - recorded, never propagated
                    failure.append(exc)

            worker = threading.Thread(target=call, name="planner-shadow-call", daemon=True)
            worker.start()
            started = time.monotonic()  # the clock starts after worker.start() (#796)
            worker.join(join_timeout)
            elapsed = time.monotonic() - started
            local_issue: int | None = None
            valid = False
            error: str | None = None
            if worker.is_alive():
                error = f"timeout after {join_timeout:g}s"
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
