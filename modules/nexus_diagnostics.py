"""One backend for "what happened to this work, and is it still alive?" (#188).

Diagnosing a Nexus failure used to mean assembling the answer by hand: probe
Git for a branch, guess which systemd unit the worker runs under, build a
``journalctl`` invocation, then correlate that against whichever of the four
id namespaces (mailbox task, experiment, controller session, issue/PR) the
operator happened to know. The #186/#187 investigation is the worked example:
Nexus created a branch and then produced no implementation commit and no PR,
and finding out *where* execution stopped took a chain of ad-hoc shell
commands composed through the mailbox.

This module is the missing first-class answer. It resolves any reference an
operator plausibly has to hand, reads the lifecycle state out of the
subsystem that owns it, and returns that together with a bounded slice of the
relevant logs - structured JSONL and, where it helps, the service journal.
Everything here is read-only by construction rather than by convention. The
database is opened in SQLite's ``mode=ro`` and further pinned with
``PRAGMA query_only``, so a write is refused by the connection itself rather
than merely absent from the code; nothing here creates the file, applies a
migration, runs a backfill, reaps or retries. That is what makes it safe to
hand to the mailbox/chat interface, and safe to point at a suspect task
without changing its state.

Extending the heartbeat contract
--------------------------------
#162 gave *controller sessions* an explicit heartbeat, which is what makes
"is this still running?" answerable rather than guessable. Mailbox tasks and
experiments never got one: they are driven through several processes and
phases, and no single owner is alive for the whole span, so there is nothing
to beat. Their liveness question is just as real, though - a task that stops
midway looks exactly like a slow one from the outside.

So the heartbeat contract is *extended* rather than duplicated. A subject's
liveness is derived from its **last progress signal**, which is the newest of:

* the heartbeat of a controller session claiming it, when one exists (the
  strongest signal - an explicit "I am alive"),
* the newest structured log entry written under any of its correlation ids
  (a witnessed side effect: weaker than a heartbeat, because a live worker
  can be quiet, but far stronger than nothing),
* the row's own ``updated_at`` (the weakest: "something changed then").

The staleness rule is then exactly the one :mod:`modules.heartbeat` already
defines, applied to that signal - including its clamped floor, so no caller
can pick a timeout that makes healthy work look dead. Which of the three
sources answered is reported alongside the verdict, because "interrupted, and
the last thing we saw was a log line 40 minutes ago" and "interrupted, and a
worker's heartbeat stopped 40 minutes ago" are different facts about how much
we know.

Classifying the outcome (#163)
------------------------------
On top of that progress verdict every subject carries an ``outcome``: what
actually happened, decided by :func:`classify_outcome` from the durable
terminal record (status, the claim's ``terminal_reason``, a recorded error or
restart marker) and the heartbeat, never from ``updated_at`` alone. It tells
a clean finish from work that simply went quiet, reports the last healthy
heartbeat and the last meaningful event, and says ``unknown`` - with the
missing evidence named - whenever the record is too thin or contradicts
itself. It also carries a retry *hint*; nothing here retries.

A task or experiment usually has no claim of its own, so the controller session
driving it speaks for it while it is still working: that session's heartbeat
and its durable reconciliation to ``abandoned`` are this work's liveness
evidence, however the session is linked. Once the subject itself has reached a
terminal state its own record decides; a session ending then is not the work
ending.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from modules import central_logging
from modules.heartbeat import (
    heartbeat_age_seconds,
    parse_timestamp,
    resolve_interval,
    resolve_stale_timeout,
)
from modules.redaction import redact, redact_credentials, sanitized_reason
from modules.run_store import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

SUBJECT_TASK = "task"
SUBJECT_EXPERIMENT = "experiment"
SUBJECT_SESSION = "session"

# Lifecycle vocabulary. Each subsystem keeps its own status values - they mean
# something precise there and renaming them would be a lie - and this is the
# small shared vocabulary they are *mapped onto*, so one caller can compare a
# mailbox task against an experiment against an adoption without learning
# three status tables.
LIFECYCLE_QUEUED = "queued"
LIFECYCLE_RUNNING = "running"
LIFECYCLE_WAITING = "waiting"
LIFECYCLE_NEEDS_REVIEW = "needs_review"
LIFECYCLE_SUCCEEDED = "succeeded"
LIFECYCLE_FAILED = "failed"
LIFECYCLE_REJECTED = "rejected"
LIFECYCLE_MERGED = "merged"
LIFECYCLE_ABANDONED = "abandoned"
LIFECYCLE_CANCELLED = "cancelled"
LIFECYCLE_UNKNOWN = "unknown"

# Lifecycles in which somebody is supposed to be driving the work. Only these
# can be *interrupted*; silence anywhere else is either correct or final.
WORKING_LIFECYCLES: frozenset[str] = frozenset({LIFECYCLE_QUEUED, LIFECYCLE_RUNNING})
# Lifecycles that have reached an outcome. Silence here is the expected state.
TERMINAL_LIFECYCLES: frozenset[str] = frozenset(
    {
        LIFECYCLE_SUCCEEDED,
        LIFECYCLE_FAILED,
        LIFECYCLE_REJECTED,
        LIFECYCLE_MERGED,
        LIFECYCLE_ABANDONED,
        LIFECYCLE_CANCELLED,
        LIFECYCLE_NEEDS_REVIEW,
    }
)
# Lifecycles that failed or gave up, in the order `last-failure` prefers them
# when two subjects are equally recent.
FAILED_LIFECYCLES: frozenset[str] = frozenset({LIFECYCLE_FAILED, LIFECYCLE_ABANDONED})

# Progress verdicts: the question "was this interrupted, is it still running,
# or did it finish?" that the failure-analysis routine needs answered before
# it can say anything useful about a stalled task.
PROGRESS_RUNNING = "running"
PROGRESS_INTERRUPTED = "interrupted"
PROGRESS_COMPLETED = "completed"
PROGRESS_NOT_STARTED = "not_started"
PROGRESS_UNKNOWN = "unknown"

# Where a last-progress timestamp came from, strongest first. Reported with
# the verdict because it says how much the verdict is worth.
PROGRESS_SOURCE_HEARTBEAT = "heartbeat"
PROGRESS_SOURCE_LOG = "log_entry"
PROGRESS_SOURCE_ROW = "updated_at"

_TASK_LIFECYCLE: dict[str, str] = {
    "pending_approval": LIFECYCLE_WAITING,
    "approved": LIFECYCLE_QUEUED,
    "submitted": LIFECYCLE_QUEUED,
    "running": LIFECYCLE_RUNNING,
    "nexus_deciding": LIFECYCLE_RUNNING,
    "needs_review": LIFECYCLE_NEEDS_REVIEW,
    "succeeded": LIFECYCLE_SUCCEEDED,
    "failed": LIFECYCLE_FAILED,
    "rejected": LIFECYCLE_REJECTED,
}

_EXPERIMENT_LIFECYCLE: dict[str, str] = {
    "queued": LIFECYCLE_QUEUED,
    "selecting": LIFECYCLE_RUNNING,
    "implementing": LIFECYCLE_RUNNING,
    "verifying": LIFECYCLE_RUNNING,
    "succeeded": LIFECYCLE_SUCCEEDED,
    "failed": LIFECYCLE_FAILED,
    "timed_out": LIFECYCLE_FAILED,
    "needs_review": LIFECYCLE_NEEDS_REVIEW,
    "rejected": LIFECYCLE_REJECTED,
    "cancelled": LIFECYCLE_CANCELLED,
    # Startup recovery's verdict on a run whose thread died with the process
    # (#281, #371): it went quiet without an outcome, like an abandoned claim.
    "interrupted": LIFECYCLE_ABANDONED,
}

_ADOPTION_LIFECYCLE: dict[str, str] = {
    "not_started": LIFECYCLE_QUEUED,
    "queued": LIFECYCLE_QUEUED,
    "adopting": LIFECYCLE_RUNNING,
    "adopted": LIFECYCLE_MERGED,
    "needs_review": LIFECYCLE_NEEDS_REVIEW,
    "failed": LIFECYCLE_FAILED,
    "interrupted": LIFECYCLE_ABANDONED,
}

_SESSION_LIFECYCLE: dict[str, str] = {
    "queued": LIFECYCLE_QUEUED,
    "active": LIFECYCLE_RUNNING,
    "waiting_for_llm": LIFECYCLE_RUNNING,
    "waiting_for_tool_resolver": LIFECYCLE_RUNNING,
    "waiting_for_knowledge": LIFECYCLE_RUNNING,
    "waiting_for_user": LIFECYCLE_WAITING,
    "blocked_missing_capability": LIFECYCLE_WAITING,
    "waiting_for_experiment": LIFECYCLE_WAITING,
    # The turn ended on purpose with a decision and released its claim; what
    # the decision then starts is diagnosed on its own subject.
    "action_selected": LIFECYCLE_SUCCEEDED,
    "abandoned": LIFECYCLE_ABANDONED,
    "failed": LIFECYCLE_FAILED,
    "succeeded": LIFECYCLE_SUCCEEDED,
    "done": LIFECYCLE_SUCCEEDED,
}

DEFAULT_LOG_LINES = 50
MAX_LOG_LINES = 500
DEFAULT_SERVICE_LINES = 100
# Bounded by default, with an explicit way to ask for more: an unbounded
# diagnostic run from chat is how a "quick look" becomes a context overflow.
DEFAULT_LAST_FAILURE_CANDIDATES = 25
MAX_LAST_FAILURE_CANDIDATES = 200
_MAX_MESSAGE_CHARS = 500
_MAX_ERROR_CHARS = 1000

# The systemd units a Nexus deployment runs. Naming them here is what lets a
# caller say `logs service mailbox` instead of knowing unit names; anything
# not in this map is still accepted verbatim but validated as a unit name, so
# no caller can smuggle a shell fragment into journalctl.
SERVICE_ALIASES: dict[str, str] = {
    "api": "agent-api",
    "agent-api": "agent-api",
    "nexus": "agent-api",
    "mailbox": "nexus-mailbox",
    "nexus-mailbox": "nexus-mailbox",
    "llama": "llama-server",
    "llm": "llama-server",
    "local-model": "llama-server",
    "llama-server": "llama-server",
}
KNOWN_SERVICES: tuple[str, ...] = ("agent-api", "nexus-mailbox", "llama-server")

# The id namespaces a bare reference is resolved against, in the order an
# exact match is preferred. Prefix matches are gathered from all of them
# before any verdict is reached (see NexusDiagnostics.resolve).
_RESOLUTION_TABLES: tuple[tuple[str, str], ...] = (
    (SUBJECT_TASK, "mailbox_task_approvals"),
    (SUBJECT_EXPERIMENT, "experiments"),
    (SUBJECT_SESSION, "nexus_task_sessions"),
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]{1,128}$")
_ISSUE_REF_RE = re.compile(r"^(?:issue[ _-]*)?#?(\d{1,9})$", re.IGNORECASE)
_PR_REF_RE = re.compile(r"^(?:pr|pull|pull[ _-]?request)[ _-]*#?(\d{1,9})$", re.IGNORECASE)


class DiagnosticsError(ValueError):
    """A reference could not be resolved, or an argument was unusable."""


def _connect_read_only(db_path: str) -> sqlite3.Connection:
    """Open an existing Nexus database without being able to change it.

    Deliberately *not* ``run_store.init_db``. That helper is the write path's
    entry point: it creates the file and its parent directory, applies the
    full schema and every additive migration, and runs the mailbox-approval
    backfill - all of which are writes. Calling it here would have made a
    diagnostic read mutate the very database it was asked to describe, which
    is exactly what a read-only command must not do, and would have done it on
    the databases most worth inspecting: the incomplete ones.

    So the connection is opened in SQLite's ``mode=ro`` and pinned with
    ``PRAGMA query_only``. The two are independent: the first refuses to open
    the file for writing at all, the second refuses write statements on the
    connection even if it were writable. Nothing is created, so a missing
    database is reported rather than conjured, and a schema this build has
    never seen is simply read as far as it goes - ``_fetchall`` already treats
    a missing table or column as "nothing known about that".
    """
    path = Path(db_path)
    if not path.is_file():
        raise DiagnosticsError(f"no Nexus database at {path}")
    # as_uri() percent-encodes, so a path containing '?' or '#' cannot be
    # misread as the start of the URI's query fragment.
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as exc:
        raise DiagnosticsError(f"cannot read the Nexus database at {path}: {exc}") from exc
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _like_escape(value: str) -> str:
    """Escape a literal for use inside a ``LIKE`` pattern.

    ``_`` is both a legal id/path character and a single-character wildcard,
    and a shorthand that silently matched any character in that position would
    answer for the wrong task - the one failure mode a diagnostic cannot
    afford. Paired with ``ESCAPE '\\'`` at every call site.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_prefix(value: str) -> str:
    return _like_escape(value) + "%"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _text(value: Any, limit: int = _MAX_MESSAGE_CHARS) -> str | None:
    """Redact and bound one free-text field on the way out."""
    if value is None:
        return None
    rendered = redact(str(value)).strip()
    if not rendered:
        return None
    return rendered[:limit]


