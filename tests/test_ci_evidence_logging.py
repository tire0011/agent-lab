"""Tests for Issue #141: the CI-evidence decision path must be readable.

In the real #115 / PR #116 retry the visible logs said only:

    finalization=needs_review; finalization retries exhausted after adoption
    status=failed: Die Integrationstests konnten nicht erfolgreich abgeschlossen werden.

which reads like a generic test failure. The actual cause - no durable CI
row for the checkpoint, a live GitHub lookup, and that lookup failing for
want of credentials - was only reachable by querying SQLite and calling
internal methods by hand. These tests pin the structured events that make
that chain visible in the normal log feed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.central_logging import ExperimentLogger
from modules.experiment_adoption_service import (
    STATUS_ADOPTED,
    STATUS_FAILED,
    ExperimentAdoptionService,
)
from modules.github_work_tracking import GitHubCIVerification, GitHubWorkTrackingService
from modules.task_finalization import PHASE_NEEDS_REVIEW, TaskFinalizationCoordinator
from tests.test_experiment_adoption_service import AdoptionRepoFixture, git

_TIMEOUT_REASON = "Die Integrationstests konnten nicht erfolgreich abgeschlossen werden."


class CIEvidenceLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)
        self.experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        self.checkpoint_sha = "28399a0ad8dfca091d948b01b788fd6280b8e12b"
        self.run_id = 33898643539
        self.fx.create_experiment(experiment_id=self.experiment_id, changed_files=["app.py"])
        ref = self.fx.github_work_tracking.for_experiment(self.experiment_id)
        ref.checkpoint_sha = self.checkpoint_sha
        self.fx.github_work_tracking._persist_experiment(ref)

    # ---- helpers -------------------------------------------------------

    def _entries(self) -> list[dict]:
        """Everything the normal log feed holds for this experiment."""
        path = Path(self.fx.logs) / f"{self.experiment_id}.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _events(self, name: str) -> list[dict]:
        return [entry for entry in self._entries() if entry.get("event") == name]

    def _one(self, name: str) -> dict:
        found = self._events(name)
        self.assertEqual(
            len(found), 1, f"expected exactly one {name}, got {len(found)}"
        )
        return found[0]

    def _integration_base(self) -> str:
        return git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()

    def _green(self) -> GitHubCIVerification:
        return GitHubCIVerification(
            status="passed",
            commit_sha=self.checkpoint_sha,
            conclusion="success",
            run_id=self.run_id,
            url="https://example.invalid/actions/runs/33898643539",
        )

    def _record_green(self) -> None:
        self.fx.github_work_tracking.record_ci_verification(
            self._green(),
            experiment_id=self.experiment_id,
            base_sha=self._integration_base(),
        )

    def _stale_failed_adoption(self) -> None:
        conn = sqlite3.connect(self.fx.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO experiment_adoptions (
                        id, experiment_id, status, test_exit_code, test_output,
                        reason, target_branch, created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, 'main', 't', 't')
                    """,
                    (
                        "c617b97314ed4da98c539be99130c6c0",
                        self.experiment_id,
                        STATUS_FAILED,
                        "[test command timed out]",
                        _TIMEOUT_REASON,
                    ),
                )
        finally:
            conn.close()

    def _without_credentials(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}, clear=False):
            tracking = GitHubWorkTrackingService(
                db_path=self.fx.db_path,
                repo_root=str(self.fx.repo),
                repository="tire0011/agent-lab",
            )
        self.addCleanup(tracking.close)
        self.assertFalse(tracking.configured)
        adoption = ExperimentAdoptionService(
            experiment_service=self.fx.experiment_service,
            db_path=self.fx.db_path,
            repo_root=str(self.fx.repo),
            worktrees_root=str(self.fx.worktrees),
            lock_path=self.fx.lock_path,
            github_work_tracking=tracking,
        )
        self.addCleanup(adoption.close)
        return tracking, adoption

    # ---- persisted evidence -------------------------------------------

    def test_a_persisted_hit_is_logged_with_the_run_and_base_it_reused(self):
        self._stale_failed_adoption()
        self._record_green()
        _, adoption = self._without_credentials()

        adoption.assess_failure(self.experiment_id)

        lookup = self._one("ci_evidence_lookup")
        self.assertTrue(lookup["persisted_found"])
        self.assertEqual(lookup["checkpoint_sha"], self.checkpoint_sha)
        self.assertEqual(lookup["integration_base_sha"], self._integration_base())

        reused = self._one("ci_evidence_reused")
        self.assertEqual(reused["source"], "persisted")
        self.assertEqual(reused["ci_status"], "passed")
        self.assertEqual(reused["run_id"], self.run_id)
        self.assertEqual(reused["provider"], "github_actions")
        self.assertEqual(reused["recorded_base_sha"], self._integration_base())
        self.assertTrue(reused["base_validated"])
        self.assertTrue(reused["observed_at"])
        # A durable hit must not reach for GitHub at all.
        self.assertEqual(self._events("ci_evidence_live_fallback"), [])
        # An accepted verdict is never also reported as rejected.
        self.assertEqual(self._events("ci_evidence_rejected"), [])

    def test_a_persisted_miss_logs_the_fallback_and_its_result(self):
        self._stale_failed_adoption()
        _, adoption = self._without_credentials()

        adoption.assess_failure(self.experiment_id)

        self.assertFalse(self._one("ci_evidence_lookup")["persisted_found"])
        fallback = self._one("ci_evidence_live_fallback")
        self.assertEqual(fallback["fallback_reason"], "no_persisted_evidence")
        self.assertTrue(self._events("ci_evidence_live_result"))

    def test_missing_credentials_are_visible_as_the_fallback_result(self):
        """The whole point: a credential problem must read as itself."""
        self._stale_failed_adoption()
        _, adoption = self._without_credentials()

        adoption.assess_failure(self.experiment_id)

        result = self._one("ci_evidence_live_result")
        self.assertEqual(result["ci_status"], "unavailable")
        self.assertIn("GITHUB_TOKEN/GH_TOKEN", result["reason"])
        self.assertFalse(result["credentials_available"])

    def test_a_moved_base_is_rejected_visibly_without_debug_logging(self):
        self._stale_failed_adoption()
        self._record_green()
        recorded_base = self._integration_base()

        # Move origin/main the way any other merge would.
        (self.fx.repo / "unrelated.py").write_text("MOVED = True\n", encoding="utf-8")
        git(self.fx.repo, "add", "unrelated.py")
        git(self.fx.repo, "commit", "-m", "unrelated change on main")
        git(self.fx.repo, "push", "origin", "main")
        git(self.fx.repo, "fetch", "origin", "main")

        _, adoption = self._without_credentials()
        adoption.assess_failure(self.experiment_id)

        rejected = self._one("ci_evidence_rejected")
        self.assertEqual(rejected["rejection_reason"], "target_base_moved")
        self.assertEqual(rejected["recorded_base_sha"], recorded_base)
        self.assertEqual(rejected["integration_base_sha"], self._integration_base())
        self.assertNotEqual(rejected["recorded_base_sha"], rejected["integration_base_sha"])
        # Visible at WARNING, so it shows without turning on debug logging.
        self.assertEqual(rejected["level"], "WARNING")
        self.assertEqual(
            self._one("ci_evidence_live_fallback")["fallback_reason"], "target_base_moved"
        )

    def _record_red(self) -> None:
        self.fx.github_work_tracking.record_ci_verification(
            GitHubCIVerification(
                status="failed",
                commit_sha=self.checkpoint_sha,
                conclusion="failure",
                run_id=99,
            ),
            experiment_id=self.experiment_id,
            base_sha=self._integration_base(),
        )

    def test_a_red_persisted_verdict_is_logged_as_such(self):
        self._stale_failed_adoption()
        self._record_red()
        _, adoption = self._without_credentials()

        adoption.assess_failure(self.experiment_id)

        rejected = self._one("ci_evidence_rejected")
        self.assertEqual(rejected["rejection_reason"], "red_verdict")
        self.assertEqual(rejected["run_id"], 99)
        self.assertEqual(rejected["source"], "persisted")
        self.assertEqual(rejected["conclusion"], "failure")

    def test_a_red_verdict_is_never_also_logged_as_reused(self):
        """One piece of evidence must not be both accepted and rejected.

        `ci_evidence_reused` means an accepted, usable verdict. A red one
        is loaded the same way but supports nothing, so logging both would
        make the decision path ambiguous for exactly the automated
        diagnosis this vocabulary exists for.
        """
        self._stale_failed_adoption()
        self._record_red()
        _, adoption = self._without_credentials()

        adoption.assess_failure(self.experiment_id)

        self.assertEqual(self._events("ci_evidence_reused"), [])
        # The store still answered, so no live lookup was made.
        self.assertEqual(self._events("ci_evidence_live_fallback"), [])
        self.assertTrue(self._one("ci_evidence_lookup")["persisted_found"])

    def test_a_red_live_verdict_is_rejected_the_same_way(self):
        """`red_verdict` means one thing regardless of where it came from."""
        self._stale_failed_adoption()
        red = GitHubCIVerification(
            status="failed",
            commit_sha=self.checkpoint_sha,
            conclusion="failure",
            run_id=1234,
        )
        tracking, adoption = self._without_credentials()

        with patch.object(tracking, "wait_for_ci", return_value=red):
            adoption.assess_failure(self.experiment_id)

        rejected = self._one("ci_evidence_rejected")
        self.assertEqual(rejected["rejection_reason"], "red_verdict")
        self.assertEqual(rejected["source"], "live")
        self.assertEqual(self._events("ci_evidence_reused"), [])

    # ---- the assessment and the requeue --------------------------------

    def test_a_retryable_assessment_is_logged_with_its_evidence(self):
        self._stale_failed_adoption()
        self._record_green()
        _, adoption = self._without_credentials()

        adoption.assess_failure(self.experiment_id)

        assessment = self._one("stale_adoption_assessment")
        self.assertTrue(assessment["retryable"])
        self.assertEqual(assessment["category"], "superseded_local_verification")
        self.assertEqual(assessment["evidence_commit_sha"], self.checkpoint_sha)
        self.assertEqual(assessment["evidence_run_id"], self.run_id)
        self.assertEqual(assessment["previous_reason"], _TIMEOUT_REASON)
        self.assertEqual(assessment["stale_retry_attempts"], 0)

    def test_a_terminal_assessment_is_logged_at_warning(self):
        self._stale_failed_adoption()
        _, adoption = self._without_credentials()

        adoption.assess_failure(self.experiment_id)

        assessment = self._one("stale_adoption_assessment")
        self.assertFalse(assessment["retryable"])
        self.assertEqual(assessment["category"], "no_ci_evidence")
        self.assertEqual(assessment["level"], "WARNING")

    def test_the_decision_is_logged_once_not_at_every_return_path(self):
        self._stale_failed_adoption()
        _, adoption = self._without_credentials()

        adoption.assess_failure(self.experiment_id)
        self.assertEqual(len(self._events("stale_adoption_assessment")), 1)

    def test_the_requeue_names_the_reused_adoption_and_the_counter(self):
        self._stale_failed_adoption()
        self._record_green()

        with patch("modules.experiment_adoption_service.run_verification_test"):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)
            self.fx.adoption_service.start_adoption(
                self.experiment_id, superseded=assessment
            )
            adoption = self.fx.wait_terminal(self.experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED, adoption.reason)
        requeued = self._one("stale_adoption_requeued")
        self.assertEqual(requeued["adoption_id"], "c617b97314ed4da98c539be99130c6c0")
        self.assertEqual(requeued["previous_status"], STATUS_FAILED)
        self.assertEqual(requeued["previous_reason"], _TIMEOUT_REASON)
        self.assertTrue(requeued["on_fresh_evidence"])
        self.assertEqual(requeued["stale_retry_attempts"], 1)

    def test_the_adoption_pipeline_tags_its_evidence_events_with_its_own_phase(self):
        """The same events fire twice per retry; the phase says which is which."""
        self._stale_failed_adoption()
        self._record_green()

        with patch("modules.experiment_adoption_service.run_verification_test"):
            assessment = self.fx.adoption_service.assess_failure(self.experiment_id)
            self.fx.adoption_service.start_adoption(
                self.experiment_id, superseded=assessment
            )
            self.fx.wait_terminal(self.experiment_id)

        phases = {entry["phase"] for entry in self._events("ci_evidence_lookup")}
        self.assertEqual(phases, {"assessment", "adoption"})

    # ---- secrets -------------------------------------------------------

    def test_no_credential_reaches_the_log(self):
        self._stale_failed_adoption()
        secret = "ghp-not-a-real-token-value-138"
        with patch.dict(os.environ, {"GITHUB_TOKEN": secret}, clear=False):
            tracking = GitHubWorkTrackingService(
                db_path=self.fx.db_path,
                repo_root=str(self.fx.repo),
                repository="tire0011/agent-lab",
            )
            self.addCleanup(tracking.close)
            adoption = ExperimentAdoptionService(
                experiment_service=self.fx.experiment_service,
                db_path=self.fx.db_path,
                repo_root=str(self.fx.repo),
                worktrees_root=str(self.fx.worktrees),
                lock_path=self.fx.lock_path,
                github_work_tracking=tracking,
            )
            self.addCleanup(adoption.close)
            with patch.object(
                tracking,
                "wait_for_ci",
                return_value=GitHubCIVerification(
                    status="unavailable",
                    commit_sha="",
                    reason=f"GitHub HTTP 401 for token {secret}",
                ),
            ):
                adoption.assess_failure(self.experiment_id)

        raw = (Path(self.fx.logs) / f"{self.experiment_id}.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)

    # ---- the acceptance criterion --------------------------------------

    def test_the_115_failure_is_diagnosable_from_the_log_feed_alone(self):
        """No SQLite queries, no internal calls: the chain is in the feed."""
        self._stale_failed_adoption()
        _, adoption = self._without_credentials()

        adoption.assess_failure(self.experiment_id)

        chain = [
            (entry["event"], entry)
            for entry in self._entries()
            if entry.get("event", "").startswith(("ci_evidence", "stale_adoption"))
        ]
        self.assertEqual(
            [name for name, _ in chain],
            [
                "ci_evidence_lookup",
                "ci_evidence_live_fallback",
                "ci_evidence_live_result",
                "stale_adoption_assessment",
            ],
        )
        by_event = dict(chain)
        self.assertFalse(by_event["ci_evidence_lookup"]["persisted_found"])
        self.assertEqual(
            by_event["ci_evidence_live_fallback"]["fallback_reason"], "no_persisted_evidence"
        )
        self.assertIn("GITHUB_TOKEN/GH_TOKEN", by_event["ci_evidence_live_result"]["reason"])
        self.assertEqual(
            by_event["stale_adoption_assessment"]["category"], "no_ci_evidence"
        )


class FinalizationBlockedLoggingTests(unittest.TestCase):
    """Finalization must say why it stays in needs_review."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name) / "logs"
        self.logs.mkdir()
        self.db_path = str(Path(self.tmp.name) / "nexus.db")
        self.experiment = SimpleNamespace(id="3946adc3485049198d02bdc0a2e658fc")
        self.approval = SimpleNamespace(id="9406d03b367b4c8e82541d55ea97958d", task_body="T")

    def _entries(self) -> list[dict]:
        path = self.logs / f"{self.experiment.id}.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _coordinator(self, *, retryable: bool):
        from modules.experiment_adoption_service import StaleFailureAssessment

        adoption = SimpleNamespace(
            status=STATUS_FAILED,
            reason=_TIMEOUT_REASON,
            integrated_commit_sha=None,
            local_sync_pending=False,
        )
        assessment = StaleFailureAssessment(
            retryable,
            "superseded_local_verification" if retryable else "no_ci_evidence",
            "green exact-commit CI" if retryable else "no successful authoritative CI",
        )

        class _Service:
            starts = 0

            def get(self, experiment_id):
                return adoption

            def assess_failure(self, experiment_id):
                return assessment

            def start_adoption(inner, experiment_id, *, superseded=None):
                inner.starts += 1
                adoption.status = "queued"
                return adoption

        experiment_service = MagicMock()
        experiment_service.get_experiment_logger.side_effect = lambda eid: ExperimentLogger(
            eid, log_dir=self.logs, _mirror=False
        )
        coordinator = TaskFinalizationCoordinator(
            experiment_service=experiment_service,
            github_work_tracking=MagicMock(),
            db_path=self.db_path,
            repo_root=self.tmp.name,
            adoption_service=_Service(),
            restart_runner=lambda services: None,
            live_validator=lambda approval, experiment: {"summary": "ok"},
        )
        self.addCleanup(coordinator.close)
        state = coordinator._get_or_create(str(self.approval.id), str(self.experiment.id))
        state.retry_count = coordinator.max_retries
        coordinator._save(state)
        return coordinator

    def test_a_blocked_finalization_names_the_evidence_category(self):
        coordinator = self._coordinator(retryable=False)

        result = coordinator.advance(self.approval, self.experiment)

        self.assertEqual(result.status, "needs_review")
        blocked = [e for e in self._entries() if e["event"] == "finalization_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["adoption_status"], STATUS_FAILED)
        self.assertEqual(blocked[0]["assessment_category"], "no_ci_evidence")
        self.assertEqual(blocked[0]["finalization_phase"], PHASE_NEEDS_REVIEW)
        self.assertEqual(blocked[0]["phase"], "finalization")
        self.assertEqual(blocked[0]["retry_count"], coordinator.max_retries)
        self.assertEqual(blocked[0]["level"], "WARNING")
        # The historical text is still there, but no longer the only thing.
        self.assertIn(_TIMEOUT_REASON, blocked[0]["reason"])

    def test_a_reopened_finalization_is_not_logged_as_blocked(self):
        coordinator = self._coordinator(retryable=True)

        result = coordinator.advance(self.approval, self.experiment)

        self.assertEqual(result.status, "running")
        self.assertEqual(
            [e for e in self._entries() if e["event"] == "finalization_blocked"], []
        )


if __name__ == "__main__":
    unittest.main()
