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

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from modules.nexus_planner import PROPOSAL_LABEL
from modules.run_store import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

# Work is selected one item at a time. These are the states that mean some
# other part of the pipeline still owns the machine.
_BUSY_EXPERIMENT_STATUSES = ("queued", "selecting", "implementing", "verifying")
_BUSY_ADOPTION_STATUSES = ("queued", "adopting")
_BUSY_APPROVAL_STATUSES = ("approved", "submitted", "running", "nexus_deciding")

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
KIND_ISSUE = "open_issue"
KIND_IDEA = "new_idea"

_DEFAULT_DAILY_BUDGET = 5
# Open proposals beyond this many are not added to (#354): they wait for review.
_MAX_OPEN_PROPOSALS = 3


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

    decision: str  # "dispatched" | "idle" | "busy" | "budget_exhausted" | "error"
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
        self._open_proposals = 0
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
            """,
            (since,),
        ).fetchone()
        return int(row["n"]) if row else 0

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

    def _issue_candidates(self) -> list[Candidate]:
        if self._issue_lister is None:
            return []
        try:
            issues = self._issue_lister() or []
        except Exception as exc:  # noqa: BLE001 - a source outage must not stop the tick
            logger.warning("initiative: issue lookup failed: %s", exc)
            return []
        linked = self._linked_issue_numbers()
        candidates: list[Candidate] = []
        self._open_proposals = 0
        for issue in issues:
            number = issue.get("number")
            labels = {
                str(label.get("name") if isinstance(label, dict) else label).lower()
                for label in issue.get("labels") or []
            }
            if PROPOSAL_LABEL in labels:
                # Waits for a human to review it and remove the label (#354).
                self._open_proposals += 1
                continue
            if number is None or int(number) in linked:
                continue
            candidates.append(
                Candidate(
                    kind=KIND_ISSUE,
                    ref=str(number),
                    title=str(issue.get("title") or f"Issue #{number}")[:200],
                    reason="open issue with no experiment working on it",
                    payload={"body": str(issue.get("body") or "")[:16000]},
                )
            )
        return candidates

    def _linked_issue_numbers(self) -> set[int]:
        numbers: set[int] = set()
        for table in ("github_experiment_links", "github_work_links"):
            try:
                rows = self._conn.execute(f"SELECT issue_number FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                try:
                    numbers.add(int(row["issue_number"]))
                except (TypeError, ValueError):
                    continue
        return numbers

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

    def candidates(self) -> list[Candidate]:
        """All *dispatchable* work, best first.

        Work whose dispatcher is not configured is left out rather than listed:
        a candidate that cannot be started is not a candidate, and reporting it
        would misrepresent both the backlog size and what the next tick will do.
        """
        found = list(self._integration_candidates()) if self.adoption_service is not None else []
        if self.controller is not None:
            issues = self._issue_candidates()
            # Integration work is dispatched first; asking the planner then
            # would be a paid call whose answer is thrown away.
            if self._planner is not None and not found:
                issues = self._planned_issue(issues)
            found.extend(issues)
            found.extend(self._idea_candidates())
        return found

    def _planned_issue(self, issues: list[Candidate]) -> list[Candidate]:
        """The one issue the planner picks as next, or none (#354).

        No plan means no issue work this tick: taking issues in list order
        is what the planning step replaces.
        """
        if not issues:
            return []
        choice = self._planner.choose(
            [{"number": int(c.ref), "title": c.title, "body": c.payload.get("body", "")} for c in issues]
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

        if self.daily_budget and self._spent_today() >= self.daily_budget:
            return self._record(
                InitiativeDecision(
                    "budget_exhausted",
                    detail=f"{self.daily_budget} self-started items in the last 24h",
                    tick_id=tick_id,
                )
            )

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
                body=str(candidate.payload.get("body") or ""),
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
