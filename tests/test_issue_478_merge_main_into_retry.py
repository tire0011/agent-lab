"""#478: a retry first merges the current main into its issue branch."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from modules.experiment_service import ExperimentService


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


class MergeMainIntoRetryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.origin = root / "origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = root / "repo"
        git(root, "clone", "-q", str(self.origin), str(self.repo))
        (self.repo / "app.py").write_text("VALUE = 1\n")
        git(self.repo, "add", "app.py")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "push", "-q", "origin", "HEAD:main")
        # The issue branch: an earlier attempt on top of the old main.
        self.branch = root / "branch"
        git(self.repo, "worktree", "add", "-q", "-b", "issue", str(self.branch))
        self.elog = MagicMock()
        self.service = ExperimentService.__new__(ExperimentService)

    def _move_main(self, content):
        (self.repo / "app.py").write_text(content)
        git(self.repo, "commit", "-qam", "main moved")
        git(self.repo, "push", "-q", "origin", "HEAD:main")

    def test_up_to_date_branch_is_left_alone(self):
        self.assertEqual(self.service._merge_current_main(str(self.branch), self.elog), "")

    def test_clean_merge_brings_main_in(self):
        (self.branch / "other.py").write_text("X = 1\n")
        git(self.branch, "add", "other.py")
        git(self.branch, "commit", "-qm", "attempt")
        self._move_main("VALUE = 2\n")
        self.assertEqual(self.service._merge_current_main(str(self.branch), self.elog), "")
        self.assertEqual((self.branch / "app.py").read_text(), "VALUE = 2\n")

    def test_conflict_is_left_for_the_coding_agent(self):
        (self.branch / "app.py").write_text("VALUE = 'attempt'\n")
        git(self.branch, "commit", "-qam", "attempt")
        self._move_main("VALUE = 'main'\n")
        note = self.service._merge_current_main(str(self.branch), self.elog)
        self.assertIn("conflict markers", note)
        self.assertIn("app.py", note)
        self.assertIn("<<<<<<<", (self.branch / "app.py").read_text())
        self.assertEqual(git(self.branch, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
