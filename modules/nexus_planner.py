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


# Planner shadow part 2 (#782): the json_schema answer format, the rate-based
# call timeout and the prompt variant recorded with every shadow row.
SHADOW_PROMPT_VARIANT = "compact_v1"
SHADOW_SCHEMA_NAME = "planner_shadow"
SHADOW_REASON_MAX_LENGTH = 160
# The answer is expected to stay short: a number and one sentence.
SHADOW_GENERATION_TOKENS = 120
SHADOW_TIMEOUT_SLACK_SECONDS = 10.0
SHADOW_TIMEOUT_CAP_SECONDS = 240.0
# Used when no local rate sample exists at all, so the timeout is never zero.
SHADOW_TIMEOUT_FALLBACK_SECONDS = 120.0
# After the deadline the monitor waits at most this long for the call to
# honour it (cancel the request, close the connection) before it records the
# timeout row anyway, noting that the call is still running.
SHADOW_CANCEL_GRACE_SECONDS = 15.0


class ShadowDeadlineExpired(TimeoutError):
    """The shared shadow deadline passed before the request was sent (#782)."""


def shadow_response_format(allowed: set[int]) -> dict[str, Any]:
    """The json_schema wire format the shadow call is sent with (#782).

    ``issue`` is an enum of exactly the allowed issue numbers, so a conforming
    server cannot answer with a number that is not allowed; ``reason`` is a
    short string. Built through :func:`modules.llama_cpp_client.json_schema_response_format`,
    the shape both llama-server and Azure actually apply (#218).
    """
    from modules.llama_cpp_client import json_schema_response_format
    return json_schema_response_format(
        SHADOW_SCHEMA_NAME,
        {
            "type": "object",
            "properties": {
                "issue": {"type": "integer", "enum": sorted(int(n) for n in allowed)},
                "reason": {"type": "string", "maxLength": SHADOW_REASON_MAX_LENGTH},
            },
            "required": ["issue", "reason"],
            "additionalProperties": False,
        },
    )


