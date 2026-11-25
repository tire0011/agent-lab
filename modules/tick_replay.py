"""Counterfactual tick replay (#755): how many paid starts a policy would have saved.

:func:`replay` walks finished experiments in start order. For every *paid*
start (a provider other than the free local worker) each policy is asked
``policy(history_so_far, candidate) -> "allow" | ("skip", reason)``. A start
that any policy skips counts as *saved*; if that run really ended merged, it
also counts as a *lost merge*, so a policy that blocks good work shows up.

Policies are pure functions: no clock, no DB, no subprocess, no network. They
see :class:`ReplayRow` objects whose derived facts (limit reset, quick run
with empty usage, no-change end, gate reason) are computed once, so 30 days
of rows replay in well under a second.

``history_so_far`` holds the rows that had *ended* before the candidate
started, oldest end first. Two modes:

- ``"actual"`` (default): every real row - what Nexus would have known at
  that tick on the real path. Rows a policy skipped still inform later
  decisions, so a cooldown is refreshed by runs that would not have happened;
  the saved count is an upper bound by about one probe per cooldown.
- ``"simulated"``: only rows the policies allowed. Stricter, but a skipped
  start then can never produce the evidence that would have ended a pause.

What the replay cannot know: which other issue or provider Nexus would have
started instead of a skipped one. ``simulated_paid_starts`` is therefore the
real starts minus the skipped ones, not a full re-simulation of the planner.

Later gate issues add their policy here as one function in :data:`POLICIES`
and one fixture test, and quote the replay number before they count as done.

Reading rows from the run store lives in :mod:`modules.tick_replay_store`.
"""

from __future__ import annotations

import bisect
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence, Union

from modules.coding_provider import is_provider_limit, is_provider_overload, parse_limit_reset
from modules.experiment_service import policy as goal_policy

Decision = Union[str, tuple[str, str]]
Policy = Callable[[Sequence["ReplayRow"], "ReplayRow"], Decision]

ALLOW = "allow"
FREE_PROVIDERS = frozenset({"local"})
MERGED_ADOPTION_STATUSES = ("adopted", "succeeded")
HISTORY_MODES = ("actual", "simulated")
FIRST_SKIPPED = 5

# provider_cooldown (#753): mirrors coding_provider.DEFAULT_LIMIT_COOLDOWN.
LIMIT_COOLDOWN = timedelta(hours=1)
# A run this short with no token usage never reached the model: the provider is
# down or locked out, the same as a limit without a reset time (#755).
QUICK_RUN_SECONDS = 10.0
QUICK_EMPTY_COOLDOWN = timedelta(hours=1)
# A provider counts as usable when it started a paid run this recently; when
# every usable provider is cooling down, nothing paid starts.
USABLE_PROVIDER_WINDOW = timedelta(days=7)

# issue_loop_guard (#753): mirrors nexus_initiative._LOOP_GUARD_*.
LOOP_GUARD_MAX_NO_CHANGE = 3
LOOP_GUARD_WINDOW = timedelta(hours=1)
LOOP_GUARD_PAUSE = timedelta(hours=6)
NO_CHANGE_STATUSES = ("failed", "timed_out", "rejected", "interrupted", "needs_review", "cancelled")
# Rejected by the goal policy before any work (#408), as nexus_initiative reads it.
_GOAL_POLICY_PREFIXES = tuple(
    prefix for prefix, _ in goal_policy._POLICY_RULE_PREFIXES if prefix.startswith("goal ")
)

# same_gate_reason_x3 (#730): the last 3 finished experiments, on 3 different
# issues, ended with the same normalized gate reason -> a host blocker. #730
# switches that gate off; the replay counts the paid starts the blocker would
# have burned while it stood (an upper bound for #730's saving).
SAME_GATE_REPEATS = 3
SAME_GATE_PAUSE = timedelta(hours=6)
GATE_STATUSES = ("failed", "needs_review", "timed_out")


