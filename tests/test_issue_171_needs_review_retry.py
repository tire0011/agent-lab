"""Tests for Issue #171: a persisted ``needs_review`` is not always final.

Experiment ``3946adc3...`` (#115 / PR #116) parked adoption in
``needs_review`` because the GitHub lifecycle was unconfigured on the VM.
The configuration was corrected and ``agent-api.service`` restarted, but
re-running the experiment kept returning the recorded
``Configure GITHUB_TOKEN/GH_TOKEN`` reason: ``needs_review`` was terminal
in ``TaskFinalizationCoordinator.advance()`` and no readiness check ever
ran again. The persisted text therefore read like a current verdict.

The rule under test mirrors #136 for reviews: only a review that names a
re-evaluable blocker may be resumed, only when that blocker demonstrably
no longer holds, only within the same bounded evidence budget, and always
on the same adoption record.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.experiment_adoption_service import (
    REVIEW_BLOCKER_GITHUB_LIFECYCLE,
    STATUS_ADOPTED,
    STATUS_NEEDS_REVIEW,
    _MAX_STALE_RETRIES,
    ExperimentAdoption,
    ExperimentAdoptionError,
    ExperimentAdoptionService,
    StaleFailureAssessment,
)
from modules.task_finalization import (
    PHASE_MERGING,
    PHASE_NEEDS_REVIEW,
    TaskFinalizationCoordinator,
)
from tests.test_experiment_adoption_service import AdoptionRepoFixture

_MANUAL_REASON = "Das Qualitäts-Gate hat die Änderung abgelehnt."


class NeedsReviewBlockerAssessmentTests(unittest.TestCase):
    """Which persisted reviews may be re-evaluated at all."""

    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture(github_configured=False)
        self.addCleanup(self.fx.close)
        self.experiment_id = "71" * 16
        self.fx.create_experiment(
            experiment_id=self.experiment_id, changed_files=["app.py"]
        )

    def _blocked_review(self) -> ExperimentAdoption:
        """Run adoption against the unconfigured lifecycle, as the VM did."""
        self.fx.adoption_service.start_adoption(self.experiment_id)
        return self.fx.wait_terminal(self.experiment_id)

    def _make_lifecycle_ready(self) -> None:
        """What correcting the VM configuration and restarting the service does."""
        self.fx.adoption_service.github_work_tracking = self.fx.github_seed_tracking

    def test_a_lifecycle_block_is_recorded_as_a_named_blocker(self):
        adoption = self._blocked_review()

        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertEqual(adoption.review_blocker, REVIEW_BLOCKER_GITHUB_LIFECYCLE)
        self.assertIn("GitHub lifecycle", adoption.reason or "")

    def test_the_block_stays_terminal_while_the_lifecycle_is_still_unconfigured(self):
        self._blocked_review()

        assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "review_blocker_persists")

    def test_the_block_is_retryable_once_the_lifecycle_is_ready(self):
        self._blocked_review()
        self._make_lifecycle_ready()

        assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertTrue(assessment.retryable, assessment.detail)
        self.assertEqual(assessment.category, "review_blocker_cleared")
        self.assertIn("GitHub lifecycle", assessment.previous_reason or "")

    def test_a_review_about_the_change_itself_is_never_retried(self):
        """An unrelated/manual review names no blocker, so nothing to re-check."""
        self._blocked_review()
        self.fx.adoption_service._set_status(
            self.experiment_id,
            STATUS_NEEDS_REVIEW,
            reason=_MANUAL_REASON,
            review_blocker=None,
        )
        self._make_lifecycle_ready()

        assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "manual_review")

    def test_a_row_written_before_this_fix_is_still_recognized(self):
        """The stale row on the VM predates the column; only its text survives."""
        self._blocked_review()
        conn = sqlite3.connect(self.fx.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE experiment_adoptions SET review_blocker=NULL WHERE experiment_id=?",
                    (self.experiment_id,),
                )
        finally:
            conn.close()
        self._make_lifecycle_ready()

        assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertTrue(assessment.retryable, assessment.detail)
        self.assertEqual(assessment.category, "review_blocker_cleared")

    def test_the_evidence_budget_bounds_the_retries(self):
        self._blocked_review()
        conn = sqlite3.connect(self.fx.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE experiment_adoptions SET stale_retry_attempts=? WHERE experiment_id=?",
                    (_MAX_STALE_RETRIES, self.experiment_id),
                )
        finally:
            conn.close()
        self._make_lifecycle_ready()

        assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "budget_exhausted")

    def test_an_operator_restart_of_a_review_still_does_nothing_on_its_own(self):
        """Without an assessment a review row stays untouched, exactly as before."""
        before = self._blocked_review()
        self._make_lifecycle_ready()

        returned = self.fx.adoption_service.start_adoption(self.experiment_id)

        self.assertEqual(returned.status, STATUS_NEEDS_REVIEW)
        self.assertEqual(returned.reason, before.reason)
        self.assertEqual(
            self.fx.adoption_service.failure_history(self.experiment_id), []
        )


class _ClearedBlockerService:
    """An adoption whose blocker has cleared but whose restart never begins."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.starts = 0
        self.adoption = SimpleNamespace(
            status=STATUS_NEEDS_REVIEW,
            reason="GitHub lifecycle ist für die Veröffentlichung nicht bereit",
            review_blocker=REVIEW_BLOCKER_GITHUB_LIFECYCLE,
            integrated_commit_sha=None,
            local_sync_pending=False,
        )

    def get(self, experiment_id):
        return self.adoption

    def assess_failure(self, experiment_id):
        return StaleFailureAssessment(
            True, "review_blocker_cleared", "the GitHub lifecycle is ready now"
        )

    def start_adoption(self, experiment_id, *, superseded=None):
        self.starts += 1
        raise self.error