def _newest(*timestamps: Any) -> tuple[str | None, int]:
    """Return the newest parsable timestamp and which argument it came from.

    Ordering is by parsed instant, not string compare: the three sources use
    three different ISO spellings (``...+00:00``, ``...123Z``, second
    precision), and comparing those as strings gets the order wrong exactly
    when it matters - between a heartbeat and a log line seconds apart.
    """
    best: datetime | None = None
    best_index = -1
    best_raw: str | None = None
    for index, value in enumerate(timestamps):
        parsed = parse_timestamp(value)
        if parsed is None:
            continue
        if best is None or parsed > best:
            best, best_index, best_raw = parsed, index, str(value)
    return best_raw, best_index


@dataclass(slots=True)
class ProgressVerdict:
    """Is this still running, was it interrupted, or did it finish? (#188)

    ``state`` is the answer; ``source`` says which signal produced
    ``last_progress_at`` (see the module docstring), and ``reason`` is the
    one-line explanation an operator or a failure analysis can quote.
    """

    state: str
    reason: str
    last_progress_at: str | None = None
    source: str | None = None
    age_seconds: float | None = None
    stale_after_seconds: int = 0
    heartbeat_interval_seconds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LifecycleState:
    """Everything about a unit of work that is not a log line.

    The field list is the issue's checklist: lifecycle state, branch, commit,
    PR, the worker/service responsible, the last progress timestamp and the
    last error - all under stable names, so a caller never has to know which
    subsystem's table an answer came out of.
    """

    subject_type: str
    subject_id: str
    lifecycle: str
    status: str | None = None
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    ended_at: str | None = None
    branch: str | None = None
    commit: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    worker_id: str | None = None
    service: str | None = None
    last_error: str | None = None
    progress: ProgressVerdict | None = None
    outcome: "OutcomeDiagnosis | None" = None
    correlation_ids: dict[str, str] = field(default_factory=dict)
    related: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["progress"] = self.progress.to_dict() if self.progress else None
        payload["outcome"] = self.outcome.to_dict() if self.outcome else None
        return payload


@dataclass(slots=True)
class DiagnosticReport:
    """One resolved subject: its lifecycle plus the logs that explain it."""

    generated_at: str
    reference: str
    lifecycle: LifecycleState
    log_entries: list[dict[str, Any]] = field(default_factory=list)
    log_sources: list[str] = field(default_factory=list)
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "reference": self.reference,
            "lifecycle": self.lifecycle.to_dict(),
            "log_entries": list(self.log_entries),
            "log_sources": list(self.log_sources),
            "truncated": self.truncated,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class ServiceLogs:
    """A bounded journal slice for one service, without ad-hoc journalctl."""

    generated_at: str
    service: str
    requested: str
    lines: int
    since: str | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess_progress(
    lifecycle: str,
    *,
    last_heartbeat_at: Any = None,
    last_log_at: Any = None,
    updated_at: Any = None,
    stale_after_seconds: object = None,
    now: datetime | None = None,
) -> ProgressVerdict:
    """Decide running / interrupted / completed from the strongest signal.

    This is the heartbeat contract of #162 applied to subjects that have no
    heartbeat of their own (see the module docstring). The timeout is resolved
    through :func:`modules.heartbeat.resolve_stale_timeout`, so it inherits the
    same clamped floor: a caller cannot pass a value that declares live work
    dead, which matters more here than anywhere else because this verdict is
    what a failure analysis will act on.
    """
    timeout = resolve_stale_timeout(stale_after_seconds)
    interval = resolve_interval()
    reference = now or _now()
    last_progress_at, index = _newest(last_heartbeat_at, last_log_at, updated_at)
    source = (
        (PROGRESS_SOURCE_HEARTBEAT, PROGRESS_SOURCE_LOG, PROGRESS_SOURCE_ROW)[index]
        if index >= 0
        else None
    )
    age = heartbeat_age_seconds(last_progress_at, now=reference)

    def verdict(state: str, reason: str) -> ProgressVerdict:
        return ProgressVerdict(
            state=state,
            reason=reason,
            last_progress_at=last_progress_at,
            source=source,
            age_seconds=age,
            stale_after_seconds=timeout,
            heartbeat_interval_seconds=interval,
        )

    if lifecycle in TERMINAL_LIFECYCLES:
        return verdict(
            PROGRESS_COMPLETED,
            f"reached the terminal lifecycle state '{lifecycle}'",
        )
    if lifecycle == LIFECYCLE_WAITING:
        # A resting state has no worker by design, so its silence is correct
        # and must never be reported as an interruption (#162).
        return verdict(PROGRESS_RUNNING, "resting: waiting by design, not stalled")
    if lifecycle not in WORKING_LIFECYCLES:
        return verdict(PROGRESS_UNKNOWN, f"no progress rule for lifecycle '{lifecycle}'")
    if age is None:
        return verdict(
            PROGRESS_NOT_STARTED if lifecycle == LIFECYCLE_QUEUED else PROGRESS_UNKNOWN,
            "no heartbeat, log entry or row update has ever been recorded",
        )
    if age > timeout:
        return verdict(
            PROGRESS_INTERRUPTED,
            f"last progress ({source}) was {int(age)}s ago, past the {timeout}s "
            f"stale timeout, while still in lifecycle '{lifecycle}'",
        )
    return verdict(
        PROGRESS_RUNNING,
        f"last progress ({source}) was {int(age)}s ago, within the {timeout}s "
        "stale timeout",
    )


# ---------------------------------------------------------------------------
# Outcome classification (#163)
#
# ``assess_progress`` answers "is anything still moving?". The question a
# failure diagnosis actually needs answered is sharper: *what happened* - did
# this finish cleanly, fail on purpose, get cancelled, or did its worker just
# disappear? Those differ in what the evidence must show, so they are decided
# here from the durable signals #162 provides (the terminal status, the claim's
# ``terminal_reason``, the heartbeat) rather than from whichever timestamp was
# newest. ``updated_at`` in particular never counts as proof of life.
#
# The vocabulary is deliberately small:
#
# * ``running``      - working, and a heartbeat (or, weaker, a log entry) is
#                      within the stale timeout,
# * ``waiting``      - resting by design (approval, user reply, an experiment
#                      it started), or queued and not yet picked up,
# * ``succeeded``    - reached a successful terminal state,
# * ``failed``       - reached a failed terminal state with an explicit record,
# * ``cancelled``    - stopped on purpose (cancelled, rejected),
# * ``needs_review`` - ended by handing the work to a human,
# * ``stale``        - the worker stopped reporting itself alive without
#                      recording an outcome,
# * ``interrupted``  - a service/process restart is on record as having ended it,
# * ``unknown``      - evidence missing or contradictory; never read as success.
# ---------------------------------------------------------------------------

OUTCOME_RUNNING = "running"
OUTCOME_WAITING = "waiting"
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_NEEDS_REVIEW = "needs_review"
OUTCOME_STALE = "stale"
OUTCOME_INTERRUPTED = "interrupted"
OUTCOME_UNKNOWN = "unknown"
OUTCOMES: tuple[str, ...] = (
    OUTCOME_RUNNING,
    OUTCOME_WAITING,
    OUTCOME_SUCCEEDED,
    OUTCOME_FAILED,
    OUTCOME_CANCELLED,
    OUTCOME_NEEDS_REVIEW,
    OUTCOME_STALE,
    OUTCOME_INTERRUPTED,
    OUTCOME_UNKNOWN,
)
# Outcomes `last-failure` reports: the work ended badly or went quiet.
FAILED_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_FAILED, OUTCOME_STALE, OUTCOME_INTERRUPTED}
)

# Whether retrying is worth considering. A hint for a human or a later
# recovery policy, not a decision: nothing here retries.
RETRY_NOT_NEEDED = "not_needed"
RETRY_POSSIBLE = "possible"
RETRY_NOT_RECOMMENDED = "not_recommended"
RETRY_INSUFFICIENT_EVIDENCE = "insufficient_evidence"