def _parse_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _has_positive_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, Mapping):
        return any(_has_positive_number(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_positive_number(v) for v in value)
    return False


def usage_is_empty(usage_json: Any) -> bool:
    """True when the stored usage holds no positive token or cost number."""
    if usage_json is None:
        return True
    if isinstance(usage_json, (Mapping, list, tuple)):
        return not _has_positive_number(usage_json)
    text = str(usage_json).strip()
    if not text:
        return True
    try:
        return not _has_positive_number(json.loads(text))
    except ValueError:
        return False  # unreadable is not proof of "empty"


_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_PATH_RE = re.compile(r"[\w./-]*\.py(?:::[\w\[\]./:-]+)?")
_NUMBER_RE = re.compile(r"\d+")
_SPACE_RE = re.compile(r"\s+")


def gate_reason(status: Any, error: Any) -> str | None:
    """The normalized gate reason of a failed run, ``None`` if it has none.

    The text before the first ``:`` (e.g. ``timing check failed closed``),
    lowercased, with shas, test paths and numbers replaced, so the same gate
    failing on different tests reads the same. The #730 timing-check reason
    keeps its ``without drift too`` marker. Provider overloads and limits are
    no gate reason.
    """
    text = str(error or "").strip()
    if str(status or "") not in GATE_STATUSES or not text or is_provider_overload(text):
        return None
    head = text.splitlines()[0]
    prefix = head.split(":", 1)[0] if ":" in head else head[:80]
    prefix = _SHA_RE.sub("<sha>", prefix.lower())
    prefix = _PATH_RE.sub("<path>", prefix)
    prefix = _NUMBER_RE.sub("#", prefix)
    prefix = _SPACE_RE.sub(" ", prefix).strip()
    if not prefix:
        return None
    if "without drift too" in text.lower():
        prefix += ": ... without drift too"
    return prefix


@dataclass(frozen=True)
class ReplayRow:
    """One finished experiment with the facts the policies need, computed once."""

    id: str
    started_at: datetime
    ended_at: datetime
    issue: int | None
    provider: str | None
    status: str
    error: str | None
    implementation_usage_json: Any
    duration: float | None
    merged: bool
    has_diff: bool
    # Derived in from_mapping:
    paid: bool = False
    limit_until: datetime | None = None
    quick_empty: bool = False
    # Reached the model (usage > 0) and hit no limit: the provider works.
    worked: bool = False
    no_change: bool = False
    gate: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ReplayRow | None":
        """Build a row; ``None`` for a row that has not finished or has no start."""
        ended = _parse_time(raw.get("ended_at"))
        started = _parse_time(raw.get("started_at")) or ended
        if ended is None or started is None:
            return None
        status = str(raw.get("status") or "")
        error = raw.get("error")
        error_text = str(error) if error is not None else None
        provider = raw.get("provider")
        provider = str(provider) if provider else None
        duration = raw.get("duration")
        if duration is None:
            duration = max(0.0, (ended - started).total_seconds())
        else:
            duration = float(duration)
        issue = raw.get("issue")
        try:
            issue = int(issue) if issue not in (None, "") else None
        except (TypeError, ValueError):
            issue = None
        merged = raw.get("merged")
        if merged is None:
            merged = str(raw.get("adoption_status") or "") in MERGED_ADOPTION_STATUSES
        has_diff = bool(raw.get("has_diff", False))
        usage = raw.get("implementation_usage_json")

        limit_until = None
        if is_provider_limit(error_text):
            limit_until = parse_limit_reset(error_text, ended)
            if limit_until is None or limit_until <= ended:
                limit_until = ended + LIMIT_COOLDOWN
        empty = usage_is_empty(usage)
        quick_empty = status != "succeeded" and duration < QUICK_RUN_SECONDS and empty
        no_change = (
            status in NO_CHANGE_STATUSES
            and (not has_diff or "no publishable" in (error_text or ""))
            and not (status == "rejected" and (error_text or "").startswith(_GOAL_POLICY_PREFIXES))
        )
        return cls(
            id=str(raw.get("id") or ""),
            started_at=started,
            ended_at=ended,
            issue=issue,
            provider=provider,
            status=status,
            error=error_text,
            implementation_usage_json=usage,
            duration=duration,
            merged=bool(merged),
            has_diff=has_diff,
            paid=provider is not None and provider not in FREE_PROVIDERS,
            limit_until=limit_until,
            quick_empty=quick_empty,
            worked=not empty and limit_until is None,
            no_change=no_change,
            gate=gate_reason(status, error_text),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at.isoformat(),
            "issue": self.issue,
            "provider": self.provider,
            "status": self.status,
            "merged": self.merged,
        }


# --- policies -----------------------------------------------------------------


def provider_cooldowns(history: Sequence[ReplayRow]) -> dict[str, datetime]:
    """``provider -> cooled until`` from *history* (#753 + quick empty runs).

    *history* is in end order. A later run of the same provider that worked
    (usage > 0, no limit) lifts its cooldown: the provider is back.
    """
    until: dict[str, datetime] = {}
    for row in history:
        if not row.paid or row.provider is None:
            continue
        if row.worked:
            until.pop(row.provider, None)
            continue
        ends = []
        if row.limit_until is not None:
            ends.append(row.limit_until)
        if row.quick_empty:
            ends.append(row.ended_at + QUICK_EMPTY_COOLDOWN)
        for end in ends:
            if row.provider not in until or end > until[row.provider]:
                until[row.provider] = end
    return until


def provider_cooldown(history: Sequence[ReplayRow], candidate: ReplayRow) -> Decision:
    """Skip when every usable paid provider is cooling down (#753).

    A limit message cools its provider until the parsed reset (else 1 h); a
    run under 10 s with empty usage cools it for 1 h; a later working run of
    that provider lifts it. Usable providers are the
    candidate's and those that started a paid run in the last 7 days. If only
    some are cooling the tick would start another one: still a paid start.
    """
    now = candidate.started_at
    usable = {candidate.provider} | {
        row.provider for row in history
        if row.paid and row.started_at >= now - USABLE_PROVIDER_WINDOW
    }
    usable.discard(None)
    cooled = {p: t for p, t in provider_cooldowns(history).items() if t > now}
    if usable and all(p in cooled for p in usable):
        resets = ", ".join(f"{p} until {cooled[p].isoformat()}" for p in sorted(usable))
        return ("skip", f"providers_cooling_down: {resets}")
    return ALLOW


def issue_loop_guard(history: Sequence[ReplayRow], candidate: ReplayRow) -> Decision:
    """Skip an issue for 6 h after more than 3 no-change ends within 1 h (#753)."""
    if candidate.issue is None:
        return ALLOW
    times = sorted(r.ended_at for r in history if r.issue == candidate.issue and r.no_change)
    quick = LOOP_GUARD_MAX_NO_CHANGE + 1
    trigger = None
    for i in range(quick - 1, len(times)):
        if times[i] - times[i - quick + 1] <= LOOP_GUARD_WINDOW:
            trigger = times[i]
    if trigger is not None and candidate.started_at < trigger + LOOP_GUARD_PAUSE:
        count = sum(1 for t in times if trigger - LOOP_GUARD_WINDOW <= t <= trigger)
        return ("skip", f"skipped_loop_guard: #{candidate.issue} had {count} no-change ends "
                        f"within 60 min; paused until {(trigger + LOOP_GUARD_PAUSE).isoformat()}")
    return ALLOW


def same_gate_reason_x3(history: Sequence[ReplayRow], candidate: ReplayRow) -> Decision:
    """Skip while a host blocker stands (#730).

    The last 3 finished experiments (provider overloads left out: they say
    nothing about the gate) on 3 different issues ended with the same
    normalized gate reason; skip paid starts for 6 h after the third.
    """
    recent: list[ReplayRow] = []
    for row in reversed(history):
        if row.error and is_provider_overload(row.error):
            continue
        recent.append(row)
        if len(recent) == SAME_GATE_REPEATS:
            break
    if len(recent) < SAME_GATE_REPEATS:
        return ALLOW
    reasons = {row.gate for row in recent}
    issues = {row.issue for row in recent}
    if len(reasons) != 1 or None in reasons or None in issues or len(issues) != SAME_GATE_REPEATS:
        return ALLOW
    third = max(row.ended_at for row in recent)
    if candidate.started_at < third + SAME_GATE_PAUSE:
        return ("skip", f"host_blocker: {recent[0].gate!s} on #"
                        + ", #".join(str(i) for i in sorted(issues)))
    return ALLOW


POLICIES: dict[str, Policy] = {
    "provider_cooldown": provider_cooldown,
    "issue_loop_guard": issue_loop_guard,
    "same_gate_reason_x3": same_gate_reason_x3,
}


def resolve_policies(names: str | Iterable[str] | None) -> dict[str, Policy]:
    """Policy functions by name; ``None``/empty means all. Unknown names raise ``ValueError``."""
    if names is None:
        return dict(POLICIES)
    if isinstance(names, str):
        names = [n for n in names.split(",")]
    wanted = [n.strip() for n in names if n and n.strip()]
    if not wanted:
        return dict(POLICIES)
    unknown = [n for n in wanted if n not in POLICIES]
    if unknown:
        raise ValueError(
            f"unknown policy: {', '.join(unknown)} (known: {', '.join(POLICIES)})"
        )
    return {n: POLICIES[n] for n in dict.fromkeys(wanted)}


# --- replay -------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    start: str | None
    end: str | None
    policies: list[str]
    history: str
    finished_rows: int
    actual_paid_starts: int
    simulated_paid_starts: int
    saved_starts: int
    lost_merges: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    per_policy: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self, first_skipped: int = FIRST_SKIPPED) -> dict[str, Any]:
        data = asdict(self)
        data.pop("skipped")
        data["lost_merge_count"] = len(self.lost_merges)
        data["first_skipped"] = [dict(row) for row in self.skipped[:first_skipped]]
        return data


