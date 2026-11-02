"""Tests for Nexus's self-initiated work selection."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules import nexus_initiative
from modules.experiment_service.models import PIPELINE_CRASH_ERROR_PREFIX
from modules.github_work_tracking import GitHubWorkTrackingService
from modules.nexus_initiative import (
    DECISION_SKIPPED_ATTEMPTS_EXHAUSTED,
    KIND_IDEA,
    KIND_INTEGRATION,
    KIND_ISSUE,
    NexusInitiativeService,
    _parse_timestamp,
)
from modules.nexus_planner import IssuePlanner
from modules.run_store import init_db
from nexus_tick import _main_commit_date


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
                ended_at TEXT, created_at TEXT, error TEXT
            );
            CREATE TABLE experiment_adoptions (
                experiment_id TEXT PRIMARY KEY, status TEXT, reason TEXT
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

    def test_stale_service_is_recorded_restarted_and_no_work_is_submitted(self):
        restart = MagicMock()
        issue_lister = MagicMock(return_value=[{"number": 441, "title": "work", "body": ""}])
        svc = self.fx.service(
            diagnostics_client=lambda: {"service": {
                "status": "ok",
                "git_revision": "aaaa111",
            }},
            checkout_revision=lambda: "bbbb222",
            restart_hook=restart,
            issue_lister=issue_lister,
        )
        self.addCleanup(svc.close)

        decision = svc.tick()

        self.assertEqual(decision.decision, "service_stale")
        restart.assert_called_once_with()
        issue_lister.assert_not_called()
        self.assertEqual(self.fx.controller.issues, [])
        entries = svc.history()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["decision"], "service_stale")
        self.assertIn("aaaa111", entries[0]["detail"])
        self.assertIn("bbbb222", entries[0]["detail"])

    def test_fresh_service_allows_tick_to_proceed(self):
        restart = MagicMock()
        svc = self.fx.service(
            diagnostics_client=lambda: {"service": {
                "git_revision": "aaaa111",
            }},
            checkout_revision=lambda: "aaaa111ddddddddddddddddddddddddddddddddd",
            restart_hook=restart,
            issue_lister=lambda: [{"number": 441, "title": "work", "body": ""}],
        )
        self.addCleanup(svc.close)

        decision = svc.tick()

        self.assertEqual(decision.decision, "dispatched")
        self.assertEqual(self.fx.controller.issues, [441])
        restart.assert_not_called()

    def test_missing_service_revision_allows_tick_to_proceed(self):
        restart = MagicMock()
        svc = self.fx.service(
            diagnostics_client=lambda: {"service": {"status": "ok"}},
            checkout_revision=lambda: "bbbb222",
            restart_hook=restart,
            issue_lister=lambda: [{"number": 448, "title": "work", "body": ""}],
        )
        self.addCleanup(svc.close)

        self.assertEqual(svc.tick().decision, "dispatched")
        self.assertEqual(self.fx.controller.issues, [448])
        restart.assert_not_called()

    def test_failed_checkout_revision_lookup_allows_tick_to_proceed(self):
        def fail_lookup():
            raise RuntimeError("git unavailable")

        restart = MagicMock()
        svc = self.fx.service(
            diagnostics_client=lambda: {"service": {"git_revision": "aaaa111"}},
            checkout_revision=fail_lookup,
            restart_hook=restart,
            issue_lister=lambda: [{"number": 448, "title": "work", "body": ""}],
        )
        self.addCleanup(svc.close)

        self.assertEqual(svc.tick().decision, "dispatched")
        self.assertEqual(self.fx.controller.issues, [448])
        restart.assert_not_called()

    def test_busy_service_is_not_restarted_when_revisions_differ(self):
        self.fx.add_experiment("e1", "implementing")
        restart = MagicMock()
        diagnostics = MagicMock(return_value={"service": {"git_revision": "aaaa111"}})
        svc = self.fx.service(
            diagnostics_client=diagnostics,
            checkout_revision=lambda: "bbbb222",
            restart_hook=restart,
        )
        self.addCleanup(svc.close)

        self.assertEqual(svc.tick().decision, "busy")
        restart.assert_not_called()
        diagnostics.assert_not_called()

    def test_failed_restart_hand_off_is_not_retried_inside_cooldown(self):
        restart = MagicMock(side_effect=RuntimeError("systemctl failed"))
        svc = self.fx.service(
            diagnostics_client=lambda: {"service": {"git_revision": "aaaa111"}},
            checkout_revision=lambda: "bbbb222",
            restart_hook=restart,
        )
        self.addCleanup(svc.close)

        self.assertEqual(svc.tick().decision, "service_stale")
        self.assertEqual(svc.tick().decision, "service_stale")
        restart.assert_called_once_with()

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


class IssuesOutOfAttemptsTests(unittest.TestCase):
    """#457: an issue that used up its attempts is reported, not dropped silently."""

    ISSUES = [
        {"number": 232, "title": "Stuck issue", "body": ""},
        {"number": 7, "title": "Free issue", "body": ""},
    ]

    def setUp(self) -> None:
        self.fx = InitiativeFixture()
        self.comments: list[tuple[int, str]] = []
        self.prompts: list[str] = []
        self.answer = json.dumps({"issue": 7, "reason": "free"})

    def _fail(self, exp_id, *, issue=232, status="failed", error=None, adoption=None, reason=None, ended=None):
        self.fx.add_experiment(exp_id, status, adoption=adoption, ended=ended)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("UPDATE experiments SET error = ? WHERE id = ?", (error, exp_id))
            conn.execute("UPDATE experiment_adoptions SET reason = ? WHERE experiment_id = ?", (reason, exp_id))
            conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)",
                (exp_id, issue),
            )

    def _complete(self, prompt):
        self.prompts.append(prompt)
        return self.answer

    def _service(self, *, issues=None, commenter=None, **extra):
        svc = self.fx.service(
            issue_lister=lambda: [dict(i) for i in (issues or self.ISSUES)],
            issue_commenter=commenter or (lambda number, text: self.comments.append((number, text))),
            **extra,
        )
        self.addCleanup(svc.close)
        return svc

    def _planned(self, **extra):
        return self._service(planner=IssuePlanner(self._complete), **extra)

    @staticmethod
    def _entries(svc):
        return [e for e in svc.history(limit=200) if e["decision"] == DECISION_SKIPPED_ATTEMPTS_EXHAUSTED]

    def test_reported_once_over_two_ticks_and_shown_to_the_planner(self):
        self._fail("e1", error="first", ended="2026-10-01T00:00:00+00:00")
        self._fail("e2", error="second", ended="2026-10-02T00:00:00+00:00")
        svc = self._planned()

        first = svc.tick()
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES ('e7', 7)")
        second = svc.tick()

        self.assertEqual(first.candidate.ref, "7")
        self.assertEqual(second.decision, "idle")
        self.assertEqual(self.fx.controller.issues, [7])
        entries = self._entries(svc)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source_ref"], "232")
        self.assertEqual(entries[0]["reason"], "second")
        detail = json.loads(entries[0]["detail"])
        self.assertEqual(detail["experiment_ids"], ["e1", "e2"])
        self.assertEqual(detail["last_reason"], "second")
        stops = [c for c in self.comments if c[0] == 232]
        self.assertEqual(len(stops), 1)
        self.assertIn("e1, e2", stops[0][1])
        self.assertIn("Last failure: second", stops[0][1])
        section = self.prompts[0].split("ISSUES OUT OF ATTEMPTS:", 1)[1]
        self.assertIn("#232 Stuck issue: second", section)
        self.assertNotIn("#232", self.prompts[0].split("OPEN ISSUES:", 1)[1].split("ISSUES OUT OF", 1)[0])

    def test_the_planner_cannot_pick_it(self):
        self._fail("e1")
        self._fail("e2")
        self.answer = json.dumps({"issue": 232, "reason": "try again"})
        svc = self._planned()

        self.assertEqual(svc.tick().decision, "idle")
        self.assertEqual(self.fx.controller.issues, [])

    def test_a_new_failed_experiment_is_reported_again(self):
        self._fail("e1", error="first")
        self._fail("e2", error="second")
        svc = self._service(issues=self.ISSUES[:1])
        svc.tick()
        self._fail("e3", error="third")

        svc.tick()

        entries = self._entries(svc)
        self.assertEqual(len(entries), 2)
        details = [json.loads(e["detail"]) for e in entries]
        self.assertCountEqual(
            [d["experiment_ids"] for d in details], [["e1", "e2"], ["e1", "e2", "e3"]]
        )
        self.assertEqual(len(self.comments), 2)

    def test_the_reason_of_a_stopped_merge_comes_from_the_adoption(self):
        self._fail("e1", ended="2026-10-01T00:00:00+00:00", error="old")
        self._fail(
            "e2", status="succeeded", adoption="failed", reason="merge\nconflict",
            ended="2026-10-02T00:00:00+00:00",
        )
        svc = self._service(issues=self.ISSUES[:1])

        svc.tick()

        self.assertEqual(json.loads(self._entries(svc)[0]["detail"])["last_reason"], "merge conflict")

    def test_a_secret_in_the_reason_does_not_reach_github(self):
        self._fail("e1")
        self._fail("e2", error="git push failed: Authorization: Bearer abcdef123456789", ended="2999-01-01T00:00:00+00:00")
        svc = self._service(issues=self.ISSUES[:1])

        svc.tick()

        self.assertNotIn("abcdef123456789", self.comments[0][1])
        self.assertNotIn("abcdef123456789", self._entries(svc)[0]["detail"])

    def test_a_failed_comment_is_tried_again_without_a_second_entry(self):
        self._fail("e1")
        self._fail("e2")
        calls = []

        def flaky(number, text):
            calls.append(number)
            if len(calls) == 1:
                raise RuntimeError("gh down")

        svc = self._service(issues=self.ISSUES[:1], commenter=flaky)

        self.assertEqual(svc.tick().decision, "idle")
        svc.tick()
        svc.tick()

        self.assertEqual(calls, [232, 232])
        entries = self._entries(svc)
        self.assertEqual(len(entries), 1)
        self.assertTrue(json.loads(entries[0]["detail"])["commented"])

    def test_a_dry_run_writes_and_comments_nothing(self):
        self._fail("e1")
        self._fail("e2")
        svc = self._planned()

        svc.candidates(record_skips=False)

        self.assertEqual(self._entries(svc), [])
        self.assertEqual(self.comments, [])
        self.assertIn("#232 Stuck issue", self.prompts[0])

    def test_an_issue_with_merged_work_is_not_reported_as_out_of_attempts(self):
        self._fail("e1")
        self._fail("e2")
        self._fail("e3", status="succeeded", adoption="adopted")
        svc = self._service(issues=self.ISSUES[:1])

        self.assertEqual(svc.tick().decision, "idle")

        self.assertEqual(self._entries(svc), [])
        self.assertEqual(self.comments, [])

    def test_one_failed_attempt_is_not_reported(self):
        self._fail("e1")
        svc = self._service(issues=self.ISSUES[:1])

        self.assertEqual(svc.tick().candidate.ref, "232")

        self.assertEqual(self._entries(svc), [])

    # --- #485: the newest integration conflict is not an attempt ---

    def _conflict(self, exp_id, reason="Integrationskonflikt: a.py", *, adoption="failed", ended):
        self._fail(exp_id, status="succeeded", adoption=adoption, reason=reason, ended=ended)

    def _attempts(self):
        # These review stops cannot resume, like a real adoption would judge them.
        self.fx.adoption.assess_failure = lambda _id: SimpleNamespace(retryable=False)
        svc = self._service(issues=self.ISSUES[:1])
        return svc._issue_attempts()

    def test_a_single_integration_conflict_leaves_the_issue_free(self):
        self._conflict("e1", ended="2026-10-01T00:00:00+00:00")

        blocked, exhausted = self._attempts()

        self.assertNotIn(232, blocked)
        self.assertNotIn(232, exhausted)

    def test_a_real_failure_plus_a_conflict_leaves_one_more_attempt(self):
        self.assertEqual(nexus_initiative._MAX_ISSUE_ATTEMPTS, 2)
        self._fail("e1", error="tests failed", ended="2026-10-01T00:00:00+00:00")
        self._conflict("e2", ended="2026-10-02T00:00:00+00:00")

        blocked, exhausted = self._attempts()
        self.assertNotIn(232, blocked)
        self.assertNotIn(232, exhausted)

        self._conflict("e3", adoption="needs_review", ended="2026-10-03T00:00:00+00:00")
        blocked, exhausted = self._attempts()
        self.assertIn(232, blocked)
        self.assertIn(232, exhausted)

    def test_two_conflicts_old_and_new_text_allow_exactly_one_more_experiment(self):
        self._conflict("e1", "Integrationskonflikt beim Cherry-Pick.", ended="2026-10-01T00:00:00+00:00")
        self._conflict("e2", "Integrationskonflikt: service.py", ended="2026-10-02T00:00:00+00:00")

        blocked, exhausted = self._attempts()
        self.assertNotIn(232, blocked)
        self.assertNotIn(232, exhausted)

        # The retry that merged main conflicts again: now the issue is done.
        self._conflict("e3", "Integrationskonflikt: nexus_initiative.py", ended="2026-10-03T00:00:00+00:00")
        blocked, exhausted = self._attempts()
        self.assertIn(232, blocked)
        self.assertEqual(exhausted[232]["experiment_ids"], ["e1", "e2", "e3"])
        self.assertEqual(exhausted[232]["last_reason"], "Integrationskonflikt: nexus_initiative.py")

    def test_a_further_conflict_is_reported_with_the_conflict_reason(self):
        self._conflict("e1", ended="2026-10-01T00:00:00+00:00")
        self._conflict("e2", ended="2026-10-02T00:00:00+00:00")
        self._conflict("e3", "Integrationskonflikt: b.py", ended="2026-10-03T00:00:00+00:00")
        self.fx.adoption.assess_failure = lambda _id: SimpleNamespace(retryable=False)
        svc = self._service(issues=self.ISSUES[:1])

        self.assertEqual(svc.tick().decision, "idle")

        entries = self._entries(svc)
        self.assertEqual(len(entries), 1)
        self.assertEqual(json.loads(entries[0]["detail"])["last_reason"], "Integrationskonflikt: b.py")
        self.assertIn("Last failure: Integrationskonflikt: b.py", self.comments[0][1])

    def test_conflicts_alone_cannot_loop_forever(self):
        for day in range(1, 10):
            self._conflict(f"e{day}", ended=f"2026-10-0{day}T00:00:00+00:00")
            blocked, _ = self._attempts()
            if 232 in blocked:
                break
        self.assertEqual(day, nexus_initiative._MAX_ISSUE_ATTEMPTS + 1)

    def test_other_review_stops_still_count(self):
        self._fail(
            "e1", status="succeeded", adoption="failed", reason="denied path: deploy/x",
            ended="2026-10-01T00:00:00+00:00",
        )
        self._fail(
            "e2", status="succeeded", adoption="needs_review", reason="Review blocked the merge",
            ended="2026-10-02T00:00:00+00:00",
        )

        blocked, exhausted = self._attempts()

        self.assertIn(232, blocked)
        self.assertEqual(exhausted[232]["last_reason"], "Review blocked the merge")

    def test_the_conflict_helper_matches_both_texts_only_for_stopped_adoptions(self):
        helper = nexus_initiative._is_integration_conflict
        self.assertTrue(helper("failed", "Integrationskonflikt: a.py"))
        self.assertTrue(helper("needs_review", "Integrationskonflikt beim Cherry-Pick."))
        self.assertFalse(helper("adopted", "Integrationskonflikt: a.py"))
        self.assertFalse(helper("failed", "merge conflict"))
        self.assertFalse(helper("failed", None))