# Which signal an outcome rests on, strongest first.
EVIDENCE_TERMINAL_STATE = "terminal_state"
EVIDENCE_HEARTBEAT = PROGRESS_SOURCE_HEARTBEAT
EVIDENCE_LOG = PROGRESS_SOURCE_LOG
EVIDENCE_ROW = PROGRESS_SOURCE_ROW
EVIDENCE_NONE = "none"

# The #162 terminal reasons, named here rather than imported so this module
# keeps depending only on the subsystem-agnostic heartbeat contract.
_REASON_HEARTBEAT_TIMEOUT = "heartbeat_timeout"
_REASON_TURN_FAILED = "turn_failed"
_REASON_TURN_CANCELLED = "turn_cancelled"
_STATUS_ABANDONED = "abandoned"
_FAILURE_REASONS = frozenset({_REASON_HEARTBEAT_TIMEOUT, _REASON_TURN_FAILED})
# What startup recovery writes on an experiment its restart orphaned (#281).
_RESTART_ERROR_MARKER = "interrupted by service restart"
# Log events that only say "still alive" - liveness evidence, but not the
# "last meaningful step" an operator is asking about.
_LIVENESS_LOG_EVENTS = frozenset({"test_progress", "heartbeat"})
# Journal entries a reaper writes *about* a session. They record its end, not
# what its worker was doing when it stopped.
_REAPER_SESSION_EVENTS = frozenset({_REASON_HEARTBEAT_TIMEOUT, _STATUS_ABANDONED})


@dataclass(slots=True)
class OutcomeDiagnosis:
    """What happened to this work, and how sure we are (#163).

    ``outcome`` is one of :data:`OUTCOMES`. ``conclusive`` is False whenever
    the verdict rests on weaker evidence than it needs (no heartbeat, only a
    log line); ``evidence_gaps`` then names what was missing, so "we do not
    know" is a stated finding instead of a silent success.
    """

    outcome: str
    conclusive: bool
    explanation: str
    evidence: str = EVIDENCE_NONE
    probable_cause: str | None = None
    terminal_status: str | None = None
    terminal_reason: str | None = None
    last_healthy_heartbeat_at: str | None = None
    last_event: dict[str, Any] | None = None
    retry: str = RETRY_INSUFFICIENT_EVIDENCE
    evidence_gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_liveness_log_event(entry: dict[str, Any]) -> bool:
    """A log entry that only reports liveness, not a step of the work."""
    event = str(entry.get("event") or "").strip().lower()
    return event in _LIVENESS_LOG_EVENTS or "heartbeat" in event


def classify_outcome(
    lifecycle: str,
    *,
    status: str | None = None,
    terminal_reason: str | None = None,
    error: str | None = None,
    last_heartbeat_at: Any = None,
    recorded_heartbeat_at: Any = None,
    own_claim: bool = False,
    driver_status: str | None = None,
    driver_terminal_reason: str | None = None,
    driver_ended_at: Any = None,
    last_log_at: Any = None,
    last_event: dict[str, Any] | None = None,
    updated_at: Any = None,
    restart_evidence: str | None = None,
    stale_after_seconds: object = None,
    now: datetime | None = None,
) -> OutcomeDiagnosis:
    """Classify one execution from its durable terminal state and liveness.

    Inputs are the facts the stores already hold, so the rule is testable
    without a database:

    * ``lifecycle``/``status``/``terminal_reason``/``error`` - the durable
      terminal record (``terminal_reason`` is the #162 claim release reason),
    * ``last_heartbeat_at`` - the live heartbeat column of the claim, if any;
      ``own_claim`` says the claim belongs to this subject itself rather than
      to a controller session that merely drives it,
    * ``recorded_heartbeat_at`` - the heartbeat a reconciliation preserved in
      its event after clearing the column,
    * ``driver_status``/``driver_terminal_reason``/``driver_ended_at`` - how the
      controller session driving this work ended, when ``own_claim`` is False.
      They only speak for work that is still working: a driver that was
      reconciled as abandoned is durable evidence its worker is gone,
    * ``last_log_at`` - the newest structured log entry (liveness, weaker),
    * ``last_event`` - the newest *meaningful* event, reported as-is,
    * ``restart_evidence`` - an explicit restart marker found elsewhere (e.g.
      an adoption interrupted by a restart).

    The stale timeout goes through :func:`resolve_stale_timeout`, so it has the
    same clamped floor as every other liveness verdict.
    """
    timeout = resolve_stale_timeout(stale_after_seconds)
    reference = now or _now()
    status_text = (status or "").strip() or None
    reason = (terminal_reason or "").strip() or None
    error_text = (error or "").strip() or None
    driver_reason = (driver_terminal_reason or "").strip() or None
    heartbeat_age = heartbeat_age_seconds(last_heartbeat_at, now=reference)
    log_age = heartbeat_age_seconds(last_log_at, now=reference)
    last_healthy = (
        str(last_heartbeat_at) if parse_timestamp(last_heartbeat_at) else None
    ) or (str(recorded_heartbeat_at) if parse_timestamp(recorded_heartbeat_at) else None)
    gaps: list[str] = []

    def result(
        outcome: str,
        explanation: str,
        *,
        evidence: str,
        retry: str,
        conclusive: bool = True,
        cause: str | None = None,
    ) -> OutcomeDiagnosis:
        if not conclusive and retry != RETRY_NOT_NEEDED:
            retry = RETRY_INSUFFICIENT_EVIDENCE
        return OutcomeDiagnosis(
            outcome=outcome,
            conclusive=conclusive,
            explanation=explanation,
            evidence=evidence,
            probable_cause=_text(cause, _MAX_ERROR_CHARS) if cause else None,
            terminal_status=status_text,
            terminal_reason=reason,
            last_healthy_heartbeat_at=last_healthy,
            last_event=dict(last_event) if last_event else None,
            retry=retry,
            evidence_gaps=list(gaps),
        )

    def where() -> str:
        if not last_event:
            return ""
        step = last_event.get("phase") or last_event.get("kind") or last_event.get("event")
        return f" (last step: {step})" if step else ""

    def active_since(moment: Any) -> bool:
        """Is this work still logging, later than *moment*, within the timeout?"""
        since = parse_timestamp(moment)
        activity = parse_timestamp(last_log_at)
        return (
            not own_claim
            and since is not None
            and activity is not None
            and activity > since
            and log_age is not None
            and log_age <= timeout
        )

    def contradicted_by_logs(driver_state: str) -> OutcomeDiagnosis:
        gaps.append("structured logs continue after the controller session stopped")
        return result(
            OUTCOME_UNKNOWN,
            f"contradictory record: the driving session {driver_state}, but this "
            f"work logged {int(log_age or 0)}s ago, later than that",
            evidence=EVIDENCE_NONE,
            retry=RETRY_INSUFFICIENT_EVIDENCE,
            conclusive=False,
        )

    # -- inconsistent records: say so rather than pick a side --------------
    if lifecycle == LIFECYCLE_UNKNOWN:
        gaps.append(f"status {status_text or '<none>'!r} has no known meaning")
        return result(
            OUTCOME_UNKNOWN,
            f"unrecognised status {status_text or '<none>'!r}; cannot classify",
            evidence=EVIDENCE_NONE,
            retry=RETRY_INSUFFICIENT_EVIDENCE,
            conclusive=False,
        )
    if own_claim and lifecycle in TERMINAL_LIFECYCLES and heartbeat_age is not None:
        # Every terminal write clears the heartbeat (#162); one that is still
        # there means two writers disagree about whether this work is over.
        gaps.append("terminal status recorded while the claim still carries a heartbeat")
        return result(
            OUTCOME_UNKNOWN,
            f"contradictory record: terminal status {status_text!r} but the claim "
            f"still heartbeats (last {int(heartbeat_age)}s ago)",
            evidence=EVIDENCE_NONE,
            retry=RETRY_INSUFFICIENT_EVIDENCE,
            conclusive=False,
        )
    if own_claim and lifecycle in WORKING_LIFECYCLES and reason and heartbeat_age is None:
        gaps.append("claim released with a terminal reason but status still working")
        return result(
            OUTCOME_UNKNOWN,
            f"contradictory record: status {status_text!r} is still working but the "
            f"claim was released with terminal reason {reason!r}",
            evidence=EVIDENCE_NONE,
            retry=RETRY_INSUFFICIENT_EVIDENCE,
            conclusive=False,
        )
    if lifecycle in (LIFECYCLE_SUCCEEDED, LIFECYCLE_MERGED) and reason in _FAILURE_REASONS:
        gaps.append(f"success status contradicts terminal reason {reason!r}")
        return result(
            OUTCOME_UNKNOWN,
            f"contradictory record: status {status_text!r} is successful but the "
            f"claim ended with {reason!r}",
            evidence=EVIDENCE_NONE,
            retry=RETRY_INSUFFICIENT_EVIDENCE,
            conclusive=False,
        )

    # -- explicit terminal outcomes ----------------------------------------
    restart = restart_evidence or (
        error_text if error_text and _RESTART_ERROR_MARKER in error_text.lower() else None
    )
    if status_text == "interrupted" and not restart:
        restart = "startup recovery marked the run interrupted"
    if restart and lifecycle in TERMINAL_LIFECYCLES and lifecycle not in (
        LIFECYCLE_SUCCEEDED,
        LIFECYCLE_MERGED,
    ):
        return result(
            OUTCOME_INTERRUPTED,
            "ended by a service/process restart while it was still running" + where(),
            evidence=EVIDENCE_TERMINAL_STATE,
            retry=RETRY_POSSIBLE,
            cause=restart,
        )
    if (
        lifecycle in (LIFECYCLE_CANCELLED, LIFECYCLE_REJECTED)
        or reason == _REASON_TURN_CANCELLED
    ):
        return result(
            OUTCOME_CANCELLED,
            f"stopped intentionally (status {status_text!r}"
            + (f", reason {reason!r}" if reason else "")
            + ")",
            evidence=EVIDENCE_TERMINAL_STATE,
            retry=RETRY_NOT_RECOMMENDED,
            cause=error_text,
        )
    if lifecycle == LIFECYCLE_ABANDONED:
        if reason == _REASON_HEARTBEAT_TIMEOUT:
            return result(
                OUTCOME_STALE,
                "the worker stopped sending heartbeats without recording an outcome; "
                "the claim was reconciled as abandoned"
                + (f" (last heartbeat {last_healthy})" if last_healthy else ""),
                evidence=EVIDENCE_HEARTBEAT,
                retry=RETRY_POSSIBLE,
                # Crash, kill or restart all look the same from here; naming
                # one would be inventing it.
                cause="worker disappeared (crash, kill or restart - not distinguishable "
                "from the record)",
            )
        gaps.append(
            "marked abandoned without a heartbeat timeout; the verdict came from "
            "row inactivity, not from a heartbeat"
        )
        return result(
            OUTCOME_STALE,
            "marked abandoned after a long period without activity" + where(),
            evidence=EVIDENCE_ROW,
            retry=RETRY_POSSIBLE,
            conclusive=False,
            cause=error_text,
        )
    if lifecycle == LIFECYCLE_FAILED:
        cause = error_text or (reason if reason and reason != _REASON_TURN_FAILED else None)
        if not cause and last_event and reason == _REASON_TURN_FAILED:
            cause = last_event.get("message")
        if not cause:
            gaps.append("failed status recorded without an error message")
        return result(
            OUTCOME_FAILED,
            f"failed explicitly (status {status_text!r}"
            + (f", reason {reason!r}" if reason else "")
            + ")"
            + where(),
            evidence=EVIDENCE_TERMINAL_STATE,
            retry=RETRY_POSSIBLE,
            cause=cause,
        )
    if lifecycle in (LIFECYCLE_SUCCEEDED, LIFECYCLE_MERGED):
        return result(
            OUTCOME_SUCCEEDED,
            f"finished successfully (status {status_text!r})",
            evidence=EVIDENCE_TERMINAL_STATE,
            retry=RETRY_NOT_NEEDED,
        )
    if lifecycle == LIFECYCLE_NEEDS_REVIEW:
        return result(
            OUTCOME_NEEDS_REVIEW,
            "ended by handing the work to a human for review" + where(),
            evidence=EVIDENCE_TERMINAL_STATE,
            retry=RETRY_NOT_RECOMMENDED,
            cause=error_text,
        )
    if lifecycle == LIFECYCLE_WAITING:
        return result(
            OUTCOME_WAITING,
            f"resting by design (status {status_text!r}); silence is expected",
            evidence=EVIDENCE_TERMINAL_STATE,
            retry=RETRY_NOT_NEEDED,
        )

    # -- still working: only liveness can tell healthy from abandoned ------
    if (
        not own_claim
        and heartbeat_age is None
        and (driver_reason == _REASON_HEARTBEAT_TIMEOUT or driver_status == _STATUS_ABANDONED)
    ):
        if active_since(driver_ended_at):
            return contradicted_by_logs("was reconciled as abandoned")
        reason = driver_reason
        if driver_reason == _REASON_HEARTBEAT_TIMEOUT:
            return result(
                OUTCOME_STALE,
                f"still {status_text!r}, but the controller session driving it stopped "
                "sending heartbeats and was reconciled as abandoned"
                + (f" (last heartbeat {last_healthy})" if last_healthy else "")
                + where(),
                evidence=EVIDENCE_HEARTBEAT,
                retry=RETRY_POSSIBLE,
                cause="worker disappeared (crash, kill or restart - not distinguishable "
                "from the record)",
            )
        gaps.append(
            "driving session marked abandoned without a heartbeat timeout; the "
            "verdict came from row inactivity, not from a heartbeat"
        )
        return result(
            OUTCOME_STALE,
            f"still {status_text!r}, but the controller session driving it was marked "
            "abandoned after a long period without activity" + where(),
            evidence=EVIDENCE_ROW,
            retry=RETRY_POSSIBLE,
            conclusive=False,
        )
    if heartbeat_age is not None:
        if heartbeat_age <= timeout:
            return result(
                OUTCOME_RUNNING,
                f"alive: last heartbeat {int(heartbeat_age)}s ago, within the "
                f"{timeout}s stale timeout",
                evidence=EVIDENCE_HEARTBEAT,
                retry=RETRY_NOT_NEEDED,
            )
        if active_since(last_heartbeat_at):
            return contradicted_by_logs(f"stopped heartbeating {int(heartbeat_age)}s ago")
        return result(
            OUTCOME_STALE,
            f"heartbeats stopped {int(heartbeat_age)}s ago (timeout {timeout}s) while "
            f"still {status_text!r}; not yet reconciled" + where(),
            evidence=EVIDENCE_HEARTBEAT,
            retry=RETRY_POSSIBLE,
            cause="worker stopped heartbeating without recording an outcome",
        )
    gaps.append("no heartbeat: this work is not covered by a live claim")
    if log_age is not None:
        if log_age <= timeout:
            return result(
                OUTCOME_RUNNING,
                f"probably alive: last log entry {int(log_age)}s ago, within the "
                f"{timeout}s stale timeout",
                evidence=EVIDENCE_LOG,
                retry=RETRY_NOT_NEEDED,
                conclusive=False,
            )
        return result(
            OUTCOME_STALE,
            f"no log activity for {int(log_age)}s (timeout {timeout}s) while still "
            f"{status_text!r}" + where(),
            evidence=EVIDENCE_LOG,
            retry=RETRY_POSSIBLE,
            conclusive=False,
        )
    gaps.append("no structured log entry")
    row_age = heartbeat_age_seconds(updated_at, now=reference)
    if lifecycle == LIFECYCLE_QUEUED and row_age is not None and row_age <= timeout:
        return result(
            OUTCOME_WAITING,
            f"queued {int(row_age)}s ago and not picked up yet",
            evidence=EVIDENCE_ROW,
            retry=RETRY_NOT_NEEDED,
            conclusive=False,
        )
    gaps.append("updated_at is not proof of life")
    return result(
        OUTCOME_UNKNOWN,
        f"still {status_text!r} with no heartbeat or log entry; cannot tell whether "
        "it is running or abandoned",
        evidence=EVIDENCE_ROW if row_age is not None else EVIDENCE_NONE,
        retry=RETRY_INSUFFICIENT_EVIDENCE,
        conclusive=False,
    )