def _positive_rate(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def shadow_timeout_seconds(prompt_tokens: int, rates: Any) -> float:
    """The shadow call timeout derived from the measured local rates (#782).

    ``prompt_tokens / prompt_tps + 120 / gen_tps + 10 s`` - prompt ingestion at
    the measured prompt rate, a 120-token answer at the measured generation
    rate, plus slack - capped at 240 s. *rates* is a
    :class:`modules.local_model_rates.LocalModelRates`-like object with
    ``medians()``; with no rate sample at all the fallback of 120 s is used.
    """
    try:
        prompt_tokens = max(1, int(prompt_tokens))
    except (TypeError, ValueError):
        prompt_tokens = 1
    try:
        prompt_tps, gen_tps = rates.medians()
    except Exception:  # noqa: BLE001 - a broken rates store must not break the tick
        prompt_tps = gen_tps = None
    if not (_positive_rate(prompt_tps) and _positive_rate(gen_tps)):
        return SHADOW_TIMEOUT_FALLBACK_SECONDS
    try:
        seconds = (prompt_tokens / float(prompt_tps)
                   + SHADOW_GENERATION_TOKENS / float(gen_tps)
                   + SHADOW_TIMEOUT_SLACK_SECONDS)
    except (TypeError, ValueError, ZeroDivisionError):
        return SHADOW_TIMEOUT_FALLBACK_SECONDS
    if not math.isfinite(seconds):
        return SHADOW_TIMEOUT_FALLBACK_SECONDS
    return min(SHADOW_TIMEOUT_CAP_SECONDS, max(0.0, seconds))


# SQLite INTEGER is a signed 64-bit value; a bigger int cannot be stored.
_SQLITE_INT_MIN, _SQLITE_INT_MAX = -(2 ** 63), 2 ** 63 - 1


def _strict_int(value: Any) -> bool:
    """True for a real JSON integer: no bool, no float (3.0, 3.9, NaN, inf)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _storable_int(value: Any) -> int | None:
    """*value* when it fits a SQLite INTEGER column, else ``None``."""
    if _strict_int(value) and _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX:
        return int(value)
    return None


def encodable_text(text: str | None) -> str | None:
    """*text* with anything UTF-8 cannot encode (lone surrogates) escaped.

    SQLite stores TEXT as UTF-8; a lone surrogate (e.g. from an escaped
    ``"\\ud800"`` JSON key or an exception message) would make the INSERT
    fail and lose the whole measurement row (#782).
    """
    if text is None:
        return None
    return str(text).encode("utf-8", "backslashreplace").decode("utf-8")


def _strict_json_object(text: str) -> dict[str, Any] | None:
    """The whole *text* parsed as one JSON value, if that value is an object.

    Unlike the paid planner's permissive :func:`_json_object`, nothing is
    extracted from surrounding text, code fences or arrays: the shadow call
    was sent with a json_schema, so anything but a bare top-level object
    (surrounding whitespace aside) does not conform (#782).
    """
    try:
        value = json.loads(text)
    except (ValueError, RecursionError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def validate_shadow_answer(text: str, allowed: set[int]) -> tuple[int | None, bool, str | None]:
    """Check a shadow answer against the requested schema (#782).

    Returns ``(local_issue, valid, error)``. The answer is valid only when the
    entire text is one JSON object with exactly ``issue`` (an integer from
    *allowed*) and ``reason`` (a string of at most 160 characters). An array,
    a fenced block or an object embedded in other text is ``invalid JSON``.
    Nothing is coerced: a float such as ``3.9`` or ``3.0``, a bool, ``NaN``
    or ``Infinity`` is an ``invalid issue``. An integer outside the allowed
    set keeps the historic ``issue is not allowed`` error and is reported as
    ``local_issue`` when it fits a SQLite INTEGER. The error is always
    UTF-8-encodable. Never raises.
    """
    try:
        data = _strict_json_object(text) if isinstance(text, str) else None
    except Exception:  # noqa: BLE001 - a malformed answer is a measurement
        data = None
    if data is None:
        return None, False, "invalid JSON"
    issue = data.get("issue")
    if not _strict_int(issue):
        return None, False, "invalid issue"
    local_issue = _storable_int(issue)
    if issue not in allowed:
        return local_issue, False, "issue is not allowed"
    reason = data.get("reason")
    if not isinstance(reason, str):
        return local_issue, False, "invalid reason"
    if len(reason) > SHADOW_REASON_MAX_LENGTH:
        return local_issue, False, "reason too long"
    extra = sorted(str(key) for key in data if key not in ("issue", "reason"))
    if extra:
        # ascii() escapes lone surrogates and control characters, so the
        # error always fits a SQLite TEXT column; plain names stay readable.
        names = (key if key.isascii() and key.isprintable() else ascii(key) for key in extra)
        return local_issue, False, ("unexpected keys: " + ", ".join(names))[:200]
    return local_issue, True, None


def _resolve_shadow_timeout(prompt_tokens: int, rates: Any, cap: float | None) -> float:
    """The shadow budget, computed on the daemon thread (#782).

    *rates* ``None`` means the local model's default rates bucket. Building
    that store or reading its medians may be slow or fail; any failure gives
    the 120 s fallback. *cap* (the constructor's ``shadow_timeout``) further
    limits the result.
    """
    try:
        if rates is None:
            rates = _default_shadow_rates()
        timeout = shadow_timeout_seconds(prompt_tokens, rates)
        if not (isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0):
            timeout = SHADOW_TIMEOUT_FALLBACK_SECONDS
    except Exception as exc:  # noqa: BLE001 - never lose the measurement row
        logger.warning("planner shadow: rates unavailable, using fallback: %s", exc)
        timeout = SHADOW_TIMEOUT_FALLBACK_SECONDS
    timeout = min(float(timeout), SHADOW_TIMEOUT_CAP_SECONDS)
    if cap is not None:
        timeout = min(timeout, float(cap))
    return timeout


def _default_shadow_rates() -> Any:
    """The rates bucket of the local model the shadow call uses (#782).

    Local calls record their rates under the client's model name
    (modules/local_coding_worker.py), so the planner reads the same bucket.
    """
    from modules.llama_cpp_client import default_model_name
    from modules.local_model_rates import LocalModelRates
    return LocalModelRates(model=default_model_name())


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

    *shadow* is the non-blocking local comparison (#671, #782): it is called as
    ``shadow(prompt, response_format, deadline)`` on a daemon thread, where
    *deadline* is an absolute :func:`time.monotonic` value - the same one the
    monitor uses - that the callable must enforce end to end (no request
    after it, cancel the request at it), and may return the raw answer string or a dict
    ``{"content": ..., "cached_prompt_tokens": ...}`` when the server reported
    reused prompt tokens. *rates* is a
    LocalModelRates-like object whose ``medians()`` size the shadow timeout;
    by default the local model's own rates bucket is used. *shadow_timeout*
    caps that rate-based timeout (default: the 240 s cap of #782).
    """

    def __init__(
        self,
        complete: Callable[[str], str],
        explore: Callable[[str], str] | None = None,
        shadow: Callable[[str, dict[str, Any] | None, float], Any] | None = None,
        *,
        db_path: str | None = None,
        shadow_timeout: float | None = SHADOW_TIMEOUT_CAP_SECONDS,
        rates: Any = None,
    ) -> None:
        self._complete = complete
        self._explore = explore
        self._shadow = shadow
        self._db_path = db_path
        self._shadow_timeout = shadow_timeout
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
                       error="skipped: prompt_over_budget",
                       prompt_variant=SHADOW_PROMPT_VARIANT)
            except Exception as exc:  # noqa: BLE001 - shadow persistence is best effort
                logger.warning("planner shadow: could not store skip: %s", exc)
            return
        prompt = compact + shadow_instructions
        prompt_tokens = max(1, estimate_tokens(prompt))
        # The answer schema restricts issue to the allowed numbers (#782).
        response_format = shadow_response_format(allowed)
        rates, cap = self._rates, self._shadow_timeout

        def monitor() -> None:
            result: list[Any] = []
            failure: list[BaseException] = []
            # The call timeout comes from the measured local rates, capped at
            # 240 s and falling back to 120 s without samples. It is resolved
            # here on the daemon thread: reading the rates file never stalls
            # the tick, and a broken store cannot lose the paid choice (#782).
            timeout = _resolve_shadow_timeout(prompt_tokens, rates, cap)
            # One absolute monotonic deadline shared by the monitor, the
            # worker and the production HTTP request (#782): a worker that
            # starts late gets only what is left and never sends after it.
            started = time.monotonic()
            deadline = started + timeout
            finished_at: list[float] = []

            def call() -> None:
                try:
                    if time.monotonic() >= deadline:
                        raise ShadowDeadlineExpired(
                            f"deadline of {timeout:g}s passed before the request was sent")
                    # The callable must enforce *deadline* (a time.monotonic()
                    # value) end to end, so it cancels the request and releases
                    # its connection instead of running on (#782).
                    result.append(self._shadow(prompt, response_format, deadline))  # type: ignore[misc]
                except BaseException as exc:  # noqa: BLE001 - recorded, never propagated
                    failure.append(exc)
                finally:
                    finished_at.append(time.monotonic())

            worker = threading.Thread(target=call, name="planner-shadow-call", daemon=True)
            worker.start()
            worker.join(max(0.0, deadline - time.monotonic()))
            timed_out = worker.is_alive()
            still_running = False
            if timed_out:
                # Record the timeout only after the call honoured the deadline
                # and released its connection; a callable that ignores it is
                # waited for at most the grace period.
                worker.join(SHADOW_CANCEL_GRACE_SECONDS)
                still_running = worker.is_alive()
            elif finished_at and finished_at[0] >= deadline:
                # Finished, but only at or after the shared deadline.
                timed_out = True
            if failure and isinstance(failure[0], ShadowDeadlineExpired):
                timed_out = True
            elapsed = time.monotonic() - started
            local_issue: int | None = None
            valid = False
            error: str | None = None
            cached_prompt_tokens: int | None = None
            if timed_out:
                error = f"timeout after {timeout:g}s"
                if still_running:
                    error += " (call still running)"
                elif failure and isinstance(failure[0], ShadowDeadlineExpired):
                    error += " (not sent)"
            elif failure:
                error = f"{type(failure[0]).__name__}: {failure[0]}"[:500]
            else:
                try:
                    raw = result[0] if result else ""
                    content: Any = raw
                    if isinstance(raw, dict):
                        # A shadow callable that knows the server timings returns
                        # {"content": ..., "cached_prompt_tokens": ...} (#782).
                        content = raw.get("content", "")
                        cached = _storable_int(raw.get("cached_prompt_tokens"))
                        if cached is not None and cached >= 0:
                            cached_prompt_tokens = cached
                    text = content if isinstance(content, str) else ""
                    local_issue, valid, error = validate_shadow_answer(text, allowed)
                except Exception as exc:  # noqa: BLE001 - a malformed answer is a measurement
                    local_issue, valid = None, False
                    error = f"invalid answer: {type(exc).__name__}"[:500]
            try:
                from modules.planner_shadow import record
                record(self._db_path, tick_id=tick_id, paid_issue=paid_issue,
                       local_issue=local_issue, local_valid=int(valid),
                       prompt_tokens=prompt_tokens, seconds=elapsed,
                       error=encodable_text(error),
                       prompt_variant=SHADOW_PROMPT_VARIANT,
                       cached_prompt_tokens=cached_prompt_tokens)
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
