"""Regression tests for Issue #144/#149 moved-target-base stale adoption recovery."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules.experiment_adoption_service import STATUS_ADOPTED, STATUS_FAILED
from modules.experiment_service import _truncate_text
from modules.github_work_tracking import GitHubCIVerification
from modules.task_finalization import TaskFinalizationCoordinator
from modules.verification_runner import (
    run_verification_test_monitored as real_run_verification_test,
)
from tests.test_experiment_adoption_service import AdoptionRepoFixture, git


_TIMEOUT_REASON = "Die Integrationstests konnten nicht erfolgreich abgeschlossen werden."


class MovedBaseIntegrationFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)
        self.experiment_id = "3946adc3485049198d02bdc0a2e658fc"
        self.checkpoint_sha = "28399a0ad8dfca091d948b01b788fd6280b8e12b"
        self.adoption_id = "c617b97314ed4da98c539be99130c6c0"
        self.fx.create_experiment(
            experiment_id=self.experiment_id,
            changed_files=["app.py"],
        )
        ref = self.fx.github_work_tracking.for_experiment(self.experiment_id)
        self.assertIsNotNone(ref)
        ref.checkpoint_sha = self.checkpoint_sha
        self.fx.github_work_tracking._persist_experiment(ref)
        self.old_base = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()

    def _green(self) -> GitHubCIVerification:
        return GitHubCIVerification(
            status="passed",
            commit_sha=self.checkpoint_sha,
            conclusion="success",
            run_id=33898643539,
            url="https://example.invalid/actions/runs/33898643539",
        )

    def _unavailable_after_base_move(self) -> GitHubCIVerification:
        return GitHubCIVerification(
            status="unavailable",
            commit_sha=self.checkpoint_sha,
            reason=(
                "target branch moved since checkpoint verification; "
                "integration fallback is required for the current base"
            ),
        )

    def _historical_timeout(
        self,
        *,
        test_exit_code=None,
        experiment_commit_sha="d757a26a8778135bacf7c1008f9db0eda1e127a1",
        actual_files=None,
    ) -> None:
        files = actual_files or ["app.py"]
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, experiment_commit_sha,
                    target_branch, actual_files_json, expected_files_json,
                    test_exit_code, test_output, reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'main', ?, ?, ?, ?, ?, 't', 't')
                """,
                (
                    self.adoption_id,
                    self.experiment_id,
                    STATUS_FAILED,
                    experiment_commit_sha,
                    json.dumps(files),
                    json.dumps(files),
                    test_exit_code,
                    "[test command timed out]" if test_exit_code is None else "FAILED",
                    _TIMEOUT_REASON,
                ),
            )

    def _commit_verified_source(self) -> str:
        experiment = self.fx.experiment_service.get_experiment(self.experiment_id)
        worktree = Path(experiment.worktree_path)
        git(worktree, "add", "--all")
        git(worktree, "commit", "-m", "persist verified experiment source")
        return git(worktree, "rev-parse", "HEAD").stdout.strip()

    def _replace_verified_snapshot_with_large_diff(self) -> None:
        experiment = self.fx.experiment_service.get_experiment(self.experiment_id)
        worktree = Path(experiment.worktree_path)
        (worktree / "app.py").write_text(
            "VALUE = 'large verified diff'\n" + "PAYLOAD = '" + ("x" * 70000) + "'\n",
            encoding="utf-8",
        )
        snapshot = self.fx.adoption_service._capture_snapshot(
            str(worktree),
            self.fx.experiment_service.get_experiment_logger(self.experiment_id),
            phase="test",
        )
        truncated = _truncate_text(
            snapshot["diff_text"],
            self.fx.experiment_service.max_diff_bytes,
            label="diff",
        )
        self.assertIsNotNone(truncated)
        self.assertIn("[... diff truncated]", truncated)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                """
                UPDATE experiments
                SET git_status=?, diff_stat=?, diff_text=?
                WHERE id=?
                """,
                (
                    snapshot["git_status"],
                    snapshot["diff_stat"],
                    truncated,
                    self.experiment_id,
                ),
            )

    def _advance_main(self) -> str:
        (self.fx.repo / "unrelated.py").write_text("MOVED = True\n", encoding="utf-8")
        git(self.fx.repo, "add", "unrelated.py")
        git(self.fx.repo, "commit", "-m", "advance main after checkpoint")
        git(self.fx.repo, "push", "origin", "main")
        git(self.fx.repo, "fetch", "origin", "main")
        return git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()

    def _coordinator(self, *, max_retries: int = 2) -> TaskFinalizationCoordinator:
        coordinator = TaskFinalizationCoordinator(
            experiment_service=self.fx.experiment_service,
            github_work_tracking=self.fx.github_work_tracking,
            db_path=self.fx.db_path,
            repo_root=str(self.fx.repo),
            adoption_service=self.fx.adoption_service,
            restart_runner=lambda services: None,
            live_validator=lambda approval, experiment: {"summary": "ok"},
            max_retries=max_retries,
        )
        self.addCleanup(coordinator.close)
        return coordinator

    def _prepare_moved_base(self, *, committed_source: bool = False) -> tuple[str, str | None]:
        source_commit = self._commit_verified_source() if committed_source else None
        self._historical_timeout(
            experiment_commit_sha=source_commit or "d757a26a8778135bacf7c1008f9db0eda1e127a1"
        )
        stored = self.fx.github_work_tracking.record_ci_verification(
            self._green(),
            experiment_id=self.experiment_id,
            base_sha=self.old_base,
        )
        self.assertTrue(stored)
        new_base = self._advance_main()
        self.assertNotEqual(self.old_base, new_base)
        return new_base, source_commit

    def _events(self) -> list[dict]:
        events: list[dict] = []
        for path in self.fx.logs.glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    events.append(json.loads(line))
        return events

    def test_moved_base_is_integration_fallback_not_no_ci_evidence(self):
        new_base, _ = self._prepare_moved_base()
        coordinator = self._coordinator()

        with patch.object(
            self.fx.github_work_tracking,
            "wait_for_ci",
            return_value=self._unavailable_after_base_move(),
        ):
            assessment = coordinator._failure_assessment(
                SimpleNamespace(id=self.experiment_id)
            )

        self.assertIsNotNone(assessment)
        self.assertTrue(assessment.retryable)
        self.assertEqual(assessment.category, "integration_fallback_required")
        self.assertEqual(assessment.evidence_commit_sha, self.checkpoint_sha)
        self.assertEqual(assessment.evidence_run_id, 33898643539)
        self.assertEqual(
            self.fx.adoption_service._integration_base_sha(),
            new_base,
        )

    def test_moved_base_passes_current_main_integration_fallback_and_reuses_row(self):
        self._prepare_moved_base()
        coordinator = self._coordinator()
        unavailable = self._unavailable_after_base_move()

        with patch.object(
            self.fx.github_work_tracking,
            "wait_for_ci",
            return_value=unavailable,
        ):
            assessment = coordinator._failure_assessment(
                SimpleNamespace(id=self.experiment_id)
            )
        self.assertTrue(assessment.retryable)

        with (
            patch.object(
                self.fx.github_work_tracking,
                "wait_for_ci",
                return_value=unavailable,
            ),
            patch(
                "modules.experiment_adoption_service.run_verification_test_monitored",
                side_effect=real_run_verification_test,
            ) as local_tests,
        ):
            self.fx.adoption_service.start_adoption(
                self.experiment_id,
                superseded=assessment,
            )
            adoption = self.fx.wait_terminal(self.experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.id, self.adoption_id)
        self.assertEqual(adoption.stale_retry_attempts, 1)
        local_tests.assert_called_once()
        history = self.fx.adoption_service.failure_history(self.experiment_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["adoption_id"], self.adoption_id)
        self.assertIn("current integration base", history[0]["superseded_by"])

    def test_committed_verified_source_reaches_moved_base_integration_candidate(self):
        _, source_commit = self._prepare_moved_base(committed_source=True)
        self.assertIsNotNone(source_commit)
        coordinator = self._coordinator(max_retries=0)
        experiment = self.fx.experiment_service.get_experiment(self.experiment_id)
        unavailable = self._unavailable_after_base_move()

        with (
            patch.object(
                self.fx.github_work_tracking,
                "wait_for_ci",
                return_value=unavailable,
            ),
            patch(
                "modules.experiment_adoption_service.run_verification_test_monitored",
                side_effect=real_run_verification_test,
            ) as local_tests,
        ):
            result = coordinator.advance(SimpleNamespace(id="approval-149"), experiment)
            adoption = self.fx.wait_terminal(self.experiment_id)

        self.assertEqual(result.status, "running")
        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.id, self.adoption_id)
        self.assertEqual(adoption.stale_retry_attempts, 1)
        local_tests.assert_called_once()

        history = self.fx.adoption_service.failure_history(self.experiment_id)
        self.assertEqual(history[0]["experiment_commit_sha"], source_commit)
        events = self._events()
        source_events = [event for event in events if event.get("event") == "verified_source_integrity"]
        candidate_events = [event for event in events if event.get("event") == "integration_candidate"]
        self.assertTrue(source_events)
        self.assertTrue(candidate_events)
        self.assertTrue(
            any(
                event.get("source_form") == "persisted_commit"
                and event.get("outcome") == "accepted"
                and event.get("source_commit_sha") == source_commit
                for event in source_events
            )
        )
        self.assertTrue(any(event.get("outcome") == "queued" for event in candidate_events))

    def test_truncated_verified_diff_reaches_moved_base_integration_candidate(self):
        self._replace_verified_snapshot_with_large_diff()
        _, source_commit = self._prepare_moved_base(committed_source=True)
        self.assertIsNotNone(source_commit)
        coordinator = self._coordinator(max_retries=0)
        experiment = self.fx.experiment_service.get_experiment(self.experiment_id)
        self.assertIn("[... diff truncated]", experiment.diff_text)
        unavailable = self._unavailable_after_base_move()

        with (
            patch.object(
                self.fx.github_work_tracking,
                "wait_for_ci",
                return_value=unavailable,
            ),
            patch(
                "modules.experiment_adoption_service.run_verification_test_monitored",
                side_effect=real_run_verification_test,
            ) as local_tests,
        ):
            result = coordinator.advance(SimpleNamespace(id="approval-149-large-diff"), experiment)
            adoption = self.fx.wait_terminal(self.experiment_id)

        self.assertEqual(result.status, "running")
        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.id, self.adoption_id)
        self.assertEqual(adoption.stale_retry_attempts, 1)
        local_tests.assert_called_once()
        source_events = [
            event for event in self._events() if event.get("event") == "verified_source_integrity"
        ]
        self.assertTrue(any(event.get("outcome") == "accepted" for event in source_events))

    def test_actual_mutation_of_committed_verified_source_is_still_rejected(self):
        _, source_commit = self._prepare_moved_base(committed_source=True)
        experiment = self.fx.experiment_service.get_experiment(self.experiment_id)
        worktree = Path(experiment.worktree_path)
        (worktree / "app.py").write_text("VALUE = 'tampered after verification'\n", encoding="utf-8")

        coordinator = self._coordinator(max_retries=0)
        with patch.object(
            self.fx.github_work_tracking,
            "wait_for_ci",
            return_value=self._unavailable_after_base_move(),
        ):
            result = coordinator.advance(SimpleNamespace(id="approval-149-tampered"), experiment)

        self.assertEqual(result.status, "needs_review")
        self.assertIn("seit der erfolgreichen Prüfung verändert", result.summary)
        self.assertEqual(
            git(worktree, "rev-parse", "HEAD").stdout.strip(),
            source_commit,
        )
        self.assertIn("tampered", (worktree / "app.py").read_text(encoding="utf-8"))
        source_events = [
            event for event in self._events() if event.get("event") == "verified_source_integrity"
        ]
        self.assertTrue(any(event.get("outcome") == "rejected" for event in source_events))

    def test_real_test_verdict_is_not_reopened_by_persisted_green_ci(self):
        self._historical_timeout(test_exit_code=1)
        self.fx.github_work_tracking.record_ci_verification(
            self._green(),
            experiment_id=self.experiment_id,
            base_sha=self.old_base,
        )
        self._advance_main()
        coordinator = self._coordinator()

        assessment = coordinator._failure_assessment(
            SimpleNamespace(id=self.experiment_id)
        )

        self.assertFalse(assessment.retryable)
        self.assertEqual(assessment.category, "test_verdict")


if __name__ == "__main__":
    unittest.main()
