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
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field
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
    "queued": LIFECYCLE_QUEUED,
    # Legacy states of the removed approval step (#264), kept readable.
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
    "waiting_for_user": LIFECYCLE_WAITING,
    "blocked_missing_capability": LIFECYCLE_WAITING,
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
    correlation_ids: dict[str, str] = field(default_factory=dict)
    related: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["progress"] = self.progress.to_dict() if self.progress else None
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
        state.progress = self._progress(state, session)
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
        session = self._session_for(experiment_id, experiment_id)
        self._attach_session(state, session)
        state.progress = self._progress(state, session)
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
        state.progress = self._progress(state, row)
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

    def _session_for(self, subject_id: str, experiment_id: Any) -> sqlite3.Row | None:
        """The controller session driving this work, when one claimed it.

        Preferred over every other progress signal because it is the only
        one that means "a worker said it was alive" rather than "something
        happened".
        """
        candidates = [subject_id]
        if experiment_id and str(experiment_id) != subject_id:
            candidates.append(str(experiment_id))
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

    def _progress(self, state: LifecycleState, session: sqlite3.Row | None) -> ProgressVerdict:
        return assess_progress(
            state.lifecycle,
            last_heartbeat_at=_column(session, "last_heartbeat_at") if session else None,
            last_log_at=self._last_log_timestamp(state.correlation_ids),
            updated_at=state.updated_at or state.created_at,
            stale_after_seconds=self.stale_after_seconds,
            now=self._now(),
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

    def _last_log_timestamp(self, correlation_ids: dict[str, str]) -> str | None:
        """Newest log timestamp across a subject's files, read from the tail.

        Only the tail of each file is read: this runs on every status call,
        including the scan behind ``last-failure``, and the newest entry is by
        construction at the end.
        """
        newest: str | None = None
        for path in self._log_paths(correlation_ids):
            try:
                entries = central_logging.read_log_entries(path, limit=1, tail_bytes=65536)
            except OSError:
                continue
            if not entries:
                continue
            candidate = str(entries[-1].get("timestamp") or "")
            if not candidate:
                continue
            best, _ = _newest(newest, candidate)
            newest = best or candidate
        return newest

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
            f"{report.lifecycle.progress.state if report.lifecycle.progress else 'unknown'})",
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
            if state.lifecycle not in FAILED_LIFECYCLES and not interrupted:
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
    "ProgressVerdict",
    "SERVICE_ALIASES",
    "ServiceLogs",
    "assess_progress",
    "render_llm_job",
    "render_report",
    "render_service_logs",
    "resolve_service",
]


if __name__ == "__main__":
    raise SystemExit(main())
