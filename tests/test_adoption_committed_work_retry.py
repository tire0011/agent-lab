"""An adoption retry must find work that is already committed.

Regression for #175, reproducing the live failure of experiment
``3946adc3``: CI was green on the verified checkpoint, #171 correctly
recognized the recorded ``github_lifecycle_not_ready`` blocker as obsolete
and restarted the adoption - and the restart then failed twice in ten
seconds with ``Keine Änderungen zur Übernahme vorhanden.``

The reason was that adoption derived its file list from the *uncommitted*
worktree diff. An earlier attempt had already committed the verified
changes, so the worktree was clean and the diff empty, while the record's
``experiment_commit_sha`` - the only other way back to that commit - is
reset to NULL by every requeue.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from modules.experiment_adoption_service import ExperimentAdoptionError

from tests.test_experiment_adoption_service import AdoptionRepoFixture, git


class AdoptionCommittedWorkRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)

    def _seed(self) -> tuple[str, str]:
        experiment_id = "d" * 32
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py", "feature.py"],
            extra_untracked={"feature.py": "FEATURE = True\n"},
        )
        return experiment_id, self.fx.experiment_service.get_experiment(experiment_id).worktree_path

    def _commit_the_verified_work(self, worktree: str) -> str:
        """Do what adoption's own first step does, then fail before finishing."""
        git(worktree, "add", "-A")
        git(worktree, "-c", "user.email=n@x", "-c", "user.name=N", "commit", "-m", "Adopt experiment")
        return git(worktree, "rev-parse", "HEAD").stdout.strip()

    def _record_ci_checkpoint(self, experiment_id: str, sha: str) -> None:
        with self.fx.experiment_service._lock, self.fx.experiment_service._conn:
            self.fx.experiment_service._conn.execute(
                "UPDATE experiments SET test_command_json = ? WHERE id = ?",
                (json.dumps(["GitHub Actions", "tests.yml", sha]), experiment_id),
            )

    def test_retry_adopts_the_commit_an_earlier_attempt_left_behind(self):
        experiment_id, worktree = self._seed()
        checkpoint = self._commit_the_verified_work(worktree)
        self._record_ci_checkpoint(experiment_id, checkpoint)

        # This is the production state: nothing uncommitted, and the record
        # has no commit to resume from because requeuing cleared it.
        self.assertEqual(git(worktree, "status", "--short").stdout.strip(), "")
        self.assertIsNone(self.fx.adoption_service._resumable_commit_sha(experiment_id))

        validation = self.fx.adoption_service._validate_startable(
            experiment_id, persist_review=False
        )

        self.assertEqual(validation["committed_commit_sha"], checkpoint)
        self.assertEqual(sorted(validation["actual_files"]), ["app.py", "feature.py"])

    def test_uncommitted_work_still_takes_the_ordinary_path(self):
        experiment_id, _worktree = self._seed()

        validation = self.fx.adoption_service._validate_startable(
            experiment_id, persist_review=False
        )

        self.assertIsNone(validation["committed_commit_sha"])
        self.assertEqual(sorted(validation["actual_files"]), ["app.py", "feature.py"])

    def test_nothing_to_adopt_is_refused_before_an_attempt_is_spent(self):
        experiment_id, worktree = self._seed()
        # Throw the change away: the worktree is clean *and* carries nothing
        # beyond the target branch, so there really is nothing to integrate.
        git(worktree, "checkout", "--", ".")
        Path(worktree, "feature.py").unlink()
        self.assertEqual(git(worktree, "status", "--short").stdout.strip(), "")
        # Record that empty state as the verified checkpoint, so the content
        # invariant is satisfied and emptiness is what actually decides.
        self._record_ci_checkpoint(
            experiment_id, git(worktree, "rev-parse", "HEAD").stdout.strip()
        )

        with self.assertRaises(ExperimentAdoptionError) as caught:
            self.fx.adoption_service._validate_startable(experiment_id, persist_review=False)
        self.assertIn("Keine Änderungen zur Übernahme vorhanden.", str(caught.exception))

        # Refused by validation means the adoption record was never requeued,
        # so the bounded retry budget is untouched.
        self.assertEqual(self.fx.adoption_service.get(experiment_id).stale_retry_attempts, 0)

    def test_committed_work_that_is_not_the_verified_content_is_refused(self):
        experiment_id, worktree = self._seed()
        committed = self._commit_the_verified_work(worktree)
        # Record a *different* checkpoint: HEAD is then not the tree CI
        # accepted, so adopting it would integrate unverified content.
        self._record_ci_checkpoint(experiment_id, f"{'0' * 39}1")
        self.assertNotEqual(committed, "")

        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service._validate_startable(experiment_id, persist_review=False)

    def test_unresolvable_target_branch_is_not_reported_as_nothing_to_adopt(self):
        experiment_id, worktree = self._seed()
        checkpoint = self._commit_the_verified_work(worktree)
        self._record_ci_checkpoint(experiment_id, checkpoint)
        # Without a resolvable target branch there is no way to tell an empty
        # change from an unanswerable question. Saying "nothing to adopt" here
        # would be the original bug wearing a different hat.
        git(worktree, "update-ref", "-d", "refs/remotes/origin/main")

        with self.assertRaises(ExperimentAdoptionError) as caught:
            self.fx.adoption_service._validate_startable(experiment_id, persist_review=False)
        self.assertNotIn("Keine Änderungen zur Übernahme vorhanden.", str(caught.exception))
        self.assertIn("origin/main", str(caught.exception))

    def test_full_adoption_succeeds_from_the_committed_work(self):
        experiment_id, worktree = self._seed()
        checkpoint = self._commit_the_verified_work(worktree)
        self._record_ci_checkpoint(experiment_id, checkpoint)

        started = self.fx.adoption_service.start_adoption(experiment_id)
        self.assertIn(started.status, {"queued", "adopting"})
        adoption = self.fx.wait_terminal(experiment_id)

        self.assertEqual(adoption.status, "adopted", adoption.reason)
        self.assertEqual(adoption.experiment_commit_sha, checkpoint)
        self.assertEqual(sorted(adoption.actual_files or []), ["app.py", "feature.py"])


if __name__ == "__main__":
    unittest.main()
