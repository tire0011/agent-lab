from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.github_work_tracking import (
    GitHubWorkRef,
    GitHubWorkTrackingError,
    GitHubWorkTrackingService,
)
from modules.nexus_planner import planning_prompt
from tests.test_nexus_initiative import InitiativeFixture


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


class BranchDriftRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.origin, self.seed, self.repo = root / "origin.git", root / "seed", root / "repo"
        git(root, "init", "--bare", str(self.origin))
        git(root, "init", "-b", "main", str(self.seed))
        for repo in (self.seed,):
            git(repo, "config", "user.email", "nexus@example.invalid")
            git(repo, "config", "user.name", "Nexus test")
        (self.seed / "shared.txt").write_text("base\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "base")
        git(self.seed, "remote", "add", "origin", str(self.origin))
        git(self.seed, "push", "-u", "origin", "main")
        git(root, "clone", "-b", "main", str(self.origin), str(self.repo))
        git(self.repo, "config", "user.email", "nexus@example.invalid")
        git(self.repo, "config", "user.name", "Nexus test")
        git(self.repo, "switch", "-c", "nexus/test")
        self.db = str(root / "state.db")
        self.service = GitHubWorkTrackingService(db_path=self.db, repo_root=str(self.repo))
        self.addCleanup(self.service.close)
        self.service._persist_experiment(GitHubWorkRef(623, "drift", "nexus/test", experiment_id="exp"))

    def commit_branch_change(self, path: str = "branch.txt", content: str = "branch\n") -> None:
        target = self.repo / path
        if content:
            target.write_text(content)
        else:
            target.unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "branch change")

    def commit_main_change(self, path: str, content: str = "main\n") -> None:
        target = self.seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-m", f"main {path}")
        git(self.seed, "push", "origin", "main")

    def test_zero_drift_and_unrelated_main_change(self) -> None:
        self.commit_branch_change()
        drift = self.service.branch_drift("exp")
        self.assertEqual(0, drift["commits_behind"])
        self.assertEqual([], drift["overlap_files"])
        self.commit_main_change("main.txt")
        drift = self.service.branch_drift("exp")
        self.assertEqual(1, drift["commits_behind"])
        self.assertEqual([], drift["overlap_files"])

    def test_overlap_and_restricted_refspec_refreshes_stale_origin_main(self) -> None:
        self.commit_branch_change("shared.txt", "branch\n")
        git(self.repo, "config", "remote.origin.fetch", "+refs/heads/nexus/*:refs/remotes/origin/nexus/*")
        stale = git(self.repo, "rev-parse", "refs/remotes/origin/main")
        self.commit_main_change("shared.txt", "main\n")
        drift = self.service.branch_drift("exp")
        self.assertEqual(1, drift["commits_behind"])
        self.assertEqual(["shared.txt"], drift["overlap_files"])
        self.assertNotEqual(stale, git(self.repo, "rev-parse", "refs/remotes/origin/main"))

    def test_main_rename_overlaps_branch_modify_and_delete(self) -> None:
        self.commit_branch_change("shared.txt", "branch\n")
        git(self.seed, "mv", "shared.txt", "renamed.txt")
        git(self.seed, "commit", "-m", "rename shared")
        git(self.seed, "push", "origin", "main")
        self.assertIn("shared.txt", self.service.branch_drift("exp")["overlap_files"])

        git(self.repo, "reset", "--hard", "main")
        git(self.repo, "switch", "-C", "nexus/test", "main")
        self.commit_branch_change("shared.txt", "")
        self.assertIn("shared.txt", self.service.branch_drift("exp")["overlap_files"])

    def test_missing_branch_mismatched_worktree_and_git_failures_are_unknown(self) -> None:
        self.service._persist_experiment(GitHubWorkRef(624, "missing", "nexus/missing", experiment_id="missing"))
        self.assertEqual("unknown", self.service.branch_drift("missing")["status"])
        other = Path(self.temp.name) / "other"
        git(Path(self.temp.name), "clone", "-b", "main", str(self.origin), str(other))
        self.assertEqual("unknown", self.service.branch_drift("exp", str(other))["status"])
        self.assertEqual("unknown", self.service.branch_drift("exp", str(other / "missing"))["status"])
        real_git = self.service._git

        def fetch_timeout(args, **kwargs):
            if args[0] == "fetch":
                raise subprocess.TimeoutExpired("git fetch", 1)
            return real_git(args, **kwargs)

        with patch.object(self.service, "_git", side_effect=fetch_timeout):
            self.assertEqual("unknown", self.service.branch_drift("exp")["status"])

        def diff_failure(args, **kwargs):
            if args[0] == "diff":
                raise GitHubWorkTrackingError("diff failed")
            return real_git(args, **kwargs)

        with patch.object(self.service, "_git", side_effect=diff_failure):
            self.assertEqual("unknown", self.service.branch_drift("exp")["status"])


class PlannerDriftTests(unittest.TestCase):
    def test_needs_review_snapshot_and_reader_exception_are_explicit(self) -> None:
        fx = InitiativeFixture()
        fx.add_experiment("e1", "needs_review", adoption="needs_review")
        service = fx.service(drift_reader=lambda _id, _path: {
            "commits_behind": 7, "overlap_files": ["modules/x.py"]
        })
        self.addCleanup(service.close)
        prompt = planning_prompt([], service._recent_experiments())
        self.assertIn("needs_review / merge: needs_review", prompt)
        self.assertIn("commits_behind=7, overlap_files=['modules/x.py']", prompt)

        broken = fx.service(drift_reader=lambda _id, _path: (_ for _ in ()).throw(RuntimeError("boom")))
        self.addCleanup(broken.close)
        prompt = planning_prompt([], broken._recent_experiments())
        self.assertIn("commits_behind=unknown, overlap_files=unknown", prompt)


if __name__ == "__main__":
    unittest.main()
