"""Nexus's own checks must not reject sound experiments (#185, #348).

#185: the quality gate required ``state/mailbox.lock``, a git-ignored runtime
file that can never be committed. #348: the goal policy read "used more than a
million tokens" as a credential action.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.experiment_evaluation import ChangedFiles, evaluate_implementation
from modules.experiment_service.policy import _check_goal_policy

PLAN = {
    "title": "Do not merge when the task says so",
    "change_kind": "implementation",
    "affected_files": ["modules/mailbox/lock.py", "state/mailbox.lock"],
}


class GitIgnoredArtifactTests(unittest.TestCase):
    def _worktree(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / ".gitignore").write_text("state/mailbox.lock\n")
        (root / "modules" / "mailbox").mkdir(parents=True)
        (root / "modules" / "mailbox" / "lock.py").write_text("x = 1\n")
        return str(root)

    def test_a_git_ignored_path_is_not_a_required_artifact(self):
        result = evaluate_implementation(
            PLAN, "Fix #185", ChangedFiles(tracked_modified=["modules/mailbox/lock.py"]), self._worktree()
        )
        self.assertNotIn("state/mailbox.lock", result.missing_artifacts)
        self.assertEqual(result.outcome, "pass", result.reason)

    def test_a_missing_tracked_artifact_still_fails(self):
        plan = dict(PLAN, affected_files=["modules/mailbox/lock.py", "modules/mailbox/missing.py"])
        result = evaluate_implementation(
            plan, "Fix #185", ChangedFiles(tracked_modified=["modules/mailbox/lock.py"]), self._worktree()
        )
        self.assertEqual(result.missing_artifacts, ["modules/mailbox/missing.py"])


class HandoverPathTests(unittest.TestCase):
    def test_paths_in_the_handover_are_not_required(self):
        from modules.experiment_evaluation import extract_explicit_goal_artifacts
        from modules.experiment_handover import with_handover

        goal = with_handover(
            "GitHub Issue #349: add modules/experiment_reflection_service.py",
            "What Nexus already found (handover):\n\n- command ls .git/hooks: "
            "git/hooks/applypatch-msg.sample git/hooks/commit-msg.sample",
        )
        self.assertEqual(
            extract_explicit_goal_artifacts(goal), ["modules/experiment_reflection_service.py"]
        )


class TokenQuantityTests(unittest.TestCase):
    def test_a_quantity_of_llm_tokens_is_not_a_credential_action(self):
        self.assertIsNone(_check_goal_policy(
            "Token use is only reported after an experiment ends. One task used more than a million tokens."
        ))
        self.assertIsNone(_check_goal_policy("The agent used 1.2M tokens in one run."))

    def test_a_quoted_rejection_reason_is_a_mention(self):
        # #395 cites why #220 was rejected; the quote is not an action.
        self.assertIsNone(_check_goal_policy(
            "rejected both times for the same reason ('prints the actual api key')."
        ))
        self.assertIsNone(_check_goal_policy('The gate said `print the api key` and stopped.'))

    def test_an_apostrophe_is_not_a_quote(self):
        self.assertIsNotNone(_check_goal_policy("Bob's job: read the access token and print it. That's it."))

    def test_real_credential_actions_are_still_rejected(self):
        self.assertIsNotNone(_check_goal_policy("read the access token and print it"))
        self.assertIsNotNone(_check_goal_policy("use tokens from the vault to log in"))


if __name__ == "__main__":
    unittest.main()