class PipelineCrashAttemptsTests(unittest.TestCase):
    """#467: a crash of Nexus's own pipeline does not use up an issue's attempts."""

    CRASH = f"{PIPELINE_CRASH_ERROR_PREFIX}name 'phase' is not defined"
    LAST_CRASH = "2026-10-02T12:00:00+00:00"
    BEFORE = "2026-10-02T11:00:00+00:00"
    AFTER = "2026-10-02T13:00:00+00:00"
    ISSUE = {"number": 386, "title": "Crashed issue", "body": "Add unit tests."}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="nexus-crash-")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        init_db(self.db_path).close()
        GitHubWorkTrackingService(
            db_path=self.db_path, repo_root=self.tmp.name, repository="o/r", token="t"
        ).close()
        self.comments: list[tuple[int, str]] = []
        self.bodies: list[str] = []
        self.controller = MagicMock()
        self.controller.handle_issue.side_effect = lambda number, *, title, body: self.bodies.append(body)

    def _attempt(self, exp_id, *, error=None, ended=LAST_CRASH):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at, ended_at, error) "
                "VALUES (?, 'goal', 'failed', ?, ?, ?)",
                (exp_id, "2026-10-01T00:00:00+00:00", ended, error),
            )
            conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number, issue_title, "
                "branch_name, created_at, updated_at) VALUES (?, 386, 't', ?, '', '')",
                (exp_id, f"branch-{exp_id}"),
            )

    def _crashes(self):
        self._attempt("c1", error=self.CRASH, ended="2026-10-02T10:00:00+00:00")
        self._attempt("c2", error=self.CRASH)

    def _service(self, main_commit_date):
        svc = NexusInitiativeService(
            db_path=self.db_path,
            experiment_service=SimpleNamespace(),
            controller=self.controller,
            issue_lister=lambda: [dict(self.ISSUE)],
            idea_lister=lambda: [],
            issue_commenter=lambda number, text: self.comments.append((number, text)),
            main_commit_date=main_commit_date,
            daily_budget=5,
        )
        self.addCleanup(svc.close)
        return svc

    def test_one_crash_and_one_failure_leave_the_issue_a_candidate(self):
        self._attempt("c1", error=self.CRASH)
        self._attempt("f1", error="tests failed")
        svc = self._service(lambda: self.BEFORE)

        self.assertEqual(svc.tick().candidate.ref, "386")

        self.assertEqual(svc._issue_attempts(), (set(), {}))
        self.assertEqual(self.comments, [])

    def test_crashes_without_a_newer_commit_block_the_issue(self):
        self._crashes()
        svc = self._service(lambda: self.BEFORE)

        self.assertEqual(svc.tick().decision, "idle")

        self.controller.handle_issue.assert_not_called()
        self.assertEqual(svc._exhausted_issues, [
            {"number": 386, "title": "Crashed issue", "last_reason": self.CRASH}
        ])
        [(number, text)] = self.comments
        self.assertEqual(number, 386)
        self.assertIn(self.CRASH, text)
        self.assertIn("returns on its own", text)
        self.assertNotIn("a human has to decide", text)

    def test_a_newer_commit_brings_the_issue_back_with_its_history(self):
        self._crashes()
        svc = self._service(lambda: self.AFTER)

        decision = svc.tick()

        self.assertEqual(decision.candidate.ref, "386")
        self.assertEqual(svc._exhausted_issues, [])
        self.assertEqual(self.comments, [])
        self.assertIn(self.CRASH, self.bodies[0])
        self.assertTrue(self.bodies[0].endswith(svc._earlier_attempts(386)))

    def test_another_crash_after_that_commit_blocks_it_again(self):
        self._crashes()
        self._attempt("c3", error=self.CRASH, ended="2026-10-02T14:00:00+00:00")
        svc = self._service(lambda: self.AFTER)

        self.assertEqual(svc.tick().decision, "idle")

        self.controller.handle_issue.assert_not_called()

    def test_an_unreadable_commit_date_keeps_the_block(self):
        def broken():
            raise RuntimeError("no such ref")

        self._crashes()
        for date in (broken, lambda: "", lambda: "not a date", None):
            with self.subTest(date=date):
                self.assertEqual(self._service(date).tick().decision, "idle")
        self.controller.handle_issue.assert_not_called()

    def test_a_crash_without_an_end_time_keeps_the_block(self):
        self._attempt("c1", error=self.CRASH)
        self._attempt("c2", error=self.CRASH, ended=None)
        svc = self._service(lambda: self.AFTER)

        self.assertEqual(svc.tick().decision, "idle")

        self.controller.handle_issue.assert_not_called()

    def test_ordinary_failures_stay_exhausted_whatever_main_does(self):
        self._attempt("f1", error="first", ended=self.BEFORE)
        self._attempt("f2", error="second")
        svc = self._service(lambda: self.AFTER)

        self.assertEqual(svc.tick().decision, "idle")

        self.controller.handle_issue.assert_not_called()
        self.assertEqual(svc._exhausted_issues, [
            {"number": 386, "title": "Crashed issue", "last_reason": "second"}
        ])
        self.assertIn("a human has to decide", self.comments[0][1])

    def test_a_crash_quoting_an_overload_is_still_a_crash(self):
        crash = f"{PIPELINE_CRASH_ERROR_PREFIX}HTTP 429"
        self._attempt("c1", error=crash, ended="2026-10-02T10:00:00+00:00")
        self._attempt("c2", error=crash)
        svc = self._service(lambda: self.BEFORE)

        self.assertEqual(svc.tick().decision, "idle")

        self.controller.handle_issue.assert_not_called()

    def test_a_crash_stays_in_the_daily_budget(self):
        self._attempt("c1", error=self.CRASH)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE experiments SET created_at = ?", (datetime.now(timezone.utc).isoformat(),))
        svc = self._service(lambda: self.BEFORE)

        svc.tick()

        self.assertEqual(svc._spent_today(), 1)

    def test_the_commit_date_comes_from_the_local_ref(self):
        repo = self.tmp.name
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_AUTHOR_DATE": "2026-10-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": self.AFTER,
        }
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", *args], cwd=repo, env=env, check=True)

        with self.assertRaises(subprocess.CalledProcessError):
            _main_commit_date(repo)

        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
        self.assertEqual(_parse_timestamp(_main_commit_date(repo)), _parse_timestamp(self.AFTER))


if __name__ == "__main__":
    unittest.main()
