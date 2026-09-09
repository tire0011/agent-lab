"""Tests for Issue #138: authoritative CI evidence must outlive the process.

#136 made a stale adoption failure retryable when exact-commit CI is
green. But `assess_failure` re-derived that evidence with a live GitHub
lookup, so on the real #115 / PR #116 retry it reported:

    CI status=unavailable
    reason=GitHub CI identity unavailable: configure GITHUB_TOKEN/GH_TOKEN
    assessment retryable=False category=no_ci_evidence

even though commit 28399a0a had already been verified by Actions run
33898643539. Evidence Nexus had accepted disappeared because the current
process happened to lack credentials, which defeats the restart-safe
lifecycle #125/#128/#134/#136 are built on.
"""

from __future__ import annotations

import os
import sqlite3
import unittest
from unittest.mock import patch

from modules.experiment_adoption_service import (
    STATUS_ADOPTED,
    STATUS_FAILED,
    ExperimentAdoptionService,
)
from modules.github_work_tracking import GitHubCIVerification, GitHubWorkTrackingService
from tests.test_experiment_adoption_service import AdoptionRepoFixture, git

_TIMEOUT_REASON = "Die Integrationstests konnten nicht erfolgreich abgeschlossen werden."


class PersistedCIEvidenceTests(unittest.TestCase):
    # Adoption always polls CI with a zero wait; the tests do the same so a
    # deliberately absent run returns at once instead of burning the
    # production start timeout.
    _BOUNDED = {
        "start_timeout_seconds": 0,
        "completion_timeout_seconds": 0,
        "poll_interval_seconds": 0.01,
    }
    # The target head the stubbed GitHub API reports, which is also the
    # checkpoint's parent - so wait_for_ci considers the base unchanged.
    _STUB_BASE_SHA = "base" + "0" * 36

    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)
        self.experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        self.checkpoint_sha = "28399a0ad8dfca091d948b01b788fd6280b8e12b"
        self.fx.create_experiment(experiment_id=self.experiment_id, changed_files=["app.py"])
        ref = self.fx.github_work_tracking.for_experiment(self.experiment_id)
        ref.checkpoint_sha = self.checkpoint_sha
        self.fx.github_work_tracking._persist_experiment(ref)

    # ---- helpers -------------------------------------------------------

    def _green(self, commit_sha: str | None = None) -> GitHubCIVerification:
        return GitHubCIVerification(
            status="passed",
            commit_sha=commit_sha or self.checkpoint_sha,
            conclusion="success",
            run_id=33898643539,
            url="https://example.invalid/actions/runs/33898643539",
        )

    def _red(self) -> GitHubCIVerification:
        return GitHubCIVerification(
            status="failed",
            commit_sha=self.checkpoint_sha,
            conclusion="failure",
            run_id=42,
        )

    def _integration_base(self) -> str:
        """The target-branch commit adoption would cherry-pick onto."""
        return git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()

    def _record(self, verification: GitHubCIVerification, **kwargs) -> bool:
        """Record a verdict against the base that is current right now."""
        kwargs.setdefault("base_sha", self._integration_base())
        return self.fx.github_work_tracking.record_ci_verification(verification, **kwargs)

    def _advance_target_branch(self) -> None:
        """Move origin/main on, the way any other merge would."""
        (self.fx.repo / "unrelated.py").write_text("MOVED = True\n", encoding="utf-8")
        git(self.fx.repo, "add", "unrelated.py")
        git(self.fx.repo, "commit", "-m", "unrelated change on main")
        git(self.fx.repo, "push", "origin", "main")
        git(self.fx.repo, "fetch", "origin", "main")

    def _stale_failed_adoption(self) -> None:
        """The historical row: a local suite timeout, no verdict."""
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

    def _restarted_without_credentials(self):
        """Rebuild the services the way a restarted process would - no token.

        The sandbox may well have a real GITHUB_TOKEN in the environment,
        so it is blanked explicitly: the point is a service that genuinely
        cannot reach GitHub, which is what the reproduction describes.
        """
        with patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}, clear=False):
            tracking = GitHubWorkTrackingService(
                db_path=self.fx.db_path,
                repo_root=str(self.fx.repo),
                repository="tire0011/agent-lab",
            )
        self.addCleanup(tracking.close)
        self.assertFalse(tracking.configured, "the rebuilt service must have no credentials")
        self.assertIn("GITHUB_TOKEN/GH_TOKEN", tracking.readiness_error() or "")

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

    # ---- the store -----------------------------------------------------

    def test_an_authoritative_verdict_is_stored_and_read_back(self):
        stored = self._record(
            self._green(), experiment_id=self.experiment_id
        )
        self.assertTrue(stored)

        recorded = self.fx.github_work_tracking.recorded_ci_for(self.checkpoint_sha)
        self.assertEqual(recorded.status, "passed")
        self.assertEqual(recorded.conclusion, "success")
        self.assertEqual(recorded.run_id, 33898643539)
        self.assertEqual(recorded.commit_sha, self.checkpoint_sha)

    def test_an_unavailable_result_is_never_cached(self):
        """A temporary outage says nothing about the commit."""
        stored = self._record(
            GitHubCIVerification(
                status="unavailable", commit_sha=self.checkpoint_sha, reason="no run started"
            )
        )
        self.assertFalse(stored)
        self.assertIsNone(self.fx.github_work_tracking.recorded_ci_for(self.checkpoint_sha))

    def test_evidence_is_only_ever_found_for_the_exact_commit(self):
        self._record(self._green())

        self.assertIsNone(self.fx.github_work_tracking.recorded_ci_for("b" * 40))
        self.assertIsNone(self.fx.github_work_tracking.recorded_ci_for(""))

    def test_the_newest_authoritative_observation_supersedes(self):
        """Deterministic precedence: a re-run that flips the verdict wins."""
        self._record(self._green())
        self._record(self._red())

        recorded = self.fx.github_work_tracking.recorded_ci_for(self.checkpoint_sha)
        self.assertEqual(recorded.status, "failed")

        rows = sqlite3.connect(self.fx.db_path).execute(
            "SELECT COUNT(*) FROM github_ci_verifications WHERE commit_sha=?",
            (self.checkpoint_sha,),
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_no_credential_is_persisted(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp-supersecret-token-value"}, clear=False):
            tracking = GitHubWorkTrackingService(
                db_path=self.fx.db_path,
                repo_root=str(self.fx.repo),
                repository="tire0011/agent-lab",
            )
            self.addCleanup(tracking.close)
            tracking.record_ci_verification(self._green(), experiment_id=self.experiment_id)

        dumped = "\n".join(
            str(value)
            for row in sqlite3.connect(self.fx.db_path).execute(
                "SELECT * FROM github_ci_verifications"
            )
            for value in row
        )
        self.assertNotIn("ghp-supersecret-token-value", dumped)

    def _github_api(self, *, head_sha: str, conclusion: str = "success"):
        """A minimal GitHub API stub for the endpoints wait_for_ci walks."""
        base_sha = self._STUB_BASE_SHA

        def _request(method, path, payload=None):
            if "/actions/workflows/" in path:
                return {
                    "workflow_runs": [{
                        "id": 33898643539,
                        "head_sha": head_sha,
                        "event": "push",
                        "status": "completed",
                        "conclusion": conclusion,
                        "run_attempt": 1,
                        "html_url": "https://example.invalid/actions/runs/33898643539",
                    }]
                }
            if f"/commits/{self.checkpoint_sha}" in path:
                return {"parents": [{"sha": base_sha}]}
            if "/git/ref/heads/" in path:
                return {"object": {"sha": base_sha}}
            raise AssertionError(f"unexpected GitHub request: {method} {path}")

        return _request

    def test_wait_for_ci_records_what_it_observed(self):
        """The store is populated at the source, so every caller fills it.

        This is what makes the verification path seed the evidence that
        adoption reads back later - in the reproduction it saw the run and
        then the fact was lost.
        """
        with patch.object(
            self.fx.github_work_tracking,
            "_request",
            side_effect=self._github_api(head_sha=self.checkpoint_sha),
        ):
            verification = self.fx.github_work_tracking.wait_for_ci(
                self.experiment_id, **self._BOUNDED
            )

        self.assertEqual(verification.status, "passed")
        recorded = self.fx.github_work_tracking.recorded_ci_for(self.checkpoint_sha)
        self.assertEqual(recorded.status, "passed")
        self.assertEqual(recorded.run_id, 33898643539)

    def test_wait_for_ci_records_a_red_verdict_too(self):
        with patch.object(
            self.fx.github_work_tracking,
            "_request",
            side_effect=self._github_api(
                head_sha=self.checkpoint_sha, conclusion="failure"
            ),
        ):
            self.fx.github_work_tracking.wait_for_ci(self.experiment_id, **self._BOUNDED)

        self.assertEqual(
            self.fx.github_work_tracking.recorded_ci_for(self.checkpoint_sha).status, "failed"
        )

    def test_wait_for_ci_does_not_record_a_run_for_another_commit(self):
        """Only a verdict for the SHA we asked about is evidence for it."""
        with patch.object(
            self.fx.github_work_tracking,
            "_request",
            side_effect=self._github_api(head_sha="e" * 40),
        ):
            self.fx.github_work_tracking.wait_for_ci(self.experiment_id, **self._BOUNDED)

        self.assertIsNone(self.fx.github_work_tracking.recorded_ci_for(self.checkpoint_sha))
        self.assertIsNone(self.fx.github_work_tracking.recorded_ci_for("e" * 40))

    # ---- the adoption read path ---------------------------------------

    def test_assess_failure_uses_persisted_evidence_without_credentials(self):
        """The acceptance criterion, end to end at the assessment level."""
        self._stale_failed_adoption()
        self._record(
            self._green(), experiment_id=self.experiment_id
        )

        _, adoption_service = self._restarted_without_credentials()
        assessment = adoption_service.assess_failure(self.experiment_id)

        self.assertTrue(assessment.retryable, assessment.detail)
        self.assertEqual(assessment.category, "superseded_local_verification")
        self.assertEqual(assessment.evidence_commit_sha, self.checkpoint_sha)

    def test_without_persisted_evidence_the_restart_still_reports_no_evidence(self):
        """Guards the test above: it must be the store doing the work."""
        self._stale_failed_adoption()

        _, adoption_service = self._restarted_without_credentials()
        assessment = adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "no_ci_evidence")

    def test_no_live_lookup_happens_when_durable_evidence_exists(self):
        self._stale_failed_adoption()
        self._record(self._green())

        tracking, adoption_service = self._restarted_without_credentials()
        with patch.object(
            tracking, "wait_for_ci", side_effect=AssertionError("must not call GitHub")
        ):
            assessment = adoption_service.assess_failure(self.experiment_id)

        self.assertTrue(assessment.retryable)

    def test_persisted_success_for_another_commit_is_not_reused(self):
        """Rule 2: a green verdict belongs to its own commit only."""
        self._stale_failed_adoption()
        self._record(self._green(commit_sha="c" * 40))

        _, adoption_service = self._restarted_without_credentials()
        assessment = adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "no_ci_evidence")

    def test_persisted_red_evidence_stays_terminal(self):
        """Rule 3: cached data must never turn a red result green."""
        self._stale_failed_adoption()
        self._record(self._red())

        _, adoption_service = self._restarted_without_credentials()
        assessment = adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "no_ci_evidence")

    def test_a_moved_integration_base_invalidates_the_cached_verdict(self):
        """Review finding on PR #139.

        `wait_for_ci` only calls exact-checkpoint CI authoritative while
        the checkpoint's parent is still the target head, because the same
        checkpoint cherry-picked onto a moved base produces a tree CI
        never saw (#134). Reading a stored verdict has to honour that too,
        or a green result cached while main=A would survive main advancing
        to B and adoption would skip the integration verification it needs.
        """
        self._stale_failed_adoption()
        self._record(self._green(), experiment_id=self.experiment_id)

        # Sanity: on the unchanged base the verdict is usable.
        _, before_move = self._restarted_without_credentials()
        self.assertTrue(before_move.assess_failure(self.experiment_id).retryable)

        self._advance_target_branch()

        _, after_move = self._restarted_without_credentials()
        assessment = after_move.assess_failure(self.experiment_id)

        self.assertFalse(
            assessment.retryable,
            "a verdict from before the base moved is not authoritative for this integration",
        )
        self.assertEqual(assessment.category, "no_ci_evidence")

    def test_the_store_rejects_a_verdict_taken_on_a_different_base(self):
        self._record(self._green())
        recorded_base = self._integration_base()

        self.assertIsNotNone(
            self.fx.github_work_tracking.recorded_ci_for(
                self.checkpoint_sha, base_sha=recorded_base
            )
        )
        self.assertIsNone(
            self.fx.github_work_tracking.recorded_ci_for(
                self.checkpoint_sha, base_sha="f" * 40
            )
        )

    def test_a_verdict_without_a_recorded_base_is_not_reused(self):
        """Rows written before the base was tracked cannot prove the rule."""
        self._record(self._green())
        conn = sqlite3.connect(self.fx.db_path)
        with conn:
            conn.execute("UPDATE github_ci_verifications SET base_sha=NULL")
        conn.close()

        self.assertIsNone(
            self.fx.github_work_tracking.recorded_ci_for(
                self.checkpoint_sha, base_sha=self._integration_base()
            )
        )
        # Without a base to check against, the row is still readable.
        self.assertIsNotNone(
            self.fx.github_work_tracking.recorded_ci_for(self.checkpoint_sha)
        )

    def test_wait_for_ci_stores_the_base_it_validated(self):
        with patch.object(
            self.fx.github_work_tracking,
            "_request",
            side_effect=self._github_api(head_sha=self.checkpoint_sha),
        ):
            self.fx.github_work_tracking.wait_for_ci(self.experiment_id, **self._BOUNDED)

        stored_base = sqlite3.connect(self.fx.db_path).execute(
            "SELECT base_sha FROM github_ci_verifications WHERE commit_sha=?",
            (self.checkpoint_sha,),
        ).fetchone()[0]
        self.assertEqual(stored_base, self._STUB_BASE_SHA)

    def test_a_new_checkpoint_is_not_covered_by_the_old_verdict(self):
        """A replaced implementation gets a new SHA, so it goes live again."""
        self._stale_failed_adoption()
        self._record(self._green())

        ref = self.fx.github_work_tracking.for_experiment(self.experiment_id)
        ref.checkpoint_sha = "d" * 40
        self.fx.github_work_tracking._persist_experiment(ref)

        _, adoption_service = self._restarted_without_credentials()
        assessment = adoption_service.assess_failure(self.experiment_id)

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "no_ci_evidence")

    def test_the_retry_completes_without_credentials_during_adoption(self):
        """The #115/#116 end-to-end shape: stale row in, adopted out.

        Nothing is edited in SQLite by hand and no GitHub credentials are
        available to the retry; the persisted verdict alone carries it.
        """
        self._stale_failed_adoption()
        self._record(
            self._green(), experiment_id=self.experiment_id
        )

        _, adoption_service = self._restarted_without_credentials()
        assessment = adoption_service.assess_failure(self.experiment_id)
        self.assertTrue(assessment.retryable)

        # The pipeline needs the GitHub lifecycle to publish, which the
        # tokenless service cannot do - so adoption runs on the configured
        # service, still reading the same persisted verdict rather than
        # making a live CI lookup.
        with (
            patch.object(
                self.fx.github_work_tracking,
                "wait_for_ci",
                side_effect=AssertionError("must not re-derive CI evidence"),
            ),
            patch("modules.experiment_adoption_service.run_verification_test") as local_tests,
        ):
            self.fx.adoption_service.start_adoption(
                self.experiment_id, superseded=assessment
            )
            adoption = self.fx.wait_terminal(self.experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED, adoption.reason)
        self.assertEqual(adoption.stale_retry_attempts, 1)
        local_tests.assert_not_called()
        self.assertIn(str(33898643539), adoption.test_output or "")


if __name__ == "__main__":
    unittest.main()
