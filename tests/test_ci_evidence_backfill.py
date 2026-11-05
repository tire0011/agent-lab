"""Regression tests for Issue #140: migrate pre-#139 CI evidence offline."""

from __future__ import annotations

import json
import os
import sqlite3
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.ci_evidence_backfill import _BACKFILL_MARKER, backfill_legacy_ci_evidence
from modules.experiment_adoption_service import STATUS_FAILED, ExperimentAdoptionService
from modules.github_work_tracking import GitHubCIVerification, GitHubWorkTrackingService
from tests.test_experiment_adoption_service import AdoptionRepoFixture, git


def _wait_for_thread(service, experiment_id, timeout=None):
    thread = service._threads.get(experiment_id)
    if thread is not None:
        thread.join(timeout)

class LegacyCIEvidenceBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)
        self.experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        self.run_id = 33898643539
        self.fx.create_experiment(experiment_id=self.experiment_id, changed_files=["app.py"])

        experiment = self.fx.experiment_service.get_experiment(self.experiment_id)
        self.worktree = Path(experiment.worktree_path)
        self.base_sha = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
        tree_sha = git(self.worktree, "rev-parse", "HEAD^{tree}").stdout.strip()
        self.checkpoint_sha = git(
            self.worktree,
            "commit-tree",
            tree_sha,
            "-p",
            self.base_sha,
            "-m",
            "legacy verified checkpoint",
        ).stdout.strip()

        git(
            self.worktree,
            "update-ref",
            f"refs/heads/legacy-ci-{self.experiment_id[:8]}",
            self.checkpoint_sha,
        )

        ref = self.fx.github_work_tracking.for_experiment(self.experiment_id)
        ref.checkpoint_sha = self.checkpoint_sha
        self.fx.github_work_tracking._persist_experiment(ref)

    def _insert_legacy_ci_run(self, *, command=None, output=None, exit_code=0) -> None:
        command = command or ["GitHub Actions", "tests.yml", self.checkpoint_sha]
        output = output or (
            f"GitHub Actions run {self.run_id} for commit {self.checkpoint_sha} "
            "completed with conclusion success. "
            f"https://example.invalid/actions/runs/{self.run_id}"
        )
        conn = sqlite3.connect(self.fx.db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO verification_runs (
                        experiment_id, iteration, started_at, ended_at, status,
                        commit_sha, command_json, exit_code, output, error
                    ) VALUES (?, 0, ?, ?, 'succeeded', ?, ?, ?, ?, NULL)
                    """,
                    (
                        self.experiment_id,
                        "2026-09-05T09:00:00+00:00",
                        "2026-09-05T09:02:00+00:00",
                        self.checkpoint_sha,
                        json.dumps(command),
                        exit_code,
                        output,
                    ),
                )
        finally:
            conn.close()

    def _insert_stale_adoption(self) -> None:
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
                        "Die Integrationstests konnten nicht erfolgreich abgeschlossen werden.",
                    ),
                )
        finally:
            conn.close()

    def _reset_to_pre_139_database(self) -> None:
        conn = sqlite3.connect(self.fx.db_path)
        try:
            with conn:
                conn.execute("DROP TABLE IF EXISTS github_ci_verifications")
                conn.execute("DELETE FROM service_settings WHERE key=?", (_BACKFILL_MARKER,))
        finally:
            conn.close()

    def _restart_current_code_without_credentials(self):
        self._reset_to_pre_139_database()

        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "",
                "GH_TOKEN": "",
                "NEXUS_REPO_ROOT": str(self.fx.repo),
            },
            clear=False,
        ):
            tracking = GitHubWorkTrackingService(
                db_path=self.fx.db_path,
                repo_root=str(self.fx.repo),
                repository="tire0011/agent-lab",
            )
            adoption = ExperimentAdoptionService(
                experiment_service=self.fx.experiment_service,
                db_path=self.fx.db_path,
                repo_root=str(self.fx.repo),
                worktrees_root=str(self.fx.worktrees),
                lock_path=self.fx.lock_path,
                github_work_tracking=tracking,
            )
        self.addCleanup(tracking.close)
        self.addCleanup(adoption.close)
        self.assertFalse(tracking.configured)
        return tracking, adoption

    def _advance_target_branch(self) -> None:
        (self.fx.repo / "unrelated-after-ci.py").write_text("MOVED = True\n", encoding="utf-8")
        git(self.fx.repo, "add", "unrelated-after-ci.py")
        git(self.fx.repo, "commit", "-m", "advance main after historical CI")
        git(self.fx.repo, "push", "origin", "main")
        git(self.fx.repo, "fetch", "origin", "main")

    def test_pre_139_green_ci_is_backfilled_and_reopens_stale_adoption(self):
        self._insert_legacy_ci_run()
        self._insert_stale_adoption()
        tracking, adoption = self._restart_current_code_without_credentials()

        with patch.object(
            tracking, "wait_for_ci", side_effect=AssertionError("must not call GitHub")
        ):
            assessment = adoption.assess_failure(self.experiment_id)

        self.assertTrue(assessment.retryable, assessment.detail)
        self.assertEqual(assessment.category, "superseded_local_verification")
        self.assertEqual(assessment.evidence_commit_sha, self.checkpoint_sha)
        self.assertEqual(assessment.evidence_run_id, self.run_id)

        row = sqlite3.connect(self.fx.db_path).execute(
            """
            SELECT experiment_id, commit_sha, status, conclusion, run_id,
                   provider, base_sha, observed_at
            FROM github_ci_verifications WHERE commit_sha=?
            """,
            (self.checkpoint_sha,),
        ).fetchone()
        self.assertEqual(
            row,
            (
                self.experiment_id,
                self.checkpoint_sha,
                "passed",
                "success",
                self.run_id,
                "github_actions",
                self.base_sha,
                "2026-09-05T09:02:00+00:00",
            ),
        )

        with patch.object(adoption, "_run_adoption_pipeline"):
            adoption.start_adoption(self.experiment_id, superseded=assessment)
            _wait_for_thread(adoption, self.experiment_id, timeout=5)
        self.assertEqual(adoption.get(self.experiment_id).stale_retry_attempts, 1)
        history = adoption.failure_history(self.experiment_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["adoption_id"], "c617b97314ed4da98c539be99130c6c0")

    def test_backfill_is_one_shot_and_does_not_replace_newer_durable_evidence(self):
        self._insert_legacy_ci_run()
        tracking, _ = self._restart_current_code_without_credentials()
        first = tracking.recorded_ci_for(self.checkpoint_sha, base_sha=self.base_sha)
        self.assertEqual(first.status, "passed")

        conn = sqlite3.connect(self.fx.db_path)
        with conn:
            conn.execute(
                """
                UPDATE github_ci_verifications
                SET status='failed', conclusion='failure', run_id=999,
                    observed_at='2026-09-09T00:00:00+00:00'
                WHERE commit_sha=?
                """,
                (self.checkpoint_sha,),
            )
        conn.close()

        from modules.run_store import init_db

        with patch("modules.ci_evidence_backfill.subprocess.run") as git_run:
            migrated = init_db(self.fx.db_path)
            migrated.close()
        git_run.assert_not_called()

        conn = sqlite3.connect(self.fx.db_path)
        row = conn.execute(
            "SELECT status, conclusion, run_id, observed_at FROM github_ci_verifications WHERE commit_sha=?",
            (self.checkpoint_sha,),
        ).fetchone()
        marker = conn.execute(
            "SELECT value FROM service_settings WHERE key=?", (_BACKFILL_MARKER,)
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("failed", "failure", 999, "2026-09-09T00:00:00+00:00"))
        self.assertEqual(marker, ("done",))

    def test_generic_local_success_is_not_reinterpreted_as_github_ci(self):
        self._insert_legacy_ci_run(
            command=["python", "-m", "unittest", "discover", "-s", "tests", "-q"],
            output="OK",
            exit_code=0,
        )
        tracking, _ = self._restart_current_code_without_credentials()

        self.assertIsNone(
            tracking.recorded_ci_for(self.checkpoint_sha, base_sha=self.base_sha)
        )
        count = sqlite3.connect(self.fx.db_path).execute(
            "SELECT COUNT(*) FROM github_ci_verifications"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_a_red_run_with_failure_detail_is_still_parsed(self):
        """The first line is the evidence; what follows it is detail (#472)."""
        self._insert_legacy_ci_run(
            output=(
                f"GitHub Actions run {self.run_id} for commit {self.checkpoint_sha} "
                "completed with conclusion failure. "
                f"https://example.invalid/actions/runs/{self.run_id}\n"
                "Failed job: unittest\n"
                "Failed step: Run tests\n"
                "Failing tests:\n"
                "FAILED tests/test_a.py::test_one - boom\n"
            ),
            exit_code=1,
        )

        tracking, _ = self._restart_current_code_without_credentials()

        details = tracking.recorded_ci_details(self.checkpoint_sha)
        self.assertEqual(details["status"], "failed")
        self.assertEqual(details["conclusion"], "failure")
        self.assertEqual(details["run_id"], self.run_id)
        self.assertEqual(
            details["url"], f"https://example.invalid/actions/runs/{self.run_id}"
        )

    def test_mismatched_ci_identity_is_not_backfilled(self):
        other_sha = "a" * 40
        self._insert_legacy_ci_run(
            command=["GitHub Actions", "tests.yml", other_sha],
        )
        tracking, _ = self._restart_current_code_without_credentials()

        self.assertIsNone(
            tracking.recorded_ci_for(self.checkpoint_sha, base_sha=self.base_sha)
        )

    def test_main_repo_recovers_parent_after_worktree_cleanup(self):
        self._insert_legacy_ci_run()
        self._reset_to_pre_139_database()

        git(self.fx.repo, "worktree", "remove", "--force", str(self.worktree))
        shutil.rmtree(self.worktree, ignore_errors=True)
        self.assertFalse(self.worktree.exists())

        tracking = GitHubWorkTrackingService(
            db_path=self.fx.db_path,
            repo_root=str(self.fx.repo),
            repository="tire0011/agent-lab",
            token="unused",
        )
        self.addCleanup(tracking.close)
        conn = sqlite3.connect(self.fx.db_path)
        inserted = backfill_legacy_ci_evidence(conn, repo_root=str(self.fx.repo))
        conn.close()

        self.assertEqual(inserted, 1)
        self.assertIsNotNone(
            tracking.recorded_ci_for(self.checkpoint_sha, base_sha=self.base_sha)
        )

    def test_backfilled_green_ci_is_invalid_after_target_base_moves(self):
        self._insert_legacy_ci_run()
        self._insert_stale_adoption()
        tracking, adoption = self._restart_current_code_without_credentials()

        with patch.object(
            tracking, "wait_for_ci", side_effect=AssertionError("must not call GitHub")
        ):
            before = adoption.assess_failure(self.experiment_id)
        self.assertTrue(before.retryable, before.detail)

        self._advance_target_branch()

        unavailable = GitHubCIVerification(
            status="unavailable",
            commit_sha=self.checkpoint_sha,
            reason="no live credentials after target base moved",
        )
        with patch.object(tracking, "wait_for_ci", return_value=unavailable):
            after = adoption.assess_failure(self.experiment_id)
        self.assertFalse(after.retryable)
        self.assertEqual(after.category, "no_ci_evidence")


if __name__ == "__main__":
    unittest.main()
