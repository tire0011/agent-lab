"""Tests for Issue #136: a persisted adoption failure is not always final.

Experiment ``3946adc3...`` (#115 / PR #116) failed adoption on 2026-09-06
because adoption re-ran the whole local suite and that run timed out after
~11 minutes. #134 then removed the local suite as a mandatory gate in
favour of exact-commit GitHub CI - but the stale ``failed`` row survived
the fix and made every later retry end in ``needs_review`` without the
adoption ever being re-evaluated.

The rule under test is deliberately narrow: a recorded *test verdict*
stays terminal forever, while a failure that never reached a verdict is
superseded only when authoritative CI for the exact verified checkpoint is
green now.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.experiment_adoption_service import (
    STATUS_ADOPTED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    _MAX_STALE_RETRIES,
    StaleFailureAssessment,
)
from modules.github_work_tracking import GitHubCIVerification
from modules.task_finalization import (
    PHASE_MERGING,
    PHASE_NEEDS_REVIEW,
    TaskFinalizationCoordinator,
)
from tests.test_experiment_adoption_service import AdoptionRepoFixture

# The historical row from the issue: no exit code, output that only says
# the command never returned, and the German reason adoption writes when
# the tests could not be *completed* (as opposed to having failed).

# modules.experiment_adoption_service was split into modules/experiment_adoption_parts/
# (#574): a patched name is looked up in the parts module that uses it, so one mock
# is installed in every parts module that looks the name up at call time.
_ADOPTION_LOOKUPS = {
    "run_verification_test_monitored": (
        "modules.experiment_adoption_parts.pipeline",
    ),

}


@contextmanager
def _patch_adoption(name: str, **kwargs):
    mock = MagicMock(**kwargs)
    with ExitStack() as stack:
        for module in _ADOPTION_LOOKUPS[name]:
            stack.enter_context(patch(f"{module}.{name}", new=mock))
        yield mock

def _wait_for_thread(service, experiment_id, timeout=None):
    thread = service._threads.get(experiment_id)
    if thread is not None:
        thread.join(timeout)

_TIMEOUT_REASON = "Die Integrationstests konnten nicht erfolgreich abgeschlossen werden."
_VERDICT_REASON = "Die Integrationstests sind fehlgeschlagen."


class StaleFailureAssessmentTests(unittest.TestCase):
    """Which persisted failures current evidence may overrule."""

    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)
        self.experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        self.checkpoint_sha = "28399a0ad8dfca091d948b01b788fd6280b8e12b"

    def _historical_failure(self, **overrides) -> None:
        """Recreate the persisted adoption row exactly as the issue reports it."""
        self.fx.create_experiment(
            experiment_id=self.experiment_id, changed_files=["app.py"]
        )
        ref = self.fx.github_work_tracking.for_experiment(self.experiment_id)
        ref.checkpoint_sha = self.checkpoint_sha
        self.fx.github_work_tracking._persist_experiment(ref)

        fields = {
            "status": STATUS_FAILED,
            "reason": _TIMEOUT_REASON,
            "test_exit_code": None,
            "test_output": "[test command timed out]",
            "experiment_commit_sha": "d757a26a8778135bacf7c1008f9db0eda1e127a1",
            "started_at": "2026-09-06T15:50:20.400744+00:00",
            "ended_at": "2026-09-06T16:01:36.571326+00:00",
        }
        fields.update(overrides)
        conn = sqlite3.connect(self.fx.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO experiment_adoptions (
                        id, experiment_id, status, started_at, ended_at,
                        experiment_commit_sha, target_branch, test_exit_code,
                        test_output, reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'main', ?, ?, ?, ?, ?)
                    """,
                    (
                        "c617b97314ed4da98c539be99130c6c0",
                        self.experiment_id,
                        fields["status"],
                        fields["started_at"],
                        fields["ended_at"],
                        fields["experiment_commit_sha"],
                        fields["test_exit_code"],
                        fields["test_output"],
                        fields["reason"],
                        fields["started_at"],
                        fields["ended_at"],
                    ),
                )
        finally:
            conn.close()

    def _green_ci(self) -> GitHubCIVerification:
        return GitHubCIVerification(
            status="passed",
            commit_sha=self.checkpoint_sha,
            conclusion="success",
            run_id=33898643539,
        )

    def test_local_timeout_is_superseded_by_green_exact_commit_ci(self):
        self._historical_failure()

        with patch.object(
            self.fx.github_work_tracking, "wait_for_ci", return_value=self._green_ci()
        ):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertTrue(assessment.retryable)
        self.assertEqual(assessment.category, "superseded_local_verification")
        self.assertEqual(assessment.previous_reason, _TIMEOUT_REASON)
        self.assertEqual(assessment.evidence_commit_sha, self.checkpoint_sha)

    def test_a_recorded_test_verdict_stays_terminal_even_with_green_ci(self):
        """The tests ran and rejected the change; no later evidence overrules that."""
        self._historical_failure(
            reason=_VERDICT_REASON, test_exit_code=1, test_output="FAILED (failures=3)"
        )

        with patch.object(
            self.fx.github_work_tracking, "wait_for_ci", return_value=self._green_ci()
        ):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "test_verdict")

    def test_a_timeout_without_ci_evidence_is_not_retryable(self):
        self._historical_failure()
        unavailable = GitHubCIVerification(
            status="unavailable", commit_sha="", reason="no run for this commit"
        )

        with patch.object(
            self.fx.github_work_tracking, "wait_for_ci", return_value=unavailable
        ):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "no_ci_evidence")

    def test_ci_for_a_different_commit_does_not_count(self):
        """Green CI for some *other* commit of the experiment proves nothing."""
        self._historical_failure()
        other = GitHubCIVerification(
            status="passed", commit_sha="a" * 40, conclusion="success", run_id=7
        )

        with patch.object(self.fx.github_work_tracking, "wait_for_ci", return_value=other):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "no_ci_evidence")

    def test_an_interrupted_adoption_is_assessed_like_a_no_verdict_failure(self):
        self._historical_failure(status=STATUS_INTERRUPTED, reason="interrupted")

        with patch.object(
            self.fx.github_work_tracking, "wait_for_ci", return_value=self._green_ci()
        ):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertTrue(assessment.retryable)

    def test_a_non_failed_adoption_is_never_stale(self):
        self._historical_failure(status=STATUS_ADOPTED, reason="adopted")

        assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "not_failed")

    def test_a_manual_needs_review_is_never_stale(self):
        """#171 assesses needs_review too, but only a named blocker is retryable."""
        self._historical_failure(status=STATUS_NEEDS_REVIEW, reason="needs a human")

        assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "manual_review")

    def test_the_stale_retry_budget_is_bounded(self):
        self._historical_failure()
        conn = sqlite3.connect(self.fx.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE experiment_adoptions SET stale_retry_attempts=? WHERE experiment_id=?",
                    (_MAX_STALE_RETRIES, self.experiment_id),
                )
        finally:
            conn.close()

        with patch.object(
            self.fx.github_work_tracking, "wait_for_ci", return_value=self._green_ci()
        ):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "budget_exhausted")

    def test_ordinary_retries_do_not_consume_the_evidence_budget(self):
        """Review finding on PR #137.

        ``stale_retry_attempts`` bounds how often *changed evidence* may
        reopen the lifecycle. If ordinary retries spent it too, two normal
        finalization retries (which never consult the assessment, because
        they are still inside their own budget) would exhaust it before any
        evidence had changed - and the one thing #136 exists to allow would
        become unreachable.
        """
        self._historical_failure()

        # Two ordinary retries: no assessment passed, so no evidence was
        # claimed. Each one fails again without a verdict.
        for _ in range(2):
            with patch.object(
                self.fx.adoption_service, "_run_adoption_pipeline"
            ) as pipeline:
                pipeline.side_effect = lambda *a, **k: self.fx.adoption_service._set_status(
                    self.experiment_id,
                    STATUS_FAILED,
                    ended_at="2026-09-08T00:00:00+00:00",
                    reason=_TIMEOUT_REASON,
                    test_exit_code=None,
                    test_output="[test command timed out]",
                )
                self.fx.adoption_service.start_adoption(self.experiment_id)
                _wait_for_thread(self.fx.adoption_service, self.experiment_id, timeout=5)

        self.assertEqual(
            self.fx.adoption_service.get(self.experiment_id).stale_retry_attempts, 0
        )

        # Evidence only turns green now. The stale retry must still be open.
        with patch.object(
            self.fx.github_work_tracking, "wait_for_ci", return_value=self._green_ci()
        ):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertTrue(assessment.retryable, assessment.detail)
        self.assertEqual(assessment.category, "superseded_local_verification")

        # Every ordinary retry still archived the failure it replaced, so
        # nothing was lost by not counting them.
        self.assertEqual(len(self.fx.adoption_service.failure_history(self.experiment_id)), 2)

    def test_only_an_evidence_granted_retry_spends_the_budget(self):
        self._historical_failure()
        green = self._green_ci()

        with (
            patch.object(self.fx.github_work_tracking, "wait_for_ci", return_value=green),
            _patch_adoption("run_verification_test_monitored"),
        ):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)
            self.fx.adoption_service.start_adoption(
                self.experiment_id, superseded=assessment
            )
            adoption = self.fx.wait_terminal(self.experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.stale_retry_attempts, 1)
        # The row's own `reason` has moved on to the success message by now,
        # so why the retry was allowed lives in the archive.
        history = self.fx.adoption_service.failure_history(self.experiment_id)
        self.assertIn("on fresh evidence", history[-1]["superseded_by"])

    def test_a_non_retryable_assessment_does_not_spend_the_budget(self):
        """A caller may hand back a refusal; that is still an ordinary retry."""
        self._historical_failure()
        refusal = StaleFailureAssessment(False, "no_ci_evidence", "nothing green")

        with patch.object(self.fx.adoption_service, "_run_adoption_pipeline"):
            self.fx.adoption_service.start_adoption(
                self.experiment_id, superseded=refusal
            )
            _wait_for_thread(self.fx.adoption_service, self.experiment_id, timeout=5)

        self.assertEqual(
            self.fx.adoption_service.get(self.experiment_id).stale_retry_attempts, 0
        )

    def test_unreadable_ci_evidence_does_not_raise_into_the_caller(self):
        self._historical_failure()

        with patch.object(
            self.fx.github_work_tracking, "wait_for_ci", side_effect=RuntimeError("boom")
        ):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "evidence_unavailable")

    def test_retrying_reuses_the_adoption_identity_and_keeps_the_failure_auditable(self):
        self._historical_failure()
        before = self.fx.adoption_service.get(self.experiment_id)

        with (
            patch.object(
                self.fx.github_work_tracking, "wait_for_ci", return_value=self._green_ci()
            ),
            _patch_adoption("run_verification_test_monitored") as local_tests,
        ):
            self.fx.adoption_service.start_adoption(self.experiment_id)
            adoption = self.fx.wait_terminal(self.experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        # Same identity, no duplicate row.
        self.assertEqual(adoption.id, before.id)
        rows = sqlite3.connect(self.fx.db_path).execute(
            "SELECT COUNT(*) FROM experiment_adoptions WHERE experiment_id=?",
            (self.experiment_id,),
        ).fetchone()[0]
        self.assertEqual(rows, 1)
        # The 11-minute local suite is not re-run; CI evidence carries it.
        local_tests.assert_not_called()

        history = self.fx.adoption_service.failure_history(self.experiment_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["reason"], _TIMEOUT_REASON)
        self.assertEqual(history[0]["status"], STATUS_FAILED)
        self.assertEqual(history[0]["adoption_id"], before.id)
        self.assertIn("[test command timed out]", "\n".join(history[0]["test_output_lines"]))

    def test_the_retry_is_counted_and_persisted_before_the_worker_runs(self):
        """The transition must be durable before anything can observe it."""
        self._historical_failure()
        observed: dict[str, object] = {}

        def _capture(*args, **kwargs):
            # Read the row the way a fresh process would: its own connection.
            row = sqlite3.connect(self.fx.db_path).execute(
                "SELECT status, stale_retry_attempts, reason FROM experiment_adoptions "
                "WHERE experiment_id=?",
                (self.experiment_id,),
            ).fetchone()
            observed["row"] = row
            raise RuntimeError("stop before the pipeline does any work")

        granted = StaleFailureAssessment(
            True, "superseded_local_verification", "green exact-commit CI"
        )
        with patch.object(
            self.fx.adoption_service, "_run_adoption_pipeline", side_effect=_capture
        ):
            self.fx.adoption_service.start_adoption(self.experiment_id, superseded=granted)
            _wait_for_thread(self.fx.adoption_service, self.experiment_id, timeout=5)

        status, attempts, reason = observed["row"]
        self.assertEqual(status, "queued")
        self.assertEqual(attempts, 1)
        self.assertIn("superseded", reason)
        # The archived failure is durable too, not only in memory.
        self.assertEqual(len(self.fx.adoption_service.failure_history(self.experiment_id)), 1)


class LegacyDatabaseMigrationTests(unittest.TestCase):
    """The fix has to work on the database that already holds the stale row.

    The acceptance criterion is explicitly "without manually deleting
    SQLite state", so the migration must add the counter and the history
    table to a database created before either existed, in place.
    """

    _LEGACY_SCHEMA = """
        CREATE TABLE experiment_adoptions (
            id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NULL, ended_at TEXT NULL, experiment_commit_sha TEXT NULL,
            integrated_commit_sha TEXT NULL, target_branch TEXT NOT NULL DEFAULT 'main',
            expected_files_json TEXT NULL, actual_files_json TEXT NULL,
            test_exit_code INTEGER NULL, test_output TEXT NULL, reason TEXT NULL,
            local_sync_pending INTEGER NOT NULL DEFAULT 0,
            diagnostic_worktree_path TEXT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(experiment_id));
    """

    def test_a_pre_136_database_is_migrated_in_place(self):
        from modules.run_store import init_db

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "legacy.db")

        conn = sqlite3.connect(path)
        with conn:
            conn.executescript(self._LEGACY_SCHEMA)
            conn.execute(
                "INSERT INTO experiment_adoptions (id, experiment_id, status, reason, "
                "test_output, created_at, updated_at) VALUES (?, ?, 'failed', ?, ?, 't', 't')",
                ("c617", "3946", _TIMEOUT_REASON, "[test command timed out]"),
            )
        conn.close()

        init_db(path)
        init_db(path)  # migrations must stay idempotent

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(experiment_adoptions)")}
        self.assertIn("recovery_attempts", columns)
        self.assertIn("stale_retry_attempts", columns)

        row = conn.execute("SELECT * FROM experiment_adoptions").fetchone()
        self.assertEqual(row["reason"], _TIMEOUT_REASON)
        self.assertEqual(row["stale_retry_attempts"], 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM experiment_adoption_failures").fetchone()[0], 0
        )


