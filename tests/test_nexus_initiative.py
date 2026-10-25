"""Tests for Nexus's self-initiated work selection."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.nexus_initiative import (
    KIND_IDEA,
    KIND_INTEGRATION,
    KIND_ISSUE,
    NexusInitiativeService,
)


class RecordingAdoption:
    def __init__(self) -> None:
        self.started: list[str] = []

    def start_adoption(self, experiment_id: str) -> None:
        self.started.append(experiment_id)


class RecordingController:
    def __init__(self) -> None:
        self.issues: list[int] = []
        self.ideas: list[str] = []

    def handle_issue(self, number: int, *, title: str, body: str) -> None:
        self.issues.append(number)

    def handle_idea(self, idea_id: str) -> None:
        self.ideas.append(idea_id)


class InitiativeFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="nexus-initiative-")
        self.db_path = str(Path(self.tmp) / "agent.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY, goal TEXT, status TEXT,
                ended_at TEXT, created_at TEXT
            );
            CREATE TABLE experiment_adoptions (
                experiment_id TEXT PRIMARY KEY, status TEXT
            );
            CREATE TABLE mailbox_task_approvals (
                id TEXT PRIMARY KEY, status TEXT, updated_at TEXT
            );
            CREATE TABLE github_experiment_links (
                experiment_id TEXT PRIMARY KEY, issue_number INTEGER
            );
            """
        )
        conn.commit()
        conn.close()
        self.adoption = RecordingAdoption()
        self.controller = RecordingController()

    def add_experiment(self, exp_id: str, status: str, *, adoption: str | None = None, ended=None):
        from datetime import datetime, timezone

        ended = ended or datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO experiments (id, goal, status, ended_at) VALUES (?, ?, ?, ?)",
            (exp_id, f"Goal for {exp_id}", status, ended),
        )
        if adoption is not None:
            conn.execute(
                "INSERT INTO experiment_adoptions (experiment_id, status) VALUES (?, ?)",
                (exp_id, adoption),
            )
        conn.commit()
        conn.close()

    def add_approval(self, status: str, *, updated: str | None = None) -> None:
        from datetime import datetime, timezone

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO mailbox_task_approvals (id, status, updated_at) VALUES (?, ?, ?)",
            ("a1", status, updated or datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

    def service(self, **kwargs) -> NexusInitiativeService:
        kwargs.setdefault("idea_lister", lambda: [])
        return NexusInitiativeService(
            db_path=self.db_path,
            experiment_service=SimpleNamespace(),
            adoption_service=self.adoption,
            controller=self.controller,
            **kwargs,
        )


class NexusInitiativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = InitiativeFixture()

    def test_does_nothing_while_an_experiment_runs(self):
        self.fx.add_experiment("e1", "implementing")
        svc = self.fx.service()
        self.addCleanup(svc.close)

        decision = svc.tick()

        self.assertEqual(decision.decision, "busy")
        self.assertFalse(decision.acted)
        self.assertEqual(self.fx.adoption.started, [])

    def test_does_nothing_while_a_mailbox_task_runs(self):
        self.fx.add_approval("running")
        svc = self.fx.service()
        self.addCleanup(svc.close)

        self.assertEqual(svc.tick().decision, "busy")

    def test_long_abandoned_running_row_does_not_block_forever(self):
        # The live database really does hold a mailbox task stuck at `running`
        # since 2026-08-13. Treating that as work in progress would mean the
        # tick never runs again.
        self.fx.add_approval("running", updated="2026-08-13T14:18:37Z")
        self.fx.add_experiment("stuck", "succeeded", adoption="needs_review")
        svc = self.fx.service()
        self.addCleanup(svc.close)

        decision = svc.tick()

        self.assertTrue(decision.acted)
        self.assertEqual(self.fx.adoption.started, ["stuck"])

    def test_long_but_live_experiment_is_not_called_abandoned(self):
        # A running experiment has no ended_at. Judging it by created_at would
        # declare a healthy long run abandoned and start work beside it, which
        # breaks the one-item-at-a-time guarantee.
        from datetime import datetime, timedelta, timezone

        started = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        conn = sqlite3.connect(self.fx.db_path)
        conn.execute(
            "INSERT INTO experiments (id, goal, status, ended_at, created_at) VALUES (?,?,?,?,?)",
            ("slow", "Long job", "implementing", None, started),
        )
        conn.commit()
        conn.close()
        svc = self.fx.service()
        self.addCleanup(svc.close)

        self.assertEqual(svc.tick().decision, "busy")
        self.assertEqual(svc.abandoned_work(), [])

    def test_abandoned_work_is_reported_rather_than_silently_ignored(self):
        self.fx.add_approval("running", updated="2026-08-13T14:18:37Z")
        svc = self.fx.service()
        self.addCleanup(svc.close)

        reported = svc.abandoned_work()

        self.assertEqual(reported, [{"table": "mailbox_task_approvals", "count": 1}])

    def test_recent_running_row_still_counts_as_busy(self):
        from datetime import datetime, timezone

        self.fx.add_approval("running", updated=datetime.now(timezone.utc).isoformat())
        self.fx.add_experiment("stuck", "succeeded", adoption="needs_review")
        svc = self.fx.service()
        self.addCleanup(svc.close)

        self.assertEqual(svc.tick().decision, "busy")
        self.assertEqual(self.fx.adoption.started, [])

    def test_work_without_a_dispatcher_is_not_offered(self):
        # --integration-only builds no controller. Ideas and issues are then
        # unstartable, so they must not appear in the backlog at all.
        svc = self.fx.service(
            issue_lister=lambda: [{"number": 7, "title": "Nope", "body": ""}],
            idea_lister=lambda: [SimpleNamespace(id="idea-1", text="Nope", state="new")],
        )
        svc.controller = None
        self.addCleanup(svc.close)

        self.assertEqual(svc.candidates(), [])
        self.assertEqual(svc.tick().decision, "idle")

    def test_idle_when_there_is_nothing_to_do(self):
        svc = self.fx.service()
        self.addCleanup(svc.close)

        decision = svc.tick()

        self.assertEqual(decision.decision, "idle")

    def test_unintegrated_verified_work_is_picked_first(self):
        # The real 3946adc3 shape: succeeded, stuck at needs_review.
        self.fx.add_experiment("stuck", "succeeded", adoption="needs_review")
        svc = self.fx.service(
            issue_lister=lambda: [{"number": 7, "title": "Something new", "body": "..."}],
            idea_lister=lambda: [SimpleNamespace(id="idea-1", text="An idea", state="new")],
        )
        self.addCleanup(svc.close)

        decision = svc.tick()

        self.assertTrue(decision.acted)
        self.assertEqual(decision.candidate.kind, KIND_INTEGRATION)
        self.assertEqual(self.fx.adoption.started, ["stuck"])
        self.assertEqual(self.fx.controller.issues, [])
        self.assertEqual(self.fx.controller.ideas, [])

    def test_adopted_work_is_not_picked_again(self):
        self.fx.add_experiment("done", "succeeded", adoption="adopted")
        svc = self.fx.service()
        self.addCleanup(svc.close)

        self.assertEqual(svc.tick().decision, "idle")
        self.assertEqual(self.fx.adoption.started, [])

    def test_open_issue_is_picked_when_nothing_is_stuck(self):
        svc = self.fx.service(
            issue_lister=lambda: [{"number": 7, "title": "Something new", "body": "do it"}]
        )
        self.addCleanup(svc.close)

        decision = svc.tick()

        self.assertEqual(decision.candidate.kind, KIND_ISSUE)
        self.assertEqual(self.fx.controller.issues, [7])

    def test_issue_already_being_worked_on_is_skipped(self):
        conn = sqlite3.connect(self.fx.db_path)
        conn.execute(
            "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)",
            ("e9", 7),
        )
        conn.commit()
        conn.close()
        svc = self.fx.service(
            issue_lister=lambda: [{"number": 7, "title": "Already taken", "body": ""}]
        )
        self.addCleanup(svc.close)

        self.assertEqual(svc.tick().decision, "idle")

    def test_new_idea_is_the_last_resort(self):
        svc = self.fx.service(
            idea_lister=lambda: [
                SimpleNamespace(id="idea-1", text="Explore this", state="new"),
                SimpleNamespace(id="idea-2", text="Already done", state="adopted"),
            ]
        )
        self.addCleanup(svc.close)

        decision = svc.tick()

        self.assertEqual(decision.candidate.kind, KIND_IDEA)
        self.assertEqual(self.fx.controller.ideas, ["idea-1"])

    def test_daily_budget_stops_runaway_self_starting(self):
        self.fx.add_experiment("stuck", "succeeded", adoption="needs_review")
        svc = self.fx.service(daily_budget=1)
        self.addCleanup(svc.close)

        self.assertTrue(svc.tick().acted)
        second = svc.tick()

        self.assertEqual(second.decision, "budget_exhausted")
        self.assertEqual(self.fx.adoption.started, ["stuck"])

    def test_default_daily_budget_is_five(self):
        svc = self.fx.service(
            issue_lister=lambda: [
                {"number": number, "title": f"Issue {number}", "body": ""}
                for number in range(1, 7)
            ]
        )
        self.addCleanup(svc.close)

        # A completed dispatch no longer appears busy, so repeated timer ticks
        # can consume the daily allowance. The sixth must remain untouched.
        for _ in range(5):
            decision = svc.tick()
            self.assertTrue(decision.acted)
            with sqlite3.connect(self.fx.db_path) as conn:
                conn.execute(
                    "INSERT INTO github_experiment_links "
                    "(experiment_id, issue_number) VALUES (?, ?)",
                    (f"e{decision.candidate.ref}", int(decision.candidate.ref)),
                )

        decision = svc.tick()

        self.assertEqual(decision.decision, "budget_exhausted")
        self.assertEqual(self.fx.controller.issues, [1, 2, 3, 4, 5])

    def test_failed_dispatch_is_recorded_not_raised(self):
        self.fx.add_experiment("stuck", "succeeded", adoption="needs_review")

        class Exploding:
            def start_adoption(self, _):
                raise RuntimeError("adoption lock held")

        svc = self.fx.service()
        svc.adoption_service = Exploding()
        self.addCleanup(svc.close)

        decision = svc.tick()

        self.assertEqual(decision.decision, "error")
        self.assertIn("adoption lock held", decision.detail)
        self.assertEqual(svc.history(limit=1)[0]["decision"], "error")

    def test_intent_is_persisted_with_what_else_was_considered(self):
        self.fx.add_experiment("stuck", "succeeded", adoption="needs_review")
        svc = self.fx.service(
            idea_lister=lambda: [SimpleNamespace(id="idea-1", text="Other option", state="new")]
        )
        self.addCleanup(svc.close)

        svc.tick()
        entry = svc.history(limit=1)[0]

        self.assertEqual(entry["decision"], "dispatched")
        self.assertEqual(entry["source_kind"], KIND_INTEGRATION)
        self.assertEqual(entry["source_ref"], "stuck")
        self.assertIn("idea-1", entry["considered_json"])
        self.assertIn("not integrated", entry["reason"])


if __name__ == "__main__":
    unittest.main()
