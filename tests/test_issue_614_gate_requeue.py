"""#614: a changed quality gate re-queues one durably linked, free verification run.

All dispatches go through the real ``NexusInitiativeService.tick`` and the real
``NexusControllerService`` start path (``handle_issue`` -> ``_execute`` ->
``ExperimentService.create_experiment``). Only the orchestrator's choice, the
GitHub work tracking and the experiment thread are replaced, so faults can be
injected before and after the experiment row is created.
"""

from __future__ import annotations

import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.experiment_service import ExperimentAlreadyActiveError, ExperimentService
from modules.github_work_tracking import GitHubWorkTrackingError
from modules.nexus_controller_service import NexusControllerService
from modules.nexus_initiative import GATE_RECHECK_REASON, NexusInitiativeService
from modules.task_orchestrator import ACTION_ANSWER, ACTION_NEW_EXPERIMENT, TaskDecision


ALLOWED = SimpleNamespace(allowed=True, reason="")


def verdicts(_store):
    """What gate_recheck reports for the three stored needs_review experiments."""
    return [
        {"id": "quality", "issue_number": 1, "old_reason": "quality evaluation: tests only",
         "new_verdict": "would_pass"},
        {"id": "blocked", "issue_number": 2, "old_reason": "quality evaluation: still bad",
         "new_verdict": "still_blocked"},
        {"id": "review", "issue_number": 3, "old_reason": "review blocked this change",
         "new_verdict": "would_pass"},
    ]


class FakeTracking:
    """GitHub work tracking double; ``bind_experiment`` writes the issue link."""

    def __init__(self, service: ExperimentService) -> None:
        self.service = service
        self.fail_ensure = False
        self.fail_bind = False

    def ensure_for_source(self, source):
        if self.fail_ensure:
            raise GitHubWorkTrackingError("GitHub unavailable before the experiment exists")
        number = int(source.source_id)
        return SimpleNamespace(issue_number=number, branch_name=f"nexus/{number}-issue")

    def bind_experiment(self, ref, experiment_id: str):
        if self.fail_bind:
            raise GitHubWorkTrackingError("binding failed after the experiment exists")
        self.link(experiment_id, ref.issue_number)
        return ref

    def link(self, experiment_id: str, number: int) -> None:
        with self.service._lock, self.service._conn:
            self.service._conn.execute(
                "INSERT OR REPLACE INTO github_experiment_links (experiment_id, issue_number) "
                "VALUES (?, ?)",
                (experiment_id, number),
            )


class GateRequeueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = str(root / "state.db")
        self.service = ExperimentService(
            db_path=self.db, repo_root=str(root),
            worktrees_root=str(root / "worktrees"), experiment_log_dir=str(root / "logs"),
        )
        self.addCleanup(self.service.close)
        with self.service._lock, self.service._conn:
            self.service._conn.execute(
                "CREATE TABLE github_experiment_links "
                "(experiment_id TEXT PRIMARY KEY, issue_number INTEGER)"
            )
            for exp_id, number, reason in (
                ("quality", 1, "quality evaluation: tests only"),
                ("blocked", 2, "quality evaluation: still bad"),
                ("review", 3, "review blocked this change"),
            ):
                self.service._conn.execute(
                    "INSERT INTO experiments (id, goal, status, created_at, ended_at, error) "
                    "VALUES (?, ?, 'needs_review', '2026-01-01T00:00:00+00:00', "
                    "'2026-01-01T01:00:00+00:00', ?)",
                    (exp_id, f"GitHub Issue #{number}: test", reason),
                )
                self.service._conn.execute(
                    "INSERT INTO github_experiment_links (experiment_id, issue_number) "
                    "VALUES (?, ?)",
                    (exp_id, number),
                )

        self.decisions: list[TaskDecision] = []
        self.orchestrator = SimpleNamespace(
            experiment_service=self.service,
            operations_registry=SimpleNamespace(repo_root=root),
            task_store=MagicMock(),
            run=self._decide,
        )
        self.tracking = FakeTracking(self.service)
        self.controller = NexusControllerService(self.orchestrator, github_work_tracking=self.tracking)
        starter = patch.object(self.controller, "_start_experiment_thread")
        self.started = starter.start()
        self.addCleanup(starter.stop)
        self.next_action = ACTION_NEW_EXPERIMENT
        self.orchestrator_raises: Exception | None = None

    def _decide(self, content, **_kwargs):
        if self.orchestrator_raises is not None:
            raise self.orchestrator_raises
        decision = TaskDecision(action=self.next_action)
        self.decisions.append(decision)
        return decision

    def initiative(self, *, daily_budget: int = 0) -> NexusInitiativeService:
        svc = NexusInitiativeService(
            db_path=self.db, experiment_service=self.service, controller=self.controller,
            issue_lister=lambda: [
                {"number": n, "title": f"issue {n}", "body": "", "state": "open"}
                for n in (1, 2, 3)
            ],
            idea_lister=lambda: [], adoption_service=None, daily_budget=daily_budget,
        )
        self.addCleanup(svc.close)
        return svc

    def tick(self, initiative: NexusInitiativeService, **patches):
        """One real tick; normal candidates are empty so only rechecks can run."""
        with ExitStack() as stack:
            stack.enter_context(patch("modules.nexus_initiative.gate_recheck", verdicts))
            stack.enter_context(
                patch.object(initiative, "_execution_allowed", return_value=ALLOWED)
            )
            stack.enter_context(patch.object(initiative, "candidates", return_value=[]))
            for name, value in patches.items():
                stack.enter_context(patch.object(initiative, name, return_value=value))
            return initiative.tick()

    def rechecks(self, source: str = "quality") -> list[str]:
        with self.service._lock:
            rows = self.service._conn.execute(
                "SELECT id FROM experiments WHERE rechecked_experiment_id = ?", (source,)
            ).fetchall()
        return [row[0] for row in rows]

    def all_ids(self) -> set[str]:
        with self.service._lock:
            return {row[0] for row in self.service._conn.execute("SELECT id FROM experiments")}

    def finish(self, experiment_id: str, error: str = "quality evaluation: failed again") -> None:
        self.service._force_status(
            experiment_id, "needs_review", ended_at="2026-01-02T00:00:00+00:00", error=error,
        )

    # -- the main case -------------------------------------------------------

    def test_only_the_quality_block_that_would_pass_is_requeued_once(self):
        initiative = self.initiative()
        before = initiative._issue_attempt_counts()
        self.assertEqual(before, {1: 1, 2: 1, 3: 1})
        ids_before = self.all_ids()

        decision = self.tick(initiative)

        self.assertEqual(decision.decision, "dispatched")
        self.assertEqual(decision.candidate.ref, "1")
        self.assertEqual(decision.candidate.reason, GATE_RECHECK_REASON)
        created = self.all_ids() - ids_before
        self.assertEqual(len(created), 1)
        new_id = created.pop()
        new = self.service.get_experiment(new_id)
        self.assertEqual(new.rechecked_experiment_id, "quality")
        self.assertEqual((new.source_type, new.source_id), ("issue", "1"))
        self.assertIn("rechecked_experiment_id=quality",
                      self.service.list_activity(experiment_id=new_id)[-1]["message"])
        self.assertEqual(self.started.call_count, 1)
        # still_blocked and review-blocked sources got nothing
        self.assertEqual(self.rechecks("blocked"), [])
        self.assertEqual(self.rechecks("review"), [])
        # the stored source is not touched: the gate stays independent of it
        self.assertEqual(self.service.get_experiment("quality").status, "needs_review")

        # The recheck run ends badly; it is not an attempt.
        self.finish(new_id)
        self.assertEqual(initiative._issue_attempt_counts(), before)

        # Second tick with Nexus free again (not just the busy guard): nothing queued.
        ids_after = self.all_ids()
        self.assertEqual(self.tick(initiative).decision, "idle")
        self.assertEqual(self.all_ids(), ids_after)
        self.assertEqual(self.rechecks(), [new_id])

    def test_recheck_run_still_blocks_the_issue_while_it_works(self):
        initiative = self.initiative()
        self.tick(initiative)
        new_id = self.rechecks()[0]
        self.service._force_status(new_id, "implementing")
        blocked, _ = initiative._issue_attempts()
        self.assertIn(1, blocked)
        self.service._force_status(new_id, "succeeded", ended_at="2026-01-02T00:00:00+00:00")
        blocked, _ = initiative._issue_attempts()
        self.assertIn(1, blocked)
        self.assertEqual(initiative._issue_attempt_counts().get(1), 1)

    def test_store_refuses_a_second_run_for_the_same_source(self):
        first = self.service.create_experiment(
            "GitHub Issue #1: recheck", source_type="issue", source_id="1",
            rechecked_experiment_id="quality",
        )
        self.finish(first.id)
        with self.assertRaises(ExperimentAlreadyActiveError):
            self.service.create_experiment(
                "GitHub Issue #1: recheck again", source_type="issue", source_id="1",
                rechecked_experiment_id="quality",
            )
        with self.assertRaises(ValueError):  # a review block is no quality-gate source
            self.service.create_experiment(
                "GitHub Issue #3: x", source_type="issue", source_id="3",
                rechecked_experiment_id="review",
            )

    # -- #418: work already running ----------------------------------------

    def test_running_experiment_for_the_issue_suppresses_requeue(self):
        with self.service._lock, self.service._conn:
            self.service._conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at) VALUES "
                "('running', 'GitHub Issue #1: live', 'implementing', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
        initiative = self.initiative()
        ids_before = self.all_ids()
        self.assertEqual(self.tick(initiative).decision, "busy")
        self.assertEqual(self.all_ids(), ids_before)
        # Even past the busy guard, the per-issue guard refuses the start and
        # leaves the source free.
        self.assertEqual(self.tick(initiative, busy_reason=None).decision, "dispatched")
        self.assertEqual(self.all_ids(), ids_before)
        self.assertEqual(self.rechecks(), [])
        self.started.assert_not_called()

    # -- daily budget --------------------------------------------------------

    def test_recheck_waits_for_the_daily_budget(self):
        initiative = self.initiative(daily_budget=1)
        ids_before = self.all_ids()
        decision = self.tick(initiative, _spent_today=1)
        self.assertEqual(decision.decision, "budget_exhausted")
        self.assertEqual(self.all_ids(), ids_before)
        self.assertEqual(self.tick(initiative, _spent_today=0).decision, "dispatched")
        self.assertEqual(len(self.rechecks()), 1)

    # -- fault injection on the real start path ------------------------------

    def test_no_start_leaves_source_free(self):
        initiative = self.initiative()
        self.next_action = ACTION_ANSWER  # Nexus chose not to run an experiment
        self.assertEqual(self.tick(initiative).decision, "dispatched")
        self.assertEqual(self.rechecks(), [])
        self.started.assert_not_called()
        # The claim was never taken: a later tick may still re-queue it.
        self.next_action = ACTION_NEW_EXPERIMENT
        self.assertEqual(self.tick(initiative).decision, "dispatched")
        self.assertEqual(len(self.rechecks()), 1)

    def test_failure_before_creation_does_not_consume_source(self):
        initiative = self.initiative()
        before = initiative._issue_attempt_counts()
        for fault in ("orchestrator", "tracking"):
            with self.subTest(fault=fault):
                self.orchestrator_raises = (
                    RuntimeError("orchestration failed") if fault == "orchestrator" else None
                )
                self.tracking.fail_ensure = fault == "tracking"
                ids_before = self.all_ids()
                self.assertEqual(self.tick(initiative).decision, "error")
                self.assertEqual(self.all_ids(), ids_before)
                self.assertEqual(self.rechecks(), [])
                self.assertEqual(initiative._issue_attempt_counts(), before)
        self.orchestrator_raises = None
        self.tracking.fail_ensure = False
        self.assertEqual(self.tick(initiative).decision, "dispatched")
        self.assertEqual(len(self.rechecks()), 1)

    def test_failure_after_creation_keeps_link_and_is_not_an_attempt(self):
        initiative = self.initiative()
        before = initiative._issue_attempt_counts()
        self.tracking.fail_bind = True
        ids_before = self.all_ids()

        self.assertEqual(self.tick(initiative).decision, "error")

        created = self.all_ids() - ids_before
        self.assertEqual(len(created), 1)
        new_id = created.pop()
        self.assertEqual(self.rechecks(), [new_id])
        self.assertEqual(self.service.get_experiment(new_id).rechecked_experiment_id, "quality")
        self.assertIn("rechecked_experiment_id=quality",
                      self.service.list_activity(experiment_id=new_id)[-1]["message"])
        self.started.assert_not_called()

        # Even once the run is linked to the issue later and ends, it is free.
        self.tracking.link(new_id, 1)
        self.finish(new_id)
        self.assertEqual(initiative._issue_attempt_counts(), before)
        # The source is consumed: no second recheck.
        self.tracking.fail_bind = False
        ids_after = self.all_ids()
        self.assertEqual(self.tick(initiative).decision, "idle")
        self.assertEqual(self.all_ids(), ids_after)


if __name__ == "__main__":
    unittest.main()
