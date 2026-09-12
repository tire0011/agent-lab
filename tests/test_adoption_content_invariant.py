"""Adoption must judge content, not worktree layout.

Regression for the real production deadlock behind experiment ``3946adc3``:
adoption's own first step commits the verified changes, which turns the dirty
worktree into a clean one. The old guard compared ``git status --short`` and
``git diff --stat`` text against what verification had stored, so every resumed
adoption concluded the worktree had been tampered with and returned
``needs_review`` - eight times in a row, for a change whose CI was green.
"""

from __future__ import annotations

import json
import unittest

from modules.experiment_adoption_service import ExperimentAdoptionError

from tests.test_experiment_adoption_service import AdoptionRepoFixture, git


class AdoptionContentInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)

    def _seed(self) -> tuple[str, str]:
        experiment_id = "c" * 32
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py", "feature.py"],
            extra_untracked={"feature.py": "FEATURE = True\n"},
        )
        return experiment_id, self.fx.experiment_service.get_experiment(experiment_id).worktree_path

    def _checkpoint_the_work(self, worktree: str) -> str:
        """Do what adoption's first pass does: commit the verified changes."""
        git(worktree, "add", "-A")
        git(worktree, "-c", "user.email=n@x", "-c", "user.name=N", "commit", "-m", "Adopt experiment")
        return git(worktree, "rev-parse", "HEAD").stdout.strip()

    def _record_ci_checkpoint(self, experiment_id: str, sha: str) -> None:
        with self.fx.experiment_service._lock, self.fx.experiment_service._conn:
            self.fx.experiment_service._conn.execute(
                "UPDATE experiments SET test_command_json = ? WHERE id = ?",
                (json.dumps(["GitHub Actions", "tests.yml", sha]), experiment_id),
            )

    def test_already_checkpointed_worktree_stays_startable(self):
        experiment_id, worktree = self._seed()
        checkpoint = self._checkpoint_the_work(worktree)
        self._record_ci_checkpoint(experiment_id, checkpoint)

        # The worktree is now clean, so the stored status/stat text no longer
        # matches - the exact condition that used to deadlock the retry.
        self.assertEqual(git(worktree, "status", "--short").stdout.strip(), "")

        result = self.fx.adoption_service._validate_startable(experiment_id, persist_review=False)
        self.assertEqual(result["experiment"].id, experiment_id)

    def test_genuinely_changed_content_is_still_rejected(self):
        experiment_id, worktree = self._seed()
        checkpoint = self._checkpoint_the_work(worktree)
        self._record_ci_checkpoint(experiment_id, checkpoint)

        # Content that CI never saw must not pass, however it is arranged.
        (self.fx.repo / "unrelated.py").write_text("SNEAKY = True\n", encoding="utf-8")
        target = __import__("pathlib").Path(worktree) / "sneaky.py"
        target.write_text("SNEAKY = True\n", encoding="utf-8")

        with self.assertRaises(ExperimentAdoptionError) as caught:
            self.fx.adoption_service._validate_startable(experiment_id, persist_review=False)
        self.assertIn("verändert", str(caught.exception))

    def test_without_checkpoint_evidence_drift_is_still_rejected(self):
        experiment_id, worktree = self._seed()
        self._checkpoint_the_work(worktree)
        # No GitHub Actions checkpoint recorded -> nothing authoritative to
        # compare against, so the conservative text comparison still applies.
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service._validate_startable(experiment_id, persist_review=False)


if __name__ == "__main__":
    unittest.main()