class NeedsReviewRequeueBudgetTests(unittest.TestCase):
    """A cleared blocker must not be retried forever when nothing can start."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.approval = SimpleNamespace(id="approval-171", task_body="Task")
        self.experiment = SimpleNamespace(id="experiment-171")
        self.service = _ClearedBlockerService(
            ExperimentAdoptionError("Der Experiment-Worktree ist nicht mehr verfügbar.")
        )
        self.coordinator = TaskFinalizationCoordinator(
            experiment_service=MagicMock(),
            github_work_tracking=MagicMock(),
            db_path=str(Path(self.tmp.name) / "nexus.db"),
            repo_root=self.tmp.name,
            adoption_service=self.service,
            restart_runner=lambda services: None,
            live_validator=lambda approval, experiment: {"summary": "live ok"},
        )
        self.addCleanup(self.coordinator.close)

    def test_a_requeue_that_never_starts_reaches_a_human_instead_of_looping(self):
        results = [
            self.coordinator.advance(self.approval, self.experiment) for _ in range(6)
        ]

        # The restart is attempted while the budget lasts and then stops
        # for good; the loop does not renew itself on the next tick.
        self.assertLessEqual(self.service.starts, self.coordinator.max_retries + 1)
        self.assertEqual(results[-1].status, "needs_review")
        self.assertEqual(results[-1].phase, PHASE_NEEDS_REVIEW)
        self.assertIn("Worktree", results[-1].summary)
        # The review reason stays readable next to the start failure.
        self.assertIn("adoption requires review", results[-1].summary)

        before = self.service.starts
        self.coordinator.advance(self.approval, self.experiment)
        self.assertEqual(self.service.starts, before)


class NeedsReviewFinalizationResumeTests(unittest.TestCase):
    """The reproduction from the issue, end to end through the coordinator."""

    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture(github_configured=False)
        self.addCleanup(self.fx.close)
        self.experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        self.fx.create_experiment(
            experiment_id=self.experiment_id, changed_files=["app.py"]
        )
        self.approval = SimpleNamespace(
            id="9406d03b367b4c8e82541d55ea97958d", task_body="Task"
        )
        self.experiment = self.fx.experiment_service.get_experiment(self.experiment_id)
        self.coordinator = TaskFinalizationCoordinator(
            experiment_service=self.fx.experiment_service,
            github_work_tracking=self.fx.github_work_tracking,
            db_path=self.fx.db_path,
            repo_root=str(self.fx.repo),
            adoption_service=self.fx.adoption_service,
            restart_runner=lambda services: None,
            live_validator=lambda approval, experiment: {"summary": "live ok"},
        )
        self.addCleanup(self.coordinator.close)

    def _adoption_rows(self) -> int:
        conn = sqlite3.connect(self.fx.db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM experiment_adoptions WHERE experiment_id=?",
                (self.experiment_id,),
            ).fetchone()[0]
        finally:
            conn.close()

    def test_a_cleared_lifecycle_block_resumes_the_same_adoption(self):
        # 1./2. The lifecycle is unconfigured: adoption parks for review and
        # finalization reports exactly that, without inventing a retry.
        first = self.coordinator.advance(self.approval, self.experiment)
        self.assertEqual(first.phase, PHASE_MERGING)
        blocked = self.fx.wait_terminal(self.experiment_id)
        self.assertEqual(blocked.status, STATUS_NEEDS_REVIEW)

        second = self.coordinator.advance(self.approval, self.experiment)
        self.assertEqual(second.status, "needs_review")
        self.assertEqual(second.phase, PHASE_NEEDS_REVIEW)
        self.assertIn("GitHub lifecycle", second.summary)

        # 3. The VM configuration is corrected and the service restarted.
        self.fx.adoption_service.github_work_tracking = self.fx.github_seed_tracking
        self.coordinator.github_work_tracking = self.fx.github_seed_tracking

        # 4./5. Resuming the same task re-evaluates the blocker instead of
        # replaying the recorded reason, and adoption continues.
        resumed = self.coordinator.advance(self.approval, self.experiment)
        self.assertEqual(resumed.status, "running")
        self.assertEqual(resumed.phase, PHASE_MERGING)

        adopted = self.fx.wait_terminal(self.experiment_id)
        self.assertEqual(adopted.status, STATUS_ADOPTED)
        # The same adoption record, and no second experiment adoption row.
        self.assertEqual(adopted.id, blocked.id)
        self.assertEqual(self._adoption_rows(), 1)
        self.assertIsNone(adopted.review_blocker)

        # The replaced review stays auditable, and it cost one unit of the
        # bounded evidence budget.
        history = self.fx.adoption_service.failure_history(self.experiment_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], STATUS_NEEDS_REVIEW)
        self.assertIn("GitHub lifecycle", history[0]["reason"])
        self.assertEqual(history[0]["adoption_id"], blocked.id)
        self.assertEqual(adopted.stale_retry_attempts, 1)

    def test_a_manual_review_is_still_terminal_for_finalization(self):
        self.coordinator.advance(self.approval, self.experiment)
        self.fx.wait_terminal(self.experiment_id)
        self.fx.adoption_service._set_status(
            self.experiment_id,
            STATUS_NEEDS_REVIEW,
            reason=_MANUAL_REASON,
            review_blocker=None,
        )
        self.fx.adoption_service.github_work_tracking = self.fx.github_seed_tracking
        self.coordinator.github_work_tracking = self.fx.github_seed_tracking

        with patch.object(
            ExperimentAdoptionService, "start_adoption", autospec=True
        ) as start:
            result = self.coordinator.advance(self.approval, self.experiment)

        start.assert_not_called()
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.phase, PHASE_NEEDS_REVIEW)
        self.assertIn(_MANUAL_REASON, result.summary)
        self.assertEqual(
            self.fx.adoption_service.get(self.experiment_id).status,
            STATUS_NEEDS_REVIEW,
        )

    def test_a_still_blocked_review_is_reported_without_restarting_adoption(self):
        self.coordinator.advance(self.approval, self.experiment)
        blocked = self.fx.wait_terminal(self.experiment_id)

        with patch.object(
            ExperimentAdoptionService, "start_adoption", autospec=True
        ) as start:
            result = self.coordinator.advance(self.approval, self.experiment)

        start.assert_not_called()
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(
            self.fx.adoption_service.get(self.experiment_id).id, blocked.id
        )
        self.assertEqual(self._adoption_rows(), 1)


if __name__ == "__main__":
    unittest.main()