class FinalizationStaleRetryTests(unittest.TestCase):
    """The coordinator must stop reporting `retries exhausted` on stale evidence."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "nexus.db")
        self.approval = SimpleNamespace(id="9406d03b367b4c8e82541d55ea97958d", task_body="Task")
        self.experiment = SimpleNamespace(id="3946adc3485049198d02bdc0a2e658fc")

    def _coordinator(self, adoption_service):
        coordinator = TaskFinalizationCoordinator(
            experiment_service=MagicMock(),
            github_work_tracking=MagicMock(),
            db_path=self.db_path,
            repo_root=self.tmp.name,
            adoption_service=adoption_service,
            restart_runner=lambda services: None,
            live_validator=lambda approval, experiment: {"summary": "live ok"},
        )
        self.addCleanup(coordinator.close)
        return coordinator

    def _exhausted_state(self, coordinator):
        state = coordinator._get_or_create(str(self.approval.id), str(self.experiment.id))
        state.retry_count = coordinator.max_retries
        state.last_error = (
            f"finalization retries exhausted after adoption status=failed: {_TIMEOUT_REASON}"
        )
        coordinator._save(state)

    def _service(self, *, retryable: bool):
        adoption = SimpleNamespace(
            status=STATUS_FAILED,
            reason=_TIMEOUT_REASON,
            integrated_commit_sha=None,
            local_sync_pending=False,
        )
        assessment = (
            StaleFailureAssessment(True, "superseded_local_verification", "green exact-commit CI")
            if retryable
            else StaleFailureAssessment(False, "test_verdict", "the tests rejected the change")
        )

        class _Service:
            starts = 0
            superseded_arguments: list = []

            def get(self, experiment_id):
                return adoption

            def assess_failure(self, experiment_id):
                return assessment

            def start_adoption(inner, experiment_id, *, superseded=None):
                inner.starts += 1
                inner.superseded_arguments = [*inner.superseded_arguments, superseded]
                adoption.status = "queued"
                return adoption

        return _Service()

    def test_an_exhausted_budget_reopens_when_the_failure_is_superseded(self):
        service = self._service(retryable=True)
        coordinator = self._coordinator(service)
        self._exhausted_state(coordinator)

        result = coordinator.advance(self.approval, self.experiment)

        self.assertEqual(result.status, "running")
        self.assertEqual(result.phase, PHASE_MERGING)
        self.assertEqual(service.starts, 1)
        # The assessment is handed back, which is what authorizes spending
        # one unit of the evidence budget.
        self.assertTrue(service.superseded_arguments[-1].retryable)
        state = coordinator._get(str(self.approval.id))
        self.assertEqual(state.retry_count, 1)
        self.assertIsNone(state.last_error)

    def test_an_exhausted_budget_stays_closed_for_a_still_valid_failure(self):
        service = self._service(retryable=False)
        coordinator = self._coordinator(service)
        self._exhausted_state(coordinator)

        result = coordinator.advance(self.approval, self.experiment)

        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.phase, PHASE_NEEDS_REVIEW)
        self.assertEqual(service.starts, 0)
        self.assertIn("retries exhausted", result.summary)

    def test_an_unexhausted_budget_never_needs_the_assessment(self):
        """Ordinary retries keep working exactly as before (#128)."""
        service = self._service(retryable=False)
        coordinator = self._coordinator(service)

        result = coordinator.advance(self.approval, self.experiment)

        self.assertEqual(result.status, "running")
        self.assertEqual(service.starts, 1)

    def test_an_adoption_service_without_the_assessment_keeps_the_old_behaviour(self):
        adoption = SimpleNamespace(
            status=STATUS_FAILED,
            reason=_TIMEOUT_REASON,
            integrated_commit_sha=None,
            local_sync_pending=False,
        )

        class _Legacy:
            def get(self, experiment_id):
                return adoption

            def start_adoption(self, experiment_id):
                raise AssertionError("must not restart without a fresh budget")

        coordinator = self._coordinator(_Legacy())
        self._exhausted_state(coordinator)

        result = coordinator.advance(self.approval, self.experiment)

        self.assertEqual(result.status, "needs_review")


if __name__ == "__main__":
    unittest.main()
