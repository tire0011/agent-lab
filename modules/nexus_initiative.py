"""Self-initiated work selection for Nexus.

Every existing entry point is reactive: the mailbox timer waits for a human to
drop a file in the inbox, ``POST /chat`` waits for someone to type, and the
mailbox task worker only moves tasks a human already approved. Nothing looks at
Nexus's own state and decides what to work on. This module is that missing step.

It deliberately does *not* add another stage to the lifecycle. It picks exactly
one piece of work per tick, records why, and hands it to the pipeline that
already exists. Its first priority is draining work that is finished but not
integrated - the state Nexus spent five days stuck in - because completing
started work is worth more than starting new work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from modules.experiment_evaluation import summarize_error
from modules.experiment_service import policy as goal_policy
from modules.experiment_service.models import PIPELINE_CRASH_ERROR_PREFIX
from modules.nexus_planner import PROPOSAL_LABEL
from modules.nexus_sources import issue_source
from modules.redaction import sanitized_reason
from modules.run_store import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

# Work is selected one item at a time. These are the states that mean some
# other part of the pipeline still owns the machine.
_BUSY_EXPERIMENT_STATUSES = ("queued", "selecting", "implementing", "verifying")
_BUSY_ADOPTION_STATUSES = ("queued", "adopting")
_BUSY_APPROVAL_STATUSES = ("queued", "approved", "submitted", "running", "nexus_deciding")

# An adoption in one of these states is not going to move on its own, but the
# experiment behind it succeeded - that is the integration backlog.
_UNFINISHED_ADOPTION_STATUSES = ("not_started", "needs_review", "interrupted")

# A row in a running state only means work is in flight if it is *recent*. The
# live database holds an experiment queued since 2026-08-14 and seven mailbox
# tasks stuck at `running`, the oldest untouched since 2026-08-13 - a busy
# check that trusted those states would never let the tick run again. Past this
# window a row is treated as abandoned, not as work in progress.
#
# Six hours is chosen to sit well clear of a healthy run: implementation and
# verification each have their own timeouts (the test timeout alone may reach
# 30 minutes), and calling a slow but live experiment abandoned would start a
# second one beside it. Too generous merely delays a tick; too tight corrupts
# the one-item-at-a-time guarantee.
_DEFAULT_STALE_AFTER_SECONDS = 6 * 60 * 60

KIND_CAPABILITY_RESUME = "capability_resume"
KIND_INTEGRATION = "integration_backlog"
# An issue whose last attempt the reviewer blocked: retried at once and free of
# the daily budget, so review findings go straight back to the coder.
REVIEW_RETRY_REASON = "the review blocked the last attempt; retry with its findings"
KIND_ISSUE = "open_issue"
KIND_IDEA = "new_idea"

_DEFAULT_DAILY_BUDGET = 5
# A restart hand-off can fail without replacing the API process.  Retrying on
# every minute tick adds noise and can contend with recovery work, so failed
# hand-offs get a fixed backoff before another attempt.
_SERVICE_RESTART_COOLDOWN = timedelta(minutes=30)
# Open proposals beyond this many are not added to (#354): they wait for review.
_MAX_OPEN_PROPOSALS = 3

# A failed issue experiment may be retried; the second failure ends it (#374).
# needs_review counts too: no human reviews it in autonomous runs, and the
# gate that stopped it may have been fixed since (#185, #349, #378).
_RETRYABLE_EXPERIMENT_STATUSES = ("failed", "timed_out", "rejected", "interrupted", "needs_review")
_MAX_ISSUE_ATTEMPTS = 2
# An open issue that used up its attempts is reported, not dropped silently
# (#457). It stays unselectable until a human acts, unless only pipeline crashes
# stopped it (#467): those end with a newer commit on origin/main.
DECISION_SKIPPED_ATTEMPTS_EXHAUSTED = "skipped_attempts_exhausted"

# An issue the goal policy rejects is not sent (#408): the experiment would be
# rejected before any work, at the cost of a tick and a budget slot.
DECISION_SKIPPED_GOAL_POLICY = "skipped_goal_policy"
# Reasons only _check_goal_policy produces. A rejection with one of them is
# known in advance from the goal text, so retrying the same text is pointless.
_GOAL_POLICY_REASON_PREFIXES = tuple(
    prefix for prefix, _ in goal_policy._POLICY_RULE_PREFIXES if prefix.startswith("goal ")
)


def _is_provider_overload(status: Any, error: Any) -> bool:
    """A run that failed because the coding agent's API was overloaded.

    A crash may quote "429" or "rate limit", but it stays a crash: an overload
    is free of every limit, and a crash loop must not be. Neither is a test
    failure an overload because the failing test is called test_rate_limit_*.
    """
    from modules.coding_provider import is_provider_overload

    return (
        status == "failed"
        and is_provider_overload(error)
        and not _is_pipeline_crash(status, error)
        and not str(error or "").startswith("tests failed")
    )


def _is_integration_conflict(adoption_status: Any, adoption_reason: Any) -> bool:
    """An adoption stopped only because the branch no longer merges into main (#485).

    The newer text lists the files ("Integrationskonflikt: a.py"); older
    adoptions recorded "Integrationskonflikt beim Cherry-Pick.".
    """
    if adoption_status not in ("needs_review", "failed"):
        return False
    reason = str(adoption_reason or "").strip()
    # The prefix covers both texts.
    return reason.startswith("Integrationskonflikt")


def _is_pipeline_crash(status: Any, error: Any) -> bool:
    """A run Nexus's own pipeline crashed in: no coding work was judged (#467)."""
    return status == "failed" and str(error or "").startswith(PIPELINE_CRASH_ERROR_PREFIX)


def _is_goal_policy_rejection(status: Any, error: Any) -> bool:
    return status == "rejected" and str(error or "").startswith(_GOAL_POLICY_REASON_PREFIXES)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _policy_hash() -> str:
    """Hash of the policy module: a changed policy may judge an issue anew."""
    try:
        return _sha256(Path(goal_policy.__file__).read_bytes())
    except OSError:
        return ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse the ISO timestamps the store writes, in either offset spelling."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _last_reason(attempts: dict[str, tuple[str, str]]) -> str:
    """The newest of ``experiment -> (when, why)``, safe to show on GitHub."""
    return sanitized_reason(" ".join(max(attempts.values())[1].split())) or "no reason recorded"


@dataclass(slots=True)
class Candidate:
    kind: str
    ref: str
    title: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InitiativeDecision:
    """What the tick decided, and why. Persisted before anything is dispatched."""

    # "dispatched" | "idle" | "busy" | "budget_exhausted" | "error"
    # | "skipped_goal_policy" | "skipped_attempts_exhausted"
    decision: str
    candidate: Candidate | None = None
    considered: list[Candidate] = field(default_factory=list)
    detail: str = ""
    tick_id: str = ""

    @property
    def acted(self) -> bool:
        return self.decision == "dispatched"


class NexusInitiativeService:
    """Choose and start one piece of Nexus's own work per tick."""

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        experiment_service: Any,
        adoption_service: Any | None = None,
        controller: Any | None = None,
        issue_lister: Callable[[], list[dict[str, Any]]] | None = None,
        idea_lister: Callable[[], list[Any]] | None = None,
        daily_budget: int | None = None,
        stale_after_seconds: int | None = None,
        planner: Any | None = None,
        issue_commenter: Callable[[int, str], None] | None = None,
        proposal_creator: Callable[[str, str], None] | None = None,
        proposal_counter: Callable[[], int] | None = None,
        diagnostics_client: Callable[[], dict[str, Any]] | None = None,
        checkout_revision: Callable[[], str] | None = None,
        restart_hook: Callable[[], None] | None = None,
        main_commit_date: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.experiment_service = experiment_service
        self.adoption_service = adoption_service
        self.controller = controller
        self._issue_lister = issue_lister
        self._idea_lister = idea_lister
        # Planning step (#354): which open issue is the most useful next one.
        self._planner = planner
        self._issue_commenter = issue_commenter
        self._proposal_creator = proposal_creator
        self._proposal_counter = proposal_counter
        self._diagnostics_client = diagnostics_client
        self._checkout_revision = checkout_revision
        self._restart_hook = restart_hook
        self._main_commit_date = main_commit_date
        self._open_proposals = 0
        self._open_proposal_titles: list[str] = []
        self._exhausted_issues: list[dict[str, Any]] = []
        if daily_budget is None:
            raw = os.environ.get("NEXUS_INITIATIVE_DAILY_BUDGET", str(_DEFAULT_DAILY_BUDGET))
            try:
                daily_budget = int(raw)
            except ValueError:
                daily_budget = _DEFAULT_DAILY_BUDGET
        self.daily_budget = max(0, daily_budget)
        if stale_after_seconds is None:
            raw = os.environ.get(
                "NEXUS_INITIATIVE_STALE_AFTER_SECONDS", str(_DEFAULT_STALE_AFTER_SECONDS)
            )
            try:
                stale_after_seconds = int(raw)
            except ValueError:
                stale_after_seconds = _DEFAULT_STALE_AFTER_SECONDS
        self.stale_after_seconds = max(60, stale_after_seconds)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        self._conn.close()

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_initiative_log (
                    tick_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    source_kind TEXT,
                    source_ref TEXT,
                    reason TEXT,
                    considered_json TEXT,
                    detail TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_initiative_created
                ON nexus_initiative_log(created_at)
                """
            )

    # ---- state inspection ------------------------------------------------

    def _activity_counts(
        self, table: str, statuses: tuple[str, ...], time_columns: tuple[str, ...]
    ) -> tuple[int, int]:
        """Return ``(active, abandoned)`` row counts for the given states."""
        placeholders = ",".join("?" for _ in statuses)
        try:
            rows = self._conn.execute(
                f"SELECT * FROM {table} WHERE status IN ({placeholders})", statuses
            ).fetchall()
        except sqlite3.OperationalError:
            # Table not present in this database yet - nothing can be busy in it.
            return (0, 0)
        cutoff = _utcnow() - timedelta(seconds=self.stale_after_seconds)
        active = abandoned = 0
        for row in rows:
            keys = row.keys()
            # The *latest* timestamp on the row, not the first one found. A
            # running experiment has no `ended_at`, so a first-match rule would
            # judge it by when it was created and could call a long but healthy
            # run abandoned - then start work beside it. This is still a
            # heuristic: without a heartbeat, "recent" is the best available
            # evidence that something is driving the row.
            stamps = [
                stamp
                for column in time_columns
                if column in keys
                for stamp in (_parse_timestamp(row[column]),)
                if stamp is not None
            ]
            # No readable timestamp counts as active: skipping a tick is cheap,
            # starting work beside a real run is not.
            if not stamps or max(stamps) >= cutoff:
                active += 1
            else:
                abandoned += 1
        return (active, abandoned)

    def busy_reason(self) -> str | None:
        """Return why Nexus is occupied, or ``None`` when it is free to start work."""
        for table, statuses, columns, label in (
            ("experiments", _BUSY_EXPERIMENT_STATUSES, ("ended_at", "created_at"),
             "an experiment is still running"),
            ("experiment_adoptions", _BUSY_ADOPTION_STATUSES, ("updated_at", "created_at"),
             "an adoption is still running"),
            ("mailbox_task_approvals", _BUSY_APPROVAL_STATUSES, ("updated_at", "created_at"),
             "a mailbox task is still running"),
        ):
            active, _ = self._activity_counts(table, statuses, columns)
            if active:
                return label
        return None

    def abandoned_work(self) -> list[dict[str, Any]]:
        """Rows left in a running state long enough that nothing is driving them.

        Reported rather than acted on: cleaning them up changes lifecycle state
        and belongs to the services that own it.
        """
        found: list[dict[str, Any]] = []
        for table, statuses, columns in (
            ("experiments", _BUSY_EXPERIMENT_STATUSES, ("ended_at", "created_at")),
            ("experiment_adoptions", _BUSY_ADOPTION_STATUSES, ("updated_at", "created_at")),
            ("mailbox_task_approvals", _BUSY_APPROVAL_STATUSES, ("updated_at", "created_at")),
        ):
            _, abandoned = self._activity_counts(table, statuses, columns)
            if abandoned:
                found.append({"table": table, "count": abandoned})
        return found

    def _spent_today(self) -> int:
        since = (_utcnow() - timedelta(days=1)).isoformat()
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM nexus_initiative_log
            WHERE decision = 'dispatched' AND created_at >= ?
              AND COALESCE(reason, '') != ?
            """,
            (since, REVIEW_RETRY_REASON),
        ).fetchone()
        spent = int(row["n"]) if row else 0
        # Crashes stay in the budget, unlike overloads (#467): an overload was
        # refused before any work, a crash can come after a paid coding run, and
        # this budget is the only limit across issues on a broken pipeline.
        return max(0, spent - self._overloaded_since(since))

    def _overloaded_since(self, since: str) -> int:
        """Experiments since *since* that failed only because the provider was overloaded."""
        try:
            rows = self._conn.execute(
                "SELECT status, error FROM experiments WHERE created_at >= ? AND status = 'failed'",
                (since,),
            ).fetchall()
        except sqlite3.OperationalError:
            return 0
        return sum(1 for r in rows if _is_provider_overload(r["status"], r["error"]))

    # ---- candidate discovery --------------------------------------------

    def _integration_candidates(self) -> list[Candidate]:
        """Experiments that succeeded but were never integrated.

        This is the highest priority on purpose. Work that is already
        implemented and verified is the cheapest possible progress, and leaving
        it stranded is what kept Nexus from ever completing a cycle.
        """
        placeholders = ",".join("?" for _ in _UNFINISHED_ADOPTION_STATUSES)
        try:
            rows = self._conn.execute(
                f"""
                SELECT e.id AS experiment_id, e.goal AS goal,
                       COALESCE(a.status, 'not_started') AS adoption_status
                FROM experiments e
                LEFT JOIN experiment_adoptions a ON a.experiment_id = e.id
                WHERE e.status = 'succeeded'
                  AND COALESCE(a.status, 'not_started') IN ({placeholders})
                ORDER BY e.ended_at ASC
                """,
                _UNFINISHED_ADOPTION_STATUSES,
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        candidates: list[Candidate] = []
        for row in rows:
            if row["adoption_status"] == "needs_review" and not self._review_may_resume(str(row["experiment_id"])):
                continue
            goal = str(row["goal"] or "").strip().splitlines()
            candidates.append(
                Candidate(
                    kind=KIND_INTEGRATION,
                    ref=str(row["experiment_id"]),
                    title=(goal[0] if goal else "(no goal recorded)")[:200],
                    reason=(
                        "experiment succeeded but adoption is "
                        f"{row['adoption_status']}; verified work is not integrated"
                    ),
                    payload={"adoption_status": str(row["adoption_status"])},
                )
            )
        return candidates

    def _review_may_resume(self, experiment_id: str) -> bool:
        """Whether a ``needs_review`` adoption could get further now.

        Most of these stop for a reason that does not go away (a denied path,
        unexpected files). Restarting them every tick changed nothing and used
        up the daily budget (d5e7de1f, 2026-10-29).
        """
        assess = getattr(self.adoption_service, "assess_failure", None)
        if assess is None:
            return True
        try:
            return bool(assess(experiment_id).retryable)
        except Exception as exc:  # noqa: BLE001 - an unknown answer is not a reason to retry
            logger.warning("initiative: cannot assess adoption %s: %s", experiment_id, exc)
            return False

    def _issue_candidates(self, *, record_skips: bool = True) -> list[Candidate]:
        if self._issue_lister is None:
            return []
        try:
            issues = self._issue_lister() or []
        except Exception as exc:  # noqa: BLE001 - a source outage must not stop the tick
            logger.warning("initiative: issue lookup failed: %s", exc)
            return []
        linked, exhausted = self._issue_attempts()
        reported = self._exhausted_reports()
        review_blocked = self._review_blocked_issues()
        skipped = self._goal_policy_skips()
        policy_hash = _policy_hash()
        candidates: list[Candidate] = []
        self._open_proposals = 0
        self._open_proposal_titles: list[str] = []
        self._exhausted_issues = []
        for issue in issues:
            number = issue.get("number")
            labels = {
                str(label.get("name") if isinstance(label, dict) else label).lower()
                for label in issue.get("labels") or []
            }
            if PROPOSAL_LABEL in labels:
                # Waits for a human to review it and remove the label (#354).
                self._open_proposals += 1
                self._open_proposal_titles.append(str(issue.get("title") or ""))
                continue
            if number is None or int(number) in linked:
                if number is not None and int(number) in exhausted:
                    self._note_exhausted(issue, exhausted[int(number)], reported, record=record_skips)
                continue
            candidate = Candidate(
                kind=KIND_ISSUE,
                ref=str(number),
                title=str(issue.get("title") or f"Issue #{number}")[:200],
                reason=(
                    REVIEW_RETRY_REASON if int(number) in review_blocked
                    else "open issue with no experiment working on it"
                ),
                payload={"body": str(issue.get("body") or "")[:16000]},
            )
            if self._fails_goal_policy(candidate, skipped, policy_hash, record=record_skips):
                continue
            candidates.append(candidate)
        return candidates

    def _review_blocked_issues(self) -> set[int]:
        """Issues whose latest attempt was stopped by the reviewer (#396)."""
        try:
            rows = self._conn.execute(
                """
                SELECT l.issue_number AS issue_number, a.status AS status, a.reason AS reason
                FROM github_experiment_links l
                JOIN experiments e ON e.id = l.experiment_id
                LEFT JOIN experiment_adoptions a ON a.experiment_id = e.id
                ORDER BY e.created_at
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
        latest: dict[int, bool] = {}
        for row in rows:
            try:
                number = int(row["issue_number"])
            except (TypeError, ValueError):
                continue
            latest[number] = row["status"] == "failed" and str(row["reason"] or "").startswith("Review by ")
        return {number for number, blocked in latest.items() if blocked}

    def _goal_policy_skips(self) -> set[tuple[str, str, str]]:
        """``(issue, issue hash, policy hash)`` of earlier goal-policy skips."""
        rows = self._conn.execute(
            "SELECT source_ref, detail FROM nexus_initiative_log WHERE decision = ?",
            (DECISION_SKIPPED_GOAL_POLICY,),
        ).fetchall()
        found: set[tuple[str, str, str]] = set()
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except ValueError:
                continue
            found.add((
                str(row["source_ref"]),
                str(detail.get("issue_hash") or ""),
                str(detail.get("policy_hash") or ""),
            ))
        return found

    def _fails_goal_policy(
        self,
        candidate: Candidate,
        skipped: set[tuple[str, str, str]],
        policy_hash: str,
        *,
        record: bool,
    ) -> bool:
        """Run the experiment's goal policy on this issue before it is sent (#408).

        The checked text is the issue source the controller builds from the
        same title and body. A skip is recorded once per issue text and policy
        version; while neither changes, the issue is not judged again.
        """
        goal = issue_source(
            int(candidate.ref), title=candidate.title, body=str(candidate.payload.get("body") or "")
        ).content
        issue_hash = _sha256(goal.encode("utf-8"))
        if (candidate.ref, issue_hash, policy_hash) in skipped:
            return True
        reason = goal_policy._check_goal_policy(goal)
        if reason is None:
            return False
        logger.info("initiative: issue #%s skipped by goal policy: %s", candidate.ref, reason)
        if record:
            self._record(
                InitiativeDecision(
                    DECISION_SKIPPED_GOAL_POLICY,
                    candidate=Candidate(KIND_ISSUE, candidate.ref, candidate.title, reason),
                    detail=json.dumps({"issue_hash": issue_hash, "policy_hash": policy_hash}),
                    tick_id=uuid.uuid4().hex,
                )
            )
            skipped.add((candidate.ref, issue_hash, policy_hash))
        return True

    def _exhausted_reports(self) -> dict[tuple[str, tuple[str, ...]], tuple[str, dict[str, Any]]]:
        """``(issue, experiment IDs) -> (tick id, detail)`` of earlier reports."""
        rows = self._conn.execute(
            "SELECT tick_id, source_ref, detail FROM nexus_initiative_log WHERE decision = ?",
            (DECISION_SKIPPED_ATTEMPTS_EXHAUSTED,),
        ).fetchall()
        found: dict[tuple[str, tuple[str, ...]], tuple[str, dict[str, Any]]] = {}
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except ValueError:
                continue
            key = (str(row["source_ref"]), tuple(detail.get("experiment_ids") or ()))
            found[key] = (str(row["tick_id"]), detail)
        return found

    def _note_exhausted(
        self,
        issue: dict[str, Any],
        attempts: dict[str, Any],
        reported: dict[tuple[str, tuple[str, ...]], tuple[str, dict[str, Any]]],
        *,
        record: bool,
    ) -> None:
        """Show this issue to the planner, and report the stop once (#457).

        One log entry per issue and set of experiment IDs. The comment is
        tracked in that entry: GitHub may be down when the entry is written,
        and without this a failed comment would never be posted.
        """
        ref = str(issue["number"])
        title = str(issue.get("title") or f"Issue #{ref}")[:200]
        self._exhausted_issues.append(
            {"number": int(ref), "title": title, "last_reason": attempts["last_reason"]}
        )
        if not record:
            return
        key = (ref, tuple(attempts["experiment_ids"]))
        if key in reported:
            tick_id, detail = reported[key]
        else:
            logger.info("initiative: issue #%s is out of attempts: %s", ref, attempts["last_reason"])
            tick_id, detail = uuid.uuid4().hex, {**attempts, "commented": False}
            self._record(
                InitiativeDecision(
                    DECISION_SKIPPED_ATTEMPTS_EXHAUSTED,
                    candidate=Candidate(KIND_ISSUE, ref, title, attempts["last_reason"]),
                    detail=json.dumps(detail),
                    tick_id=tick_id,
                )
            )
            reported[key] = (tick_id, detail)
        if detail.get("commented") or self._issue_commenter is None:
            return
        if attempts.get("pipeline_crash"):
            stop = f"Nexus paused this issue: its own pipeline crashed {_MAX_ISSUE_ATTEMPTS} times or more."
            how_on = (
                "The crash says nothing about this issue. It returns on its own once "
                "origin/main has a commit newer than the last crash; no human decision is needed."
            )
        else:
            stop = f"Nexus stopped working on this issue: it used up its {_MAX_ISSUE_ATTEMPTS} attempts."
            how_on = "Nexus does not start it again on its own; a human has to decide how it goes on."
        try:
            self._issue_commenter(
                int(ref),
                f"{stop}\n\n"
                f"Experiments: {', '.join(detail['experiment_ids'])}\n"
                f"Last failure: {detail['last_reason']}\n\n"
                f"{how_on}",
            )
        except Exception as exc:  # noqa: BLE001 - tried again on the next tick
            logger.warning("initiative: out-of-attempts comment failed for #%s: %s", ref, exc)
            return
        detail["commented"] = True
        self._record(
            InitiativeDecision(
                DECISION_SKIPPED_ATTEMPTS_EXHAUSTED, detail=json.dumps(detail), tick_id=tick_id
            ),
            update=True,
        )

    def _issue_attempts(self) -> tuple[set[int], dict[int, dict[str, Any]]]:
        """Issues not to start again (#374), and those among them out of attempts.

        An issue is free again after a failed experiment, up to
        ``_MAX_ISSUE_ATTEMPTS`` failed attempts. A running or succeeded
        experiment, or a link without a known experiment, keeps it blocked,
        and such an issue is not reported as out of attempts (#457): its work
        has not stopped.

        A pipeline crash is not an attempt (#467). ``_MAX_ISSUE_ATTEMPTS``
        crashes block the issue until origin/main has a commit newer than the
        last crash, so a broken pipeline gets one more run per commit.

        The newest integration-conflict stop of an issue is not an attempt
        (#485): its retry merges current main first. An older conflict stop
        counts, so a retry that conflicts again uses up the attempts.
        """
        # issue -> experiment -> (when, why it failed)
        failed: dict[int, dict[str, tuple[str, str]]] = {}
        # issues with an adoption stopped by a merge conflict; exactly one
        # such stop, the newest, is left out of the count
        conflicted: set[int] = set()
        # issue -> experiment -> (when it ended, the crash)
        crashes: dict[int, dict[str, tuple[str, str]]] = {}
        blocked: set[int] = set()
        for table in ("github_experiment_links", "github_work_links"):
            try:
                rows = self._conn.execute(
                    f"""
                    SELECT l.issue_number AS issue_number,
                           l.experiment_id AS experiment_id, e.status AS status,
                           e.error AS error, e.created_at AS created_at, e.ended_at AS ended_at,
                           a.status AS adoption_status, a.reason AS adoption_reason
                    FROM {table} l LEFT JOIN experiments e ON e.id = l.experiment_id
                    LEFT JOIN experiment_adoptions a ON a.experiment_id = l.experiment_id
                    """
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                try:
                    number = int(row["issue_number"])
                except (TypeError, ValueError):
                    continue
                # ensure_for_source records the source before any experiment
                # exists (#464): a link without an experiment is no work.
                if row["experiment_id"] is None:
                    continue
                # A goal-policy rejection is neither an attempt nor a block
                # (#408): the preflight in _issue_candidates judges the text.
                if _is_goal_policy_rejection(row["status"], row["error"]):
                    continue
                # An overloaded provider says nothing about the issue: not an attempt.
                if _is_provider_overload(row["status"], row["error"]):
                    continue
                if _is_pipeline_crash(row["status"], row["error"]):
                    crashes.setdefault(number, {})[str(row["experiment_id"])] = (
                        str(row["ended_at"] or ""),
                        str(row["error"]),
                    )
                    continue
                # A review that blocked the merge (#396) fails the adoption of
                # a succeeded experiment: that is a failed attempt too.
                merge_stopped = self._merge_stopped(row)
                if row["status"] in _RETRYABLE_EXPERIMENT_STATUSES or merge_stopped:
                    first, second = row["adoption_reason"], row["error"]
                    if not merge_stopped:
                        first, second = second, first
                    failed.setdefault(number, {})[str(row["experiment_id"])] = (
                        str(row["created_at"] or row["ended_at"] or ""),
                        str(first or second or ""),
                    )
                    if merge_stopped and _is_integration_conflict(
                        row["adoption_status"], row["adoption_reason"]
                    ):
                        conflicted.add(number)
                else:
                    blocked.add(number)
        # The exempted stop still names the experiments and the last reason.
        counted = {
            number: len(attempts) - (number in conflicted)
            for number, attempts in failed.items()
        }
        exhausted = {
            number: {
                "experiment_ids": sorted(attempts),
                "last_reason": _last_reason(attempts),
            }
            for number, attempts in failed.items()
            if counted[number] >= _MAX_ISSUE_ATTEMPTS and number not in blocked
        }
        blocked.update(n for n, count in counted.items() if count >= _MAX_ISSUE_ATTEMPTS)
        crash_limited = {n: a for n, a in crashes.items() if len(a) >= _MAX_ISSUE_ATTEMPTS}
        main_commit = self._main_commit_time() if crash_limited else None
        for number, attempts in crash_limited.items():
            ended = [_parse_timestamp(when) for when, _ in attempts.values()]
            if main_commit is not None and all(ended) and main_commit > max(ended):
                continue
            if number not in blocked:
                exhausted[number] = {
                    "experiment_ids": sorted(attempts),
                    "last_reason": _last_reason(attempts),
                    "pipeline_crash": True,
                }
            blocked.add(number)
        return blocked, exhausted

    def _main_commit_time(self) -> datetime | None:
        """When origin/main last moved, from the local ref; ``None`` if unreadable."""
        if self._main_commit_date is None:
            return None
        try:
            return _parse_timestamp(self._main_commit_date())
        except Exception as exc:  # noqa: BLE001 - an unreadable date keeps the block
            logger.warning("initiative: cannot read the origin/main commit date: %s", exc)
            return None

    def _merge_stopped(self, row: sqlite3.Row) -> bool:
        """The experiment succeeded but its merge stopped for good.

        A ``needs_review`` that cannot resume is such a stop too: the issue
        gets another attempt with the reason, instead of waiting for a human.
        """
        status = row["adoption_status"]
        if status == "failed":
            return True
        return status == "needs_review" and not self._review_may_resume(str(row["experiment_id"]))

    def _idea_candidates(self) -> list[Candidate]:
        lister = self._idea_lister
        if lister is None:
            from data_sources import list_ideas as lister  # local import keeps the module light
        try:
            ideas = lister() or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("initiative: idea lookup failed: %s", exc)
            return []
        candidates: list[Candidate] = []
        for idea in ideas:
            if str(getattr(idea, "state", "")).lower() != "new":
                continue
            candidates.append(
                Candidate(
                    kind=KIND_IDEA,
                    ref=str(getattr(idea, "id", "")),
                    title=str(getattr(idea, "text", ""))[:200],
                    reason="idea in state 'new' that nobody has explored",
                )
            )
        return candidates

    def resume_blocked_capability(self) -> InitiativeDecision:
        """Resume at most one capability-blocked session behind normal initiative gates."""
        tick_id = uuid.uuid4().hex
        if self.controller is None:
            return self._record(
                InitiativeDecision("idle", detail="no controller configured", tick_id=tick_id)
            )

        busy = self.busy_reason()
        if busy is not None:
            return self._record(InitiativeDecision("busy", detail=busy, tick_id=tick_id))

        if self.daily_budget and self._spent_today() >= self.daily_budget:
            return self._record(
                InitiativeDecision(
                    "budget_exhausted",
                    detail=f"{self.daily_budget} self-started items in the last 24h",
                    tick_id=tick_id,
                )
            )

        blockers = self.controller.orchestrator.task_store.blocked_missing_capabilities()
        if not blockers:
            return self._record(
                InitiativeDecision("idle", detail="no structured capability blocker", tick_id=tick_id)
            )

        current_catalog = {
            str(getattr(spec, "name", "")).strip()
            for spec in self.controller.orchestrator.operations_registry.catalog()
            if str(getattr(spec, "name", "")).strip()
        }
        candidate = next(
            (
                blocker
                for blocker in blockers
                if str(blocker["capability"]).strip() in current_catalog
                and str(blocker["capability"]).strip()
                not in {
                    str(name).strip()
                    for name in blocker.get("catalog_at_block") or []
                    if str(name).strip()
                }
            ),
            None,
        )
        if candidate is None:
            return self._record(
                InitiativeDecision("idle", detail="no newly available blocked capability", tick_id=tick_id)
            )

        chosen = Candidate(
            kind=KIND_CAPABILITY_RESUME,
            ref=str(candidate["session"].id),
            title=f"Resume {candidate['capability']}"[:200],
            reason=f"capability {candidate['capability']} became available",
            payload={"capability": str(candidate["capability"])},
        )
        decision = InitiativeDecision(
            "dispatched", candidate=chosen, considered=[chosen], tick_id=tick_id
        )
        self._record(decision)
        try:
            resumed = self.controller.resume_available_capabilities(limit=1)
            if not resumed:
                decision.decision = "idle"
                decision.detail = "capability blocker was claimed elsewhere"
                self._record(decision, update=True)
        except Exception as exc:  # noqa: BLE001 - failed unattended work is recorded
            logger.exception("initiative: capability resume failed for %s", chosen.ref)
            decision.decision = "error"
            decision.detail = f"{type(exc).__name__}: {exc}"[:1000]
            self._record(decision, update=True)
        return decision

    def candidates(self, *, record_skips: bool = True) -> list[Candidate]:
        """All *dispatchable* work, best first.

        Work whose dispatcher is not configured is left out rather than listed:
        a candidate that cannot be started is not a candidate, and reporting it
        would misrepresent both the backlog size and what the next tick will do.
        ``record_skips=False`` judges issues without writing skips to history.
        """
        found = list(self._integration_candidates()) if self.adoption_service is not None else []
        if self.controller is not None:
            issues = self._issue_candidates(record_skips=record_skips)
            retries = [c for c in issues if c.reason == REVIEW_RETRY_REASON]
            if retries:
                # Review findings go straight back to the coder, before new work.
                issues = retries
            # Integration work is dispatched first; asking the planner then
            # would be a paid call whose answer is thrown away.
            elif self._planner is not None and not found:
                issues = self._planned_issue(issues)
            found.extend(issues)
            found.extend(self._idea_candidates())
        return found

    def _recent_experiments(self, limit: int = 10) -> list[dict[str, str]]:
        """What Nexus tried lately and how it ended, for its own ideas.

        Each entry carries the newest adoption outcome too (#439): a
        ``succeeded`` experiment whose merge a review blocked is not done.
        Without an ``experiment_adoptions`` table only the old fields come back.
        """
        with_adoption = True
        try:
            rows = self._conn.execute(
                "SELECT e.goal, e.status, e.error, "
                "a.status AS adoption_status, a.reason AS adoption_reason "
                "FROM experiments e "
                "LEFT JOIN experiment_adoptions a ON a.rowid = ("
                "  SELECT a2.rowid FROM experiment_adoptions a2 "
                "  WHERE a2.experiment_id = e.id ORDER BY a2.rowid DESC LIMIT 1) "
                "WHERE e.ended_at IS NOT NULL ORDER BY e.ended_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            with_adoption = False
            try:
                rows = self._conn.execute(
                    "SELECT goal, status, error FROM experiments "
                    "WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        recent = []
        for row in rows:
            lines = str(row["goal"] or "").strip().splitlines()
            entry = {
                "title": lines[0][:120] if lines else "",
                "status": str(row["status"] or ""),
                "error": summarize_error(row["error"]),
            }
            if with_adoption:
                entry["adoption_status"] = str(row["adoption_status"] or "")
                entry["adoption_reason"] = " ".join(str(row["adoption_reason"] or "").split())[:200]
            recent.append(entry)
        return recent

    def _planned_issue(self, issues: list[Candidate]) -> list[Candidate]:
        """The one issue the planner picks as next, or none (#354).

        No plan means no issue work this tick: taking issues in list order
        is what the planning step replaces.
        """
        if not issues:
            return []
        choice = self._planner.choose(
            [{"number": int(c.ref), "title": c.title, "body": c.payload.get("body", "")} for c in issues],
            recent=self._recent_experiments(),
            exhausted=self._exhausted_issues,
            proposals=self._open_proposal_titles,
        )
        if choice is None:
            return []
        chosen = next(c for c in issues if int(c.ref) == choice.issue)
        chosen.reason = choice.reason
        chosen.payload["planned"] = True
        if choice.proposal_title and self._proposal_slot_free():
            chosen.payload["proposal"] = {"title": choice.proposal_title, "body": choice.proposal_body}
        return [chosen]

    def _proposal_slot_free(self) -> bool:
        """Fewer than three proposals are open; unknown counts as full.

        The issue list is capped, so it can miss older proposals: the count
        comes from its own exhaustive query when one is configured.
        """
        if self._proposal_counter is None:
            return self._open_proposals < _MAX_OPEN_PROPOSALS
        try:
            return self._proposal_counter() < _MAX_OPEN_PROPOSALS
        except Exception as exc:  # noqa: BLE001 - no proposal is the safe side
            logger.warning("initiative: proposal count failed: %s", exc)
            return False

    # ---- the tick --------------------------------------------------------

    def tick(self) -> InitiativeDecision:
        tick_id = uuid.uuid4().hex
        busy = self.busy_reason()
        if busy is not None:
            return self._record(InitiativeDecision("busy", detail=busy, tick_id=tick_id))

        stale = self._stale_service_revisions()
        if stale is not None:
            startup, checkout = stale
            decision = self._record(InitiativeDecision(
                "service_stale",
                detail=f"git_revision={startup} checkout_revision={checkout}",
                tick_id=tick_id,
            ))
            if self._restart_hook is not None and not self._restart_cooldown_active():
                try:
                    self._restart_hook()
                except Exception as exc:  # noqa: BLE001 - the activity must survive a failed hand-off
                    logger.exception("initiative: stale service restart hand-off failed")
                    decision.detail += f"; restart failed: {type(exc).__name__}: {exc}"[:500]
                    self._record(decision, update=True)
            return decision

        if self.daily_budget and self._spent_today() >= self.daily_budget:
            found = [c for c in self._issue_candidates() if c.reason == REVIEW_RETRY_REASON] if self.controller else []
            if not found:
                return self._record(
                    InitiativeDecision(
                        "budget_exhausted",
                        detail=f"{self.daily_budget} self-started items in the last 24h",
                        tick_id=tick_id,
                    )
                )
        else:
            found = self.candidates()
        if not found:
            return self._record(
                InitiativeDecision("idle", detail="no actionable work found", tick_id=tick_id)
            )

        chosen = found[0]
        decision = InitiativeDecision(
            "dispatched", candidate=chosen, considered=found[:10], tick_id=tick_id
        )
        # Persist the intent *before* acting, so a crash mid-dispatch leaves a
        # record of what Nexus was trying to do rather than a silent gap.
        self._record(decision)
        try:
            self._dispatch(chosen)
        except Exception as exc:  # noqa: BLE001 - a failed dispatch is data, not a crash
            logger.exception("initiative: dispatch failed for %s %s", chosen.kind, chosen.ref)
            decision.decision = "error"
            decision.detail = f"{type(exc).__name__}: {exc}"[:1000]
            self._record(decision, update=True)
        return decision

    def _stale_service_revisions(self) -> tuple[str, str] | None:
        """Return the two revisions when live diagnostics prove the API is stale."""
        if self._diagnostics_client is None or self._checkout_revision is None:
            return None
        try:
            payload = self._diagnostics_client()
            service = payload.get("service", payload)
            startup = service.get("git_revision")
        except Exception as exc:  # noqa: BLE001 - diagnostics must not disable initiative
            logger.warning("initiative: service diagnostics failed: %s", exc)
            return None
        if not startup:
            logger.warning("initiative: service diagnostics did not report git_revision")
            return None
        try:
            checkout = self._checkout_revision()
        except Exception as exc:  # noqa: BLE001 - revision lookup must not disable initiative
            logger.warning("initiative: checkout revision lookup failed: %s", exc)
            return None
        if not checkout:
            logger.warning("initiative: checkout revision lookup returned no revision")
            return None
        startup_text = str(startup).strip()
        checkout_text = str(checkout).strip()
        if not (startup_text.startswith(checkout_text) or checkout_text.startswith(startup_text)):
            return startup_text, checkout_text
        return None

    def _restart_cooldown_active(self) -> bool:
        """Whether a recent failed restart hand-off is still cooling down."""
        row = self._conn.execute(
            """
            SELECT created_at FROM nexus_initiative_log
            WHERE decision = 'service_stale' AND detail LIKE '%; restart failed:%'
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()
        failed_at = _parse_timestamp(row["created_at"]) if row is not None else None
        return failed_at is not None and failed_at > _utcnow() - _SERVICE_RESTART_COOLDOWN

    def _earlier_attempts(self, issue_number: int) -> str:
        """How earlier experiments for this issue ended, for the retry (#374).

        A review that blocked the merge (#396) explains exactly what was
        wrong; without it the next attempt would make the same mistake.
        """
        from modules.experiment_handover import EARLIER_ATTEMPTS_HEADING

        try:
            rows = self._conn.execute(
                """
                SELECT e.id AS id, e.id AS experiment_id, e.status AS status, e.error AS error,
                       a.status AS adoption_status, a.reason AS adoption_reason
                FROM github_experiment_links l
                JOIN experiments e ON e.id = l.experiment_id
                LEFT JOIN experiment_adoptions a ON a.experiment_id = e.id
                WHERE l.issue_number = ?
                ORDER BY e.created_at
                """,
                (issue_number,),
            ).fetchall()
        except sqlite3.OperationalError:
            return ""
        lines = []
        for row in rows:
            # Its reason quotes the rejected text, which would reject the retry.
            if _is_goal_policy_rejection(row["status"], row["error"]):
                continue
            if self._merge_stopped(row):
                outcome = f"merge stopped: {row['adoption_reason'] or ''}"
            elif row["status"] in _RETRYABLE_EXPERIMENT_STATUSES:
                outcome = f"{row['status']}: {row['error'] or ''}"
            else:
                continue
            lines.append(f"- experiment {str(row['id'])[:8]}, {' '.join(outcome.split())[:1500]}")
        if not lines:
            return ""
        return f"\n\n{EARLIER_ATTEMPTS_HEADING}\n" + "\n".join(lines)

    def _dispatch(self, candidate: Candidate) -> None:
        if candidate.kind == KIND_INTEGRATION:
            if self.adoption_service is None:
                raise RuntimeError("no adoption service configured")
            self.adoption_service.start_adoption(candidate.ref)
            return
        if self.controller is None:
            raise RuntimeError("no controller configured")
        if candidate.kind == KIND_ISSUE:
            self._announce_plan(candidate)
            self.controller.handle_issue(
                int(candidate.ref),
                title=candidate.title,
                body=str(candidate.payload.get("body") or "") + self._earlier_attempts(int(candidate.ref)),
            )
            return
        if candidate.kind == KIND_IDEA:
            self.controller.handle_idea(candidate.ref)
            return
        raise RuntimeError(f"unknown candidate kind {candidate.kind!r}")

    def _announce_plan(self, candidate: Candidate) -> None:
        """Say on GitHub why this issue is next, and open a suggested one (#354).

        Best effort: a failed comment must not stop the work itself.
        """
        if not candidate.payload.get("planned"):
            return
        if self._issue_commenter is not None:
            try:
                self._issue_commenter(
                    int(candidate.ref),
                    f"Nexus picked this issue as the next one to work on: {candidate.reason}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("initiative: plan comment failed for #%s: %s", candidate.ref, exc)
        proposal = candidate.payload.get("proposal")
        if proposal and self._proposal_creator is not None:
            try:
                self._proposal_creator(proposal["title"], proposal["body"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("initiative: proposal issue failed: %s", exc)

    def _record(self, decision: InitiativeDecision, *, update: bool = False) -> InitiativeDecision:
        considered = json.dumps(
            [{"kind": c.kind, "ref": c.ref, "reason": c.reason} for c in decision.considered]
        )
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO nexus_initiative_log (
                    tick_id, created_at, decision, source_kind, source_ref,
                    reason, considered_json, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tick_id) DO UPDATE SET
                    decision = excluded.decision,
                    detail = excluded.detail
                """,
                (
                    decision.tick_id,
                    _utcnow().isoformat(),
                    decision.decision,
                    decision.candidate.kind if decision.candidate else None,
                    decision.candidate.ref if decision.candidate else None,
                    decision.candidate.reason if decision.candidate else None,
                    considered,
                    decision.detail,
                ),
            )
        return decision

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM nexus_initiative_log
            ORDER BY created_at DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
        return [dict(row) for row in rows]