def _decide(policy: Policy, history: Sequence[ReplayRow], row: ReplayRow) -> str | None:
    """The skip reason, or ``None`` for allow. Anything not a skip is an allow."""
    decision = policy(history, row)
    if isinstance(decision, tuple) and len(decision) == 2 and decision[0] == "skip":
        return str(decision[1])
    return None


def replay(
    rows: Iterable[Mapping[str, Any] | ReplayRow],
    policies: Mapping[str, Policy] | Iterable[str] | str | None,
    start: Any = None,
    end: Any = None,
    *,
    history: str = "actual",
) -> Report:
    """Replay the finished experiments that started in ``[start, end)``.

    Rows before *start* still form the history (a cooldown from the evening
    before counts). The first skipping policy (in the given order) names the
    reason; ``per_policy`` counts each policy on its own as well.
    """
    if history not in HISTORY_MODES:
        raise ValueError(f"history must be one of {', '.join(HISTORY_MODES)}")
    chosen = policies if isinstance(policies, Mapping) else resolve_policies(policies)
    start_at = _parse_time(start)
    end_at = _parse_time(end)
    parsed = [r if isinstance(r, ReplayRow) else ReplayRow.from_mapping(r) for r in rows]
    finished = [r for r in parsed if r is not None]
    by_end = sorted(finished, key=lambda r: (r.ended_at, r.started_at, r.id))
    end_times = [r.ended_at for r in by_end]
    candidates = sorted(
        (r for r in finished
         if (start_at is None or r.started_at >= start_at)
         and (end_at is None or r.started_at < end_at)),
        key=lambda r: (r.started_at, r.id),
    )
    skipped_ids: set[str] = set()
    actual = simulated = 0
    lost: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    per_policy = {name: {"saved_starts": 0, "lost_merges": 0} for name in chosen}
    for row in candidates:
        if not row.paid:
            continue
        actual += 1
        known = by_end[: bisect.bisect_right(end_times, row.started_at)]
        if history == "simulated":
            known = [r for r in known if r.id not in skipped_ids]
        first: tuple[str, str] | None = None
        for name, policy in chosen.items():
            reason = _decide(policy, known, row)
            if reason is None:
                continue
            per_policy[name]["saved_starts"] += 1
            per_policy[name]["lost_merges"] += int(row.merged)
            if first is None:
                first = (name, reason)
        if first is None:
            simulated += 1
            continue
        skipped_ids.add(row.id)
        entry = {**row.summary(), "policy": first[0], "reason": first[1]}
        skipped.append(entry)
        if row.merged:
            lost.append(entry)
    return Report(
        start=start_at.isoformat() if start_at else None,
        end=end_at.isoformat() if end_at else None,
        policies=list(chosen),
        history=history,
        finished_rows=len(candidates),
        actual_paid_starts=actual,
        simulated_paid_starts=simulated,
        saved_starts=actual - simulated,
        lost_merges=lost,
        skipped=skipped,
        per_policy=per_policy,
    )


def format_report(report: Report, first_skipped: int = FIRST_SKIPPED) -> str:
    """Plain text for the script and issue comments."""
    lines = [
        f"Tick replay {report.start or 'begin'} .. {report.end or 'now'} "
        f"(policies: {', '.join(report.policies)}; history: {report.history})",
        f"finished experiments: {report.finished_rows}",
        f"paid starts actual: {report.actual_paid_starts}",
        f"paid starts simulated: {report.simulated_paid_starts}",
        f"saved starts: {report.saved_starts}",
        f"lost merges: {len(report.lost_merges)}",
    ]
    for name, counts in report.per_policy.items():
        lines.append(f"  {name}: saved {counts['saved_starts']}, lost merges {counts['lost_merges']}")
    for row in report.lost_merges:
        lines.append(f"  lost merge {row['id']} #{row['issue']} ({row['policy']})")
    if report.skipped:
        lines.append(f"first {min(first_skipped, len(report.skipped))} skipped:")
        for row in report.skipped[:first_skipped]:
            lines.append(
                f"  {row['started_at']} {row['id']} #{row['issue']} {row['provider']} "
                f"{row['status']}: {row['reason']}"
            )
    return "\n".join(lines)