def resolve_service(name: str | None) -> str:
    """Map a friendly service name onto a systemd unit, safely.

    An unknown name is not rejected - a deployment may run units this module
    has never heard of - but it is validated against a unit-name pattern
    before it can reach ``journalctl``.
    """
    candidate = (name or "").strip()
    if not candidate:
        return KNOWN_SERVICES[0]
    alias = SERVICE_ALIASES.get(candidate.lower())
    if alias:
        return alias
    stripped = candidate[:-8] if candidate.endswith(".service") else candidate
    if not _SAFE_UNIT_RE.fullmatch(stripped):
        raise DiagnosticsError(f"unusable service name: {candidate[:64]!r}")
    return stripped


def _bound_int(
    value: object, *, default: int, maximum: int, name: str = "lines"
) -> int:
    """Clamp a caller-supplied bound, refusing only what is not a number.

    Out-of-range is clamped rather than rejected: a caller asking for more
    history than the ceiling allows wants as much as it can have, and a 400
    there would be pedantry. A non-number is a different thing - it means the
    caller believes it asked for something this does not understand - so it is
    reported.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise DiagnosticsError(f"{name} must be a whole number")
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        raise DiagnosticsError(f"{name} must be a whole number") from None
    return max(1, min(resolved, maximum))


def _bound_lines(value: object, *, default: int, maximum: int = MAX_LOG_LINES) -> int:
    return _bound_int(value, default=default, maximum=maximum)


def _compact(entry: dict[str, Any]) -> dict[str, Any]:
    """Strip a log entry down to what a diagnosis reads, redacted and bounded."""
    return {
        "timestamp": str(entry.get("timestamp") or ""),
        "level": str(entry.get("level") or ""),
        "phase": str(entry.get("phase") or ""),
        "event": str(entry.get("event") or ""),
        "message": _text(entry.get("message")) or "",
        "source": str(entry.get("_source") or entry.get("source") or ""),
        **{
            key: str(entry[key])
            for key in ("experiment_id", "mailbox_task_id", "session_id", "job_id")
            if isinstance(entry.get(key), str) and entry.get(key)
        },
    }


class NexusDiagnostics:
    """Read-only diagnostic backend shared by the API, operations and analysis.

    One instance owns one SQLite connection, in the same
    open-per-request/close style the other diagnostic surfaces use. Every
    statement it issues is a SELECT; there is deliberately no code path here
    that writes, so exposing it through the mailbox cannot change state.
    """

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        log_dir: str | Path | None = None,
        log_reader: Any = None,
        stale_after_seconds: object = None,
        now: Any = None,
    ) -> None:
        self._conn = _connect_read_only(db_path)
        self.db_path = db_path
        self.log_dir = Path(log_dir or central_logging.resolve_log_dir())
        # Public and rebindable: the API injects the process's shared
        # LogReader per request, and a test injects a fake, so neither has
        # to reach past a private name.
        self.log_reader = log_reader
        self.stale_after_seconds = resolve_stale_timeout(stale_after_seconds)
        self._now = now or _now

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "NexusDiagnostics":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Reference resolution
    # ------------------------------------------------------------------

    def resolve(self, reference: str) -> tuple[str, str]:
        """Turn whatever the operator has to hand into ``(subject_type, id)``.

        Resolution is ordered by how specific a match is, not by how likely
        the caller is to have used that namespace: an exact id in any table
        beats a prefix in any other, so a full id is never shadowed by some
        other row that happens to start with the same characters. A prefix
        must then be unique across *every* namespace, not merely within the
        first one that matches - otherwise the order of this list, rather than
        the operator's input, would be deciding the answer. Issue and PR
        references resolve through the GitHub work links, which is the only
        place that mapping is durably recorded.
        """
        candidate = (reference or "").strip()
        if not candidate:
            raise DiagnosticsError("a reference is required")

        pr_match = _PR_REF_RE.fullmatch(candidate)
        if pr_match:
            resolved = self._resolve_github_number("pr_number", int(pr_match.group(1)))
            if resolved:
                return resolved
            raise DiagnosticsError(f"no Nexus work is linked to PR #{pr_match.group(1)}")

        issue_match = _ISSUE_REF_RE.fullmatch(candidate)
        if issue_match:
            resolved = self._resolve_github_number("issue_number", int(issue_match.group(1)))
            if resolved:
                return resolved
            if not candidate.isdigit():
                # "#188" / "issue 188" is a promise that this *is* an issue, so
                # failing to find it is the answer. A bare "188" is only a
                # guess, and might equally be the start of an id, so it falls
                # through to id resolution below.
                raise DiagnosticsError(
                    f"no Nexus work is linked to issue #{issue_match.group(1)}"
                )

        if not _SAFE_ID_RE.fullmatch(candidate):
            # Not an id at all - most likely an inbox filename, which carries
            # its own derived task id.
            resolved = self._resolve_inbox_filename(candidate)
            if resolved:
                return resolved
            raise DiagnosticsError(f"unusable reference: {candidate[:64]!r}")

        for subject_type, table in _RESOLUTION_TABLES:
            if self._exists(table, candidate):
                return subject_type, candidate

        # Prefix uniqueness is judged across *all* namespaces at once, not one
        # table at a time. Checking per table and returning the first hit would
        # have let namespace order silently decide between a mailbox task and
        # an experiment that share a prefix - which is precisely the guess the
        # contract promises not to make.
        matches: list[tuple[str, str]] = []
        for subject_type, table in _RESOLUTION_TABLES:
            matches.extend(
                (subject_type, found) for found in self._prefix_matches(table, candidate)
            )
            if len(matches) > 1:
                break
        if len(matches) == 1:
            return matches[0]

        # An inbox filename can look like a safe id (dots and dashes are legal
        # in both), so it is tried before either verdict below: a reference
        # that names a real message is answerable even if it also happens to
        # prefix something.
        resolved = self._resolve_inbox_filename(candidate)
        if resolved:
            return resolved
        if matches:
            kinds = ", ".join(sorted({subject_type for subject_type, _ in matches}))
            raise DiagnosticsError(
                f"{candidate[:64]!r} is an ambiguous prefix: it matches more than "
                f"one subject ({kinds}). Use more of the id."
            )
        raise DiagnosticsError(f"no task, experiment or session matches {candidate[:64]!r}")

    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """SELECT, treating a table that does not exist yet as "no rows".

        Several of the tables joined here are created lazily by the service
        that owns them - the controller store, the GitHub work tracker - so a
        deployment that has not yet run one of them simply has no such table.
        A diagnostic must degrade to "nothing known about that" there, not
        fail: the databases most worth inspecting are the incomplete ones.
        A column missing for the same reason (an additive migration a caller's
        database has not run) is treated the same way. Any other SQL error is
        a bug here and stays loud.
        """
        try:
            return list(self._conn.execute(sql, tuple(params)).fetchall())
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or "no such column" in str(exc):
                logger.debug("diagnostics: skipping unavailable source (%s)", exc)
                return []
            raise

    def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self._fetchall(sql, params)
        return rows[0] if rows else None

    def _exists(self, table: str, identifier: str) -> bool:
        return (
            self._fetchone(
                f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", (identifier,)  # noqa: S608
            )
            is not None
        )

    def _prefix_matches(self, table: str, identifier: str) -> list[str]:
        """Ids in *table* starting with *identifier*, capped at two.

        Two is all any caller needs: one match is a resolution, and two is
        already "ambiguous" - counting the rest would only cost a bigger scan
        to reach the same verdict. The caller sums these across namespaces,
        because ambiguity is a property of the whole id space, not of one
        table.
        """
        rows = self._fetchall(
            f"SELECT id FROM {table} WHERE id LIKE ? ESCAPE '\\' ORDER BY id LIMIT 2",  # noqa: S608
            (_like_prefix(identifier),),
        )
        return [str(row["id"]) for row in rows]

    def _resolve_github_number(self, column: str, number: int) -> tuple[str, str] | None:
        row = self._fetchone(
            f"""SELECT source_type, source_id, experiment_id
                  FROM github_work_links
                 WHERE {column} = ?
              ORDER BY updated_at DESC LIMIT 1""",  # noqa: S608
            (number,),
        )
        if row is None:
            return None
        experiment_id = row["experiment_id"]
        if experiment_id and self._exists("experiments", str(experiment_id)):
            return SUBJECT_EXPERIMENT, str(experiment_id)
        source_id = row["source_id"]
        if source_id and self._exists("mailbox_task_approvals", str(source_id)):
            return SUBJECT_TASK, str(source_id)
        if experiment_id:
            return SUBJECT_EXPERIMENT, str(experiment_id)
        return None

    def _resolve_inbox_filename(self, reference: str) -> tuple[str, str] | None:
        """An inbox message names its own task, via the same digest the
        mailbox uses - so an operator who only has the filename still gets in.
        """
        try:
            from modules.log_trace import mailbox_task_ids_for_reference

            candidates = mailbox_task_ids_for_reference(reference)
        except Exception:  # noqa: BLE001 - a resolution aid must never raise
            return None
        for task_id in candidates:
            if (self.log_dir / f"{central_logging.TASK_LOG_PREFIX}{task_id}.jsonl").is_file():
                return SUBJECT_TASK, task_id
        row = self._fetchone(
            "SELECT id FROM mailbox_task_approvals WHERE file_path LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT 1",
            ("%" + _like_escape(reference.strip().strip("/")),),
        )
        if row is not None:
            return SUBJECT_TASK, str(row["id"])
        return None

    # ------------------------------------------------------------------
    # Lifecycle state
    # ------------------------------------------------------------------

    def status(self, reference: str) -> LifecycleState:
        """``nexus status <reference>``: lifecycle only, no log bodies."""
        subject_type, subject_id = self.resolve(reference)
        return self.status_for(subject_type, subject_id)

    def status_report(self, reference: str) -> DiagnosticReport:
        """``nexus status <reference>`` as a report, so one shape serves both.

        Same envelope as :meth:`diagnose` with the log bodies left out, rather
        than a second response shape a caller would have to branch on.
        """
        state = self.status(reference)
        return DiagnosticReport(
            generated_at=_iso(self._now()),
            reference=reference,
            lifecycle=state,
            notes=[state.progress.reason] if state.progress else [],
        )

    def status_for(self, subject_type: str, subject_id: str) -> LifecycleState:
        if subject_type == SUBJECT_TASK:
            return self.task_status(subject_id)
        if subject_type == SUBJECT_EXPERIMENT:
            return self.experiment_status(subject_id)
        if subject_type == SUBJECT_SESSION:
            return self.session_status(subject_id)
        raise DiagnosticsError(f"unknown subject type: {subject_type!r}")

    def task_status(self, task_id: str) -> LifecycleState:
        """Lifecycle of one mailbox task, joined across every subsystem it touched.

        A task's story is spread over four tables - the approval row owns
        intake and outcome, the experiment owns the implementation phase, the
        GitHub link owns branch/PR, and the controller session owns the
        heartbeat - and the operator's question ("where did this stop?") is
        answerable only from all four at once. That join is the point of this
        method; doing it by hand is what #188 is about.
        """
        row = self._fetchone(
            "SELECT * FROM mailbox_task_approvals WHERE id = ?", (task_id,)
        )
        if row is None:
            raise DiagnosticsError(f"no mailbox task {task_id[:64]!r}")
        status = str(row["status"] or "")
        lifecycle = _TASK_LIFECYCLE.get(status, LIFECYCLE_UNKNOWN)
        experiment_id = row["experiment_id"]
        correlation = {"mailbox_task_id": task_id}
        if experiment_id:
            correlation["experiment_id"] = str(experiment_id)

        state = LifecycleState(
            subject_type=SUBJECT_TASK,
            subject_id=task_id,
            lifecycle=lifecycle,
            status=status,
            title=_text(row["task_body"], 200),
            created_at=_column(row, "received_at") or _column(row, "created_at"),
            updated_at=_column(row, "updated_at"),
            # Approval is where a task *starts*, not where it ends, so only a
            # rejection ends it here; a task that ran ends when its experiment
            # does, which is attached below.
            ended_at=_column(row, "rejected_at"),
            last_error=_text(
                _column(row, "error")
                or _column(row, "rejection_reason")
                or _column(row, "publish_last_error"),
                _MAX_ERROR_CHARS,
            ),
            correlation_ids=correlation,
            service=SERVICE_ALIASES["mailbox"],
        )

        experiment = None
        if experiment_id:
            experiment = self._fetchone(
                "SELECT * FROM experiments WHERE id = ?", (str(experiment_id),)
            )
        if experiment is not None:
            experiment_status = str(experiment["status"] or "")
            state.related["experiment"] = {
                "id": str(experiment_id),
                "status": experiment_status,
                "lifecycle": _EXPERIMENT_LIFECYCLE.get(experiment_status, LIFECYCLE_UNKNOWN),
                "ended_at": _column(experiment, "ended_at"),
            }
            state.branch = _column(experiment, "branch_name")
            state.ended_at = state.ended_at or _column(experiment, "ended_at")
            if lifecycle in WORKING_LIFECYCLES:
                # While the task itself is still open, the experiment is the
                # phase actually running, so it - not the coarser task row -
                # is what "where is this now?" should report.
                lifecycle = _EXPERIMENT_LIFECYCLE.get(experiment_status, lifecycle)
                state.lifecycle = lifecycle
            if not state.last_error:
                state.last_error = _text(_column(experiment, "error"), _MAX_ERROR_CHARS)

        self._attach_github(state, experiment_id=experiment_id, source_id=task_id)
        self._attach_adoption(state, experiment_id=experiment_id)
        session = self._session_for(task_id, experiment_id)
        self._attach_session(state, session)
        self._assess(state, session)
        return state

    def experiment_status(self, experiment_id: str) -> LifecycleState:
        row = self._fetchone(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        )
        if row is None:
            raise DiagnosticsError(f"no experiment {experiment_id[:64]!r}")
        status = str(row["status"] or "")
        state = LifecycleState(
            subject_type=SUBJECT_EXPERIMENT,
            subject_id=experiment_id,
            lifecycle=_EXPERIMENT_LIFECYCLE.get(status, LIFECYCLE_UNKNOWN),
            status=status,
            title=_text(row["goal"], 200),
            created_at=_column(row, "created_at"),
            updated_at=_column(row, "ended_at") or _column(row, "created_at"),
            ended_at=_column(row, "ended_at"),
            branch=_column(row, "branch_name"),
            last_error=_text(_column(row, "error"), _MAX_ERROR_CHARS),
            correlation_ids={"experiment_id": experiment_id},
            service=SERVICE_ALIASES["api"],
        )
        task_row = self._fetchone(
            "SELECT id FROM mailbox_task_approvals WHERE experiment_id = ? LIMIT 1",
            (experiment_id,),
        )
        if task_row is not None:
            state.correlation_ids["mailbox_task_id"] = str(task_row["id"])
        exit_code = _column(row, "test_exit_code")
        if exit_code is not None:
            state.related["test_exit_code"] = int(exit_code)

        self._attach_github(state, experiment_id=experiment_id, source_id=None)
        self._attach_adoption(state, experiment_id=experiment_id)
        session = self._session_for(
            experiment_id, experiment_id, task_id=state.correlation_ids.get("mailbox_task_id")
        )
        self._attach_session(state, session)
        self._assess(state, session)
        return state

    def session_status(self, session_id: str) -> LifecycleState:
        row = self._fetchone(
            "SELECT * FROM nexus_task_sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise DiagnosticsError(f"no controller session {session_id[:64]!r}")
        status = str(row["status"] or "")
        state = LifecycleState(
            subject_type=SUBJECT_SESSION,
            subject_id=session_id,
            lifecycle=_SESSION_LIFECYCLE.get(status, LIFECYCLE_UNKNOWN),
            status=status,
            title=_text(_column(row, "title"), 200),
            created_at=_column(row, "created_at"),
            updated_at=_column(row, "updated_at"),
            worker_id=_column(row, "worker_id"),
            last_error=_text(_column(row, "terminal_reason"), _MAX_ERROR_CHARS),
            correlation_ids={"session_id": session_id},
            service=SERVICE_ALIASES["api"],
        )
        source_id = _column(row, "source_id")
        if source_id:
            state.correlation_ids["source_id"] = str(source_id)
        state.related["claim"] = {
            "worker_id": _column(row, "worker_id"),
            "claimed_at": _column(row, "claimed_at"),
            "last_heartbeat_at": _column(row, "last_heartbeat_at"),
            "terminal_reason": _column(row, "terminal_reason"),
        }
        self._assess(state, row, own_claim=True)
        return state

    # ------------------------------------------------------------------
    # Joins onto the subsystems that own the remaining facts
    # ------------------------------------------------------------------

    def _attach_github(
        self, state: LifecycleState, *, experiment_id: Any, source_id: str | None
    ) -> None:
        """Branch / issue / PR, from whichever link row covers this work.

        The branch is looked up even when the experiment already carries one:
        the #186/#187 case is precisely "the branch exists but nothing came
        after it", and a caller that cannot see the branch cannot tell that
        case from "never started".
        """
        row = None
        if experiment_id:
            row = self._fetchone(
                "SELECT * FROM github_work_links WHERE experiment_id = ? LIMIT 1",
                (str(experiment_id),),
            )
        if row is None and source_id:
            row = self._fetchone(
                "SELECT * FROM github_work_links WHERE source_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (source_id,),
            )
        if row is None and experiment_id:
            row = self._fetchone(
                "SELECT * FROM github_experiment_links WHERE experiment_id = ? LIMIT 1",
                (str(experiment_id),),
            )
        if row is None:
            return
        state.branch = _column(row, "branch_name") or state.branch
        issue_number = _column(row, "issue_number")
        if issue_number is not None:
            state.issue_number = int(issue_number)
        pr_number = _column(row, "pr_number")
        if pr_number is not None:
            state.pr_number = int(pr_number)
        state.commit = _column(row, "checkpoint_sha") or state.commit

    def _attach_adoption(self, state: LifecycleState, *, experiment_id: Any) -> None:
        """Adoption is what turns a green experiment into merged main."""
        if not experiment_id:
            return
        row = self._fetchone(
            "SELECT * FROM experiment_adoptions WHERE experiment_id = ? LIMIT 1",
            (str(experiment_id),),
        )
        if row is None:
            return
        status = str(row["status"] or "")
        state.related["adoption"] = {
            "id": _column(row, "id"),
            "status": status,
            "lifecycle": _ADOPTION_LIFECYCLE.get(status, LIFECYCLE_UNKNOWN),
            "started_at": _column(row, "started_at"),
            "ended_at": _column(row, "ended_at"),
            "target_branch": _column(row, "target_branch"),
            "reason": _text(_column(row, "reason"), _MAX_ERROR_CHARS),
        }
        state.commit = (
            _column(row, "integrated_commit_sha")
            or _column(row, "experiment_commit_sha")
            or state.commit
        )
        if status == "adopted":
            # Merged is further along than "the experiment succeeded", and it
            # is the state an operator asking "did this ship?" means.
            state.lifecycle = LIFECYCLE_MERGED
        elif status in ("failed", "interrupted") and state.lifecycle not in TERMINAL_LIFECYCLES:
            state.lifecycle = _ADOPTION_LIFECYCLE[status]
            if not state.last_error:
                state.last_error = _text(_column(row, "reason"), _MAX_ERROR_CHARS)

    def _session_for(
        self, subject_id: str, experiment_id: Any, *, task_id: str | None = None
    ) -> sqlite3.Row | None:
        """The controller session driving this work, when one claimed it.

        Preferred over every other progress signal because it is the only
        one that means "a worker said it was alive" rather than "something
        happened". An experiment is rarely a session's source: the session
        belongs to the task that started it, or waits on it by id, so both
        links are followed and the same work finds the same session from
        either side.
        """
        candidates = [subject_id]
        for linked in (experiment_id, task_id):
            if linked and str(linked) not in candidates:
                candidates.append(str(linked))
        for candidate in candidates:
            row = self._fetchone(
                """SELECT * FROM nexus_task_sessions
                    WHERE source_id = ? OR id = ?
                 ORDER BY last_heartbeat_at IS NULL, last_heartbeat_at DESC,
                          updated_at DESC
                    LIMIT 1""",
                (candidate, candidate),
            )
            if row is not None:
                return row
        if experiment_id:
            return self._fetchone(
                """SELECT * FROM nexus_task_sessions
                    WHERE waiting_experiment_id = ?
                 ORDER BY updated_at DESC
                    LIMIT 1""",
                (str(experiment_id),),
            )
        return None

    def _attach_session(self, state: LifecycleState, session: sqlite3.Row | None) -> None:
        if session is None:
            return
        state.worker_id = state.worker_id or _column(session, "worker_id")
        state.correlation_ids.setdefault("session_id", str(session["id"]))
        state.related["session"] = {
            "id": str(session["id"]),
            "status": _column(session, "status"),
            "worker_id": _column(session, "worker_id"),
            "claimed_at": _column(session, "claimed_at"),
            "last_heartbeat_at": _column(session, "last_heartbeat_at"),
            "terminal_reason": _column(session, "terminal_reason"),
        }

    def _assess(
        self,
        state: LifecycleState,
        session: sqlite3.Row | None,
        *,
        own_claim: bool = False,
    ) -> None:
        """Attach the progress verdict and the outcome classification (#163).

        ``own_claim`` is True only when *session* is the subject itself: then
        its ``terminal_reason`` is this work's terminal record. A session that
        merely drives a task contributes its heartbeat and how it ended, which
        :func:`classify_outcome` only applies to work that is still working:
        its turn ending is not the task ending, but its worker vanishing is
        the task's liveness evidence.
        """
        log_entries = self._recent_log_entries(state.correlation_ids)
        last_log_at = None
        for entry in log_entries:
            best, _ = _newest(last_log_at, entry.get("timestamp"))
            last_log_at = best or last_log_at
        heartbeat = _column(session, "last_heartbeat_at") if session else None
        state.progress = assess_progress(
            state.lifecycle,
            last_heartbeat_at=heartbeat,
            last_log_at=last_log_at,
            updated_at=state.updated_at or state.created_at,
            stale_after_seconds=self.stale_after_seconds,
            now=self._now(),
        )
        events = self._session_events(str(session["id"])) if session is not None else []
        state.outcome = classify_outcome(
            state.lifecycle,
            status=state.status,
            terminal_reason=_column(session, "terminal_reason") if own_claim else None,
            error=None if own_claim else state.last_error,
            last_heartbeat_at=heartbeat,
            recorded_heartbeat_at=_recorded_heartbeat(events),
            own_claim=own_claim,
            driver_status=None if own_claim else _column(session, "status"),
            driver_terminal_reason=None if own_claim else _column(session, "terminal_reason"),
            driver_ended_at=(
                None if own_claim else _reconciled_at(events) or _column(session, "updated_at")
            ),
            last_log_at=last_log_at,
            last_event=_last_meaningful_event(log_entries, events),
            updated_at=state.updated_at or state.created_at,
            restart_evidence=_restart_evidence(state),
            stale_after_seconds=self.stale_after_seconds,
            now=self._now(),
        )
        if state.outcome.outcome == OUTCOME_STALE and state.progress.state == PROGRESS_RUNNING:
            # Progress takes the newest timestamp, so a fresh ``updated_at`` can
            # read as movement after the heartbeat evidence has said otherwise.
            state.progress = replace(
                state.progress,
                state=PROGRESS_INTERRUPTED,
                reason=state.outcome.explanation,
            )

    def _session_events(self, session_id: str, *, limit: int = 20) -> list[sqlite3.Row]:
        """The controller journal's newest entries for one session, newest first."""
        return self._fetchall(
            """SELECT role, kind, content, metadata_json, created_at
                 FROM nexus_task_events
                WHERE session_id = ?
             ORDER BY id DESC
                LIMIT ?""",
            (session_id, limit),
        )

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    def _log_paths(self, correlation_ids: dict[str, str]) -> list[Path]:
        paths: list[Path] = []
        experiment_id = correlation_ids.get("experiment_id")
        if experiment_id and central_logging.is_safe_log_id(experiment_id):
            paths.append(self.log_dir / f"{experiment_id}.jsonl")
        task_id = correlation_ids.get("mailbox_task_id")
        if task_id and central_logging.is_safe_log_id(task_id):
            paths.append(self.log_dir / f"{central_logging.TASK_LOG_PREFIX}{task_id}.jsonl")
        session_id = correlation_ids.get("session_id")
        if session_id and central_logging.is_safe_log_id(session_id):
            paths.append(self.log_dir / f"{central_logging.CHAT_LOG_PREFIX}{session_id}.jsonl")
        return paths

    def _read_logs(
        self, correlation_ids: dict[str, str], *, limit: int
    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        """Merge every log a subject wrote, newest-bounded and chronological.

        Reading is best-effort per file: a diagnosis of a task whose experiment
        log is missing is still worth having, and is in fact the more
        interesting case, so a missing file is a note rather than an error.
        """
        entries: list[dict[str, Any]] = []
        sources: list[str] = []
        for path in self._log_paths(correlation_ids):
            try:
                found = central_logging.read_log_entries(path)
            except OSError:
                continue
            if not found:
                continue
            entries.extend(found)
            sources.append(path.name)
        entries.sort(key=central_logging.entry_sort_key)
        truncated = len(entries) > limit
        if truncated:
            entries = entries[-limit:]
        return entries, sources, truncated

    def _recent_log_entries(
        self, correlation_ids: dict[str, str], *, per_file: int = 20
    ) -> list[dict[str, Any]]:
        """The tail of each of a subject's log files, for liveness and last step.

        Only the tail is read: this runs on every status call, including the
        scan behind ``last-failure``, and the newest entries are by
        construction at the end. A few entries rather than one, because the
        newest is often a liveness tick and the last *meaningful* step sits
        just before it.
        """
        entries: list[dict[str, Any]] = []
        for path in self._log_paths(correlation_ids):
            try:
                found = central_logging.read_log_entries(
                    path, limit=per_file, tail_bytes=65536
                )
            except OSError:
                continue
            for entry in found:
                if entry.get("timestamp"):
                    entries.append({**entry, "_file": path.name})
        return entries

    def diagnose(self, reference: str, *, lines: object = None) -> DiagnosticReport:
        """``nexus logs <reference>``: lifecycle plus the logs that explain it."""
        subject_type, subject_id = self.resolve(reference)
        return self.diagnose_subject(subject_type, subject_id, lines=lines, reference=reference)

    def diagnose_subject(
        self,
        subject_type: str,
        subject_id: str,
        *,
        lines: object = None,
        reference: str | None = None,
    ) -> DiagnosticReport:
        limit = _bound_lines(lines, default=DEFAULT_LOG_LINES)
        state = self.status_for(subject_type, subject_id)
        entries, sources, truncated = self._read_logs(state.correlation_ids, limit=limit)
        notes: list[str] = []
        if not entries:
            notes.append(
                "no structured log entries were found for this subject - "
                f"check the {state.service or KNOWN_SERVICES[0]} service log for the "
                "same time window"
            )
        if state.progress and state.progress.state == PROGRESS_INTERRUPTED:
            notes.append(state.progress.reason)
        return DiagnosticReport(
            generated_at=_iso(self._now()),
            reference=reference or subject_id,
            lifecycle=state,
            log_entries=[_compact(entry) for entry in entries],
            log_sources=sources,
            truncated=truncated,
            notes=notes,
        )

    def evidence_bundle(
        self,
        *,
        experiment_id: str | None = None,
        mailbox_task_id: str | None = None,
        limit: int = 200,
    ) -> tuple[LifecycleState | None, list[dict[str, Any]]]:
        """Lifecycle state plus raw log entries, for the failure-analysis path.

        The same resolution and the same merge the ``logs``/``status``
        commands use, so a diagnosis and an operator looking at the same task
        see the same facts (#188). Entries come back *raw* rather than
        compacted: the analyzer applies its own relevance filter and its own
        bounds, and pre-compacting here would silently drop the fields that
        filter reads.

        Best-effort throughout. A subject that cannot be resolved, or a log
        directory that cannot be read, yields ``(None, [])`` - a diagnosis
        with no lifecycle context is weaker but still worth having, and
        evidence collection must never be the reason an analysis fails.
        """
        state: LifecycleState | None = None
        for subject_type, subject_id in (
            (SUBJECT_EXPERIMENT, experiment_id),
            (SUBJECT_TASK, mailbox_task_id),
        ):
            if not subject_id:
                continue
            try:
                state = self.status_for(subject_type, subject_id)
                break
            except DiagnosticsError:
                continue

        correlation = dict(state.correlation_ids) if state else {}
        # Even with no resolvable row, the ids the caller handed us still name
        # log files; reading them is the whole point of this call.
        if experiment_id:
            correlation.setdefault("experiment_id", experiment_id)
        if mailbox_task_id:
            correlation.setdefault("mailbox_task_id", mailbox_task_id)
        try:
            entries, _, _ = self._read_logs(correlation, limit=max(1, int(limit)))
        except Exception:  # noqa: BLE001 - evidence must never block diagnosis
            logger.warning(
                "diagnostics: could not read logs for experiment=%s task=%s",
                experiment_id, mailbox_task_id, exc_info=True,
            )
            entries = []
        return state, entries

    # ------------------------------------------------------------------
    # last-failure
    # ------------------------------------------------------------------

    def last_failure(self, *, lines: object = None, scan: object = None) -> DiagnosticReport:
        """The most recent failed *or stalled* piece of work, with its context.

        Stalled counts as failed here on purpose. The failure an operator is
        looking for after a silent night is usually not a row that says
        ``failed`` - a process that died mid-run never got to write one - it is
        a row still claiming to be ``implementing`` whose last progress signal
        is hours old. Finding only the former is what made the #186/#187
        investigation start from a blank page.
        """
        candidates = self._failure_candidates(scan=scan)
        if not candidates:
            raise DiagnosticsError("no failed or stalled task or experiment was found")
        _, subject_type, subject_id = candidates[0]
        report = self.diagnose_subject(subject_type, subject_id, lines=lines, reference="last-failure")
        report.notes.insert(
            0,
            f"most recent failed/stalled work: {subject_type} {subject_id} "
            f"(lifecycle {report.lifecycle.lifecycle}, progress "
            f"{report.lifecycle.progress.state if report.lifecycle.progress else 'unknown'}, "
            f"outcome {report.lifecycle.outcome.outcome if report.lifecycle.outcome else 'unknown'})",
        )
        return report

    def _failure_candidates(self, *, scan: object = None) -> list[tuple[str, str, str]]:
        """Recent tasks/experiments that failed or went quiet, newest first."""
        depth = _bound_int(
            scan,
            default=DEFAULT_LAST_FAILURE_CANDIDATES,
            maximum=MAX_LAST_FAILURE_CANDIDATES,
            name="scan",
        )
        rows: list[tuple[str, str, str]] = []
        for subject_type, sql in (
            (
                SUBJECT_EXPERIMENT,
                "SELECT id, created_at AS ts FROM experiments "
                "ORDER BY created_at DESC LIMIT ?",
            ),
            (
                SUBJECT_TASK,
                "SELECT id, updated_at AS ts FROM mailbox_task_approvals "
                "ORDER BY updated_at DESC LIMIT ?",
            ),
        ):
            for row in self._fetchall(sql, (depth,)):
                rows.append((str(row["ts"] or ""), subject_type, str(row["id"])))

        scored: list[tuple[str, str, str]] = []
        covered_experiments: set[str] = set()
        for _, subject_type, subject_id in rows:
            try:
                state = self.status_for(subject_type, subject_id)
            except DiagnosticsError:
                continue
            progress = state.progress
            interrupted = progress is not None and progress.state == PROGRESS_INTERRUPTED
            failed_outcome = (
                state.outcome is not None and state.outcome.outcome in FAILED_OUTCOMES
            )
            if state.lifecycle not in FAILED_LIFECYCLES and not interrupted and not failed_outcome:
                continue
            # Order by when the work was last seen alive, not by when its row
            # was created: "the latest failure" means the one that stopped most
            # recently.
            marker = (
                (progress.last_progress_at if progress else None)
                or state.ended_at
                or state.updated_at
                or state.created_at
                or ""
            )
            scored.append((marker, subject_type, subject_id))
            if subject_type == SUBJECT_TASK:
                covered_experiments.add(state.correlation_ids.get("experiment_id") or "")
        # A task and the experiment it spawned are one piece of work seen from
        # two sides, and both are candidates. The task view is the complete
        # one - it already carries the experiment's lifecycle, branch and logs
        # - so reporting the experiment separately would just be the same
        # failure a second time, with less context.
        scored = [
            entry
            for entry in scored
            if not (entry[1] == SUBJECT_EXPERIMENT and entry[2] in covered_experiments)
        ]
        scored.sort(
            key=lambda item: parse_timestamp(item[0])
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return scored

    # ------------------------------------------------------------------
    # Service logs
    # ------------------------------------------------------------------

    async def service_logs(
        self,
        service: str | None = None,
        *,
        lines: object = None,
        since_minutes: object = None,
    ) -> ServiceLogs:
        """``nexus logs service <name>``: a bounded journal slice, no journalctl.

        The caller names a service, not a unit, and never builds a command
        line. A journal that cannot be read (no systemd, unknown unit, a
        timeout) is reported in ``error`` rather than raised: a missing
        journal is itself a diagnostic finding, and the rest of the answer
        (which unit was consulted, over what window) still stands.
        """
        unit = resolve_service(service)
        limit = _bound_lines(lines, default=DEFAULT_SERVICE_LINES)
        reader = self.log_reader
        if reader is None:
            from modules.log_reader import LogReader

            reader = LogReader()
        since = None
        minutes = None
        if since_minutes is not None:
            minutes = _bound_int(
                since_minutes, default=60, maximum=60 * 24 * 7, name="since_minutes"
            )
            since = f"{minutes}m"
        result = ServiceLogs(
            generated_at=_iso(self._now()),
            service=unit,
            requested=(service or unit),
            lines=limit,
            since=since,
        )
        try:
            entries = await reader.tail_journal_entries(unit, limit)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            result.error = sanitized_reason(str(exc))
            return result
        if minutes is not None:
            cutoff = self._now().timestamp() - minutes * 60
            entries = [
                entry
                for entry in entries
                if (parsed := parse_timestamp(entry.get("timestamp"))) is None
                or parsed.timestamp() >= cutoff
            ]
        result.entries = [_compact(entry) for entry in entries]
        return result

    # ------------------------------------------------------------------
    # Local LLM jobs (#259)
    # ------------------------------------------------------------------

    def llm_job(self, job_id: str) -> dict[str, Any]:
        """One persisted ``local_llm_jobs`` row, looked up by its exact id.

        Exact id only: a job id is what the controller and the journal print,
        and a prefix match here would add a second resolution rule for no
        gain. Free text (prompt, system, result, error) has credential values
        masked on the way out - paths, URLs and JSON stay intact, because
        they are what the analysis needs; the row itself is only read.
        """
        candidate = (job_id or "").strip()
        if not candidate:
            raise DiagnosticsError("a job id is required")
        if not _SAFE_ID_RE.fullmatch(candidate):
            raise DiagnosticsError(f"unusable job id: {candidate[:64]!r}")
        row = self._fetchone("SELECT * FROM local_llm_jobs WHERE id = ?", (candidate,))
        if row is None:
            raise DiagnosticsError(f"no local LLM job with id {candidate[:64]!r}")
        job = {key: row[key] for key in row.keys()}
        for key in ("prompt", "system", "result", "error"):
            if job.get(key) is not None:
                job[key] = redact_credentials(str(job[key]))
        return job


def _column(row: Any, name: str) -> Any:
    """Read a column that may not exist on an older row shape.

    Schema migrations here are additive, so a database written before a column
    existed still answers every other question correctly; a diagnostic that
    raised ``no such column`` on it would be useless exactly when the history
    being diagnosed is old.
    """
    if row is None:
        return None
    try:
        value = row[name]
    except (IndexError, KeyError):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _event_metadata(event: Any) -> dict[str, Any]:
    raw = _column(event, "metadata_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _recorded_heartbeat(events: Sequence[Any]) -> str | None:
    """The last heartbeat a reconciliation preserved before clearing it (#162).

    ``reconcile_stale_heartbeats`` clears ``last_heartbeat_at`` - that is what
    makes it idempotent - so after the fact the only record of when the worker
    was last alive is the metadata of its ``heartbeat_timeout`` event.
    """
    for event in events:
        if _column(event, "kind") != _REASON_HEARTBEAT_TIMEOUT:
            continue
        value = _event_metadata(event).get("last_heartbeat_at")
        if parse_timestamp(value) is not None:
            return str(value)
    return None


def _reconciled_at(events: Sequence[Any]) -> str | None:
    """When a reconciliation declared this session's worker gone, if one did."""
    for event in events:
        if _column(event, "kind") == _REASON_HEARTBEAT_TIMEOUT:
            return _column(event, "created_at")
    return None


def _last_meaningful_event(
    log_entries: Sequence[dict[str, Any]], events: Sequence[Any]
) -> dict[str, Any] | None:
    """The newest step of the work itself, across logs and the session journal.

    Liveness ticks are skipped: "tests 40/80 completed" says the worker was
    alive, not what it was doing when it stopped. So are a reaper's own journal
    entries, which would otherwise always be the newest step of stale work.
    """
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for entry in log_entries:
        if is_liveness_log_event(entry):
            continue
        parsed = parse_timestamp(entry.get("timestamp"))
        if parsed is None:
            continue
        candidates.append(
            (
                parsed,
                {
                    "at": str(entry.get("timestamp")),
                    "source": f"log:{entry.get('_file') or 'unknown'}",
                    "kind": str(entry.get("event") or "") or None,
                    "phase": str(entry.get("phase") or "") or None,
                    "level": str(entry.get("level") or "") or None,
                    "message": _text(entry.get("message")),
                },
            )
        )
    for event in events:
        if _column(event, "kind") in _REAPER_SESSION_EVENTS:
            continue
        parsed = parse_timestamp(_column(event, "created_at"))
        if parsed is None:
            continue
        candidates.append(
            (
                parsed,
                {
                    "at": str(_column(event, "created_at")),
                    "source": "session_event",
                    "kind": _column(event, "kind"),
                    "phase": None,
                    "level": None,
                    "message": _text(_column(event, "content")),
                },
            )
        )
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _restart_evidence(state: LifecycleState) -> str | None:
    """An explicit restart marker recorded on a phase of this work, if any.

    Only recorded facts count: a restart is never inferred from silence,
    because a crash or a kill leaves exactly the same silence behind.
    """
    experiment = state.related.get("experiment") or {}
    if experiment.get("status") == "interrupted":
        return "the experiment was marked interrupted by startup recovery after a restart"
    adoption = state.related.get("adoption") or {}
    if adoption.get("status") == "interrupted":
        return "the adoption was interrupted by a process restart"
    return None


def render_report(report: DiagnosticReport) -> list[str]:
    """Flat, stable key/value lines for the operations/chat surface.

    The API returns structured JSON; this is the same content rendered for
    callers whose transport is a list of lines. The shape is deliberately
    ``key: value`` so it stays parseable rather than becoming prose.
    """
    state = report.lifecycle
    lines = [
        f"subject: {state.subject_type} {state.subject_id}",
        f"lifecycle: {state.lifecycle}",
        f"status: {state.status or '-'}",
    ]
    if state.progress:
        lines.extend(
            [
                f"progress: {state.progress.state}",
                f"progress_reason: {state.progress.reason}",
                f"last_progress_at: {state.progress.last_progress_at or '-'}"
                f" ({state.progress.source or 'none'})",
            ]
        )
    if state.outcome:
        outcome = state.outcome
        last_event = outcome.last_event or {}
        lines.extend(
            [
                f"outcome: {outcome.outcome}"
                f"{'' if outcome.conclusive else ' (inconclusive)'}",
                f"outcome_reason: {outcome.explanation}",
                f"outcome_evidence: {outcome.evidence}",
                f"probable_cause: {outcome.probable_cause or '-'}",
                f"last_healthy_heartbeat_at: {outcome.last_healthy_heartbeat_at or '-'}",
                "last_event: "
                + (
                    f"{last_event.get('at')} {last_event.get('source')} "
                    f"{last_event.get('kind') or '-'} {last_event.get('message') or ''}"
                    .strip()[:500]
                    if last_event
                    else "-"
                ),
                f"retry: {outcome.retry}",
            ]
        )
        for gap in outcome.evidence_gaps:
            lines.append(f"evidence_gap: {gap}")
    lines.extend(
        [
            f"branch: {state.branch or '-'}",
            f"commit: {state.commit or '-'}",
            f"issue: {'#' + str(state.issue_number) if state.issue_number else '-'}",
            f"pr: {'#' + str(state.pr_number) if state.pr_number else '-'}",
            f"worker: {state.worker_id or '-'}",
            f"service: {state.service or '-'}",
            f"last_error: {state.last_error or '-'}",
            f"correlation_ids: {_render_ids(state.correlation_ids) or '-'}",
        ]
    )
    for note in report.notes:
        lines.append(f"note: {note}")
    if report.log_entries:
        lines.append(f"log_entries: {len(report.log_entries)}"
                     f"{' (truncated)' if report.truncated else ''}")
        for entry in report.log_entries:
            lines.append(
                f"  {entry['timestamp']} {entry['level']} {entry['phase']} "
                f"{entry['event']} {entry['message']}"[:500]
            )
    return lines


LLM_JOB_VIEWS = ("summary", "result", "prompt")
# Bounds the full-text views; a local model's answer is far below this.
_MAX_LLM_JOB_TEXT_CHARS = 50_000


def _chars(value: Any) -> int:
    return len(value) if isinstance(value, str) else 0


def _preview(value: Any) -> str | None:
    # Already credential-masked by ``llm_job``; ``_text`` would also strip paths.
    text = str(value).strip() if value is not None else ""
    return text[:_MAX_MESSAGE_CHARS] or None


def _bounded_text_lines(label: str, value: Any) -> list[str]:
    if not value:
        return [f"{label}: -"]
    text = str(value)
    truncated = len(text) > _MAX_LLM_JOB_TEXT_CHARS
    lines = [f"{label}:" + (" (truncated)" if truncated else "")]
    lines.extend(text[:_MAX_LLM_JOB_TEXT_CHARS].splitlines())
    return lines


def render_llm_job(job: dict[str, Any], *, view: str = "summary") -> list[str]:
    """``nexus debug llm-job <id> [--result|--prompt]`` as key/value lines."""
    if view not in LLM_JOB_VIEWS:
        raise DiagnosticsError(f"view must be one of: {', '.join(LLM_JOB_VIEWS)}")
    if view == "result":
        return [f"job_id: {job['id']}", *_bounded_text_lines("result", job.get("result"))]
    if view == "prompt":
        return [
            f"job_id: {job['id']}",
            *_bounded_text_lines("system", job.get("system")),
            *_bounded_text_lines("prompt", job.get("prompt")),
        ]
    started = parse_timestamp(job.get("started_at"))
    finished = parse_timestamp(job.get("finished_at"))
    duration = (
        f"{(finished - started).total_seconds():.1f}s" if started and finished else "-"
    )
    result = job.get("result")
    return [
        f"job_id: {job['id']}",
        f"role: {job.get('role') or '-'}",
        f"session_id: {job.get('session_id') or '-'}",
        # Every job runs on the local llama.cpp server; the model name is
        # not persisted per job.
        "provider: local",
        f"status: {job.get('status') or '-'}",
        f"created_at: {job.get('created_at') or '-'}",
        f"started_at: {job.get('started_at') or '-'}",
        f"finished_at: {job.get('finished_at') or '-'}",
        f"duration: {duration}",
        f"worker: {job.get('worker_id') or '-'}",
        f"lease_expires_at: {job.get('lease_expires_at') or '-'}",
        f"temperature: {job.get('temperature') if job.get('temperature') is not None else '-'}",
        f"max_tokens: {job.get('max_tokens') or '-'}",
        f"response_format: {'yes' if job.get('response_format') else 'no'}",
        f"prompt_chars: {_chars(job.get('prompt'))}",
        f"system_chars: {_chars(job.get('system'))}",
        f"result_chars: {_chars(result)}",
        f"error_kind: {job.get('error_kind') or '-'}",
        f"error: {_preview(job.get('error')) or '-'}",
        f"result_preview: {_preview(result) or '-'}",
    ]


def _render_ids(ids: dict[str, str]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(ids.items()))


def render_service_logs(result: ServiceLogs) -> list[str]:
    lines = [
        f"service: {result.service}",
        f"requested: {result.requested}",
        f"lines: {result.lines}",
        f"since: {result.since or '-'}",
    ]
    if result.error:
        lines.append(f"error: {result.error}")
    for entry in result.entries:
        lines.append(f"  {entry['timestamp']} {entry['level']} {entry['message']}"[:500])
    return lines


def build_cli_parser() -> "argparse.ArgumentParser":
    """``nexus debug ...``: the terminal form of the read-only diagnostics."""
    import argparse

    parser = argparse.ArgumentParser(prog="nexus", description="Read-only Nexus diagnostics.")
    commands = parser.add_subparsers(dest="command", required=True)
    debug = commands.add_parser("debug", help="Inspect persisted Nexus state.")
    targets = debug.add_subparsers(dest="target", required=True)
    llm_job = targets.add_parser("llm-job", help="One local LLM job by exact id (#259).")
    llm_job.add_argument("job_id")
    view = llm_job.add_mutually_exclusive_group()
    view.add_argument("--result", dest="view", action="store_const", const="result",
                      help="Print the full result.")
    view.add_argument("--prompt", dest="view", action="store_const", const="prompt",
                      help="Print the system prompt and prompt.")
    llm_job.set_defaults(view="summary")
    llm_job.add_argument("--db", default=DEFAULT_DB_PATH,
                         help="Nexus database (default: AGENT_DB_PATH or /var/lib/nexus/agent.db).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    args = build_cli_parser().parse_args(argv)
    try:
        with NexusDiagnostics(db_path=args.db) as diagnostics:
            lines = render_llm_job(diagnostics.llm_job(args.job_id), view=args.view)
    except DiagnosticsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


__all__ = [
    "DiagnosticReport",
    "DiagnosticsError",
    "KNOWN_SERVICES",
    "LLM_JOB_VIEWS",
    "LifecycleState",
    "NexusDiagnostics",
    "OutcomeDiagnosis",
    "ProgressVerdict",
    "SERVICE_ALIASES",
    "ServiceLogs",
    "assess_progress",
    "classify_outcome",
    "render_llm_job",
    "render_report",
    "render_service_logs",
    "resolve_service",
]


if __name__ == "__main__":
    raise SystemExit(main())
