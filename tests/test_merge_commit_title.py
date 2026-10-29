"""Squash merges and checkpoints name the issue, not only the experiment id (#423)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules.github_work_tracking import GitHubWorkRef, GitHubWorkTrackingService
from tests.test_github_work_tracking import FakeGitHub


class MergeCommitTitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeGitHub()
        self.service = GitHubWorkTrackingService(
            db_path=str(Path(self.tmp.name) / "nexus.db"),
            repo_root=self.tmp.name,
            repository="tire0011/agent-lab",
            transport=self.fake,
        )

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def _merge(self, ref: GitHubWorkRef) -> dict:
        self.fake.issues.setdefault(
            ref.issue_number, {"number": ref.issue_number, "title": ref.issue_title, "state": "open"}
        )
        self.fake.pulls.append({"number": 100, "state": "open", "merged": False})
        ref.pr_number = 100
        self.service.merge_and_complete(ref, expected_head_sha="head-sha")
        puts = [call for call in self.fake.calls if call[0] == "PUT" and call[1].endswith("/merge")]
        self.assertEqual(len(puts), 1)
        return puts[0][2]

    def test_merge_payload_names_issue_and_experiment(self):
        ref = GitHubWorkRef(
            issue_number=408,
            issue_title="WIP: Initiative tick: run the goal policy before planning",
            branch_name="nexus/408-initiative-tick",
            experiment_id="c0ddb4fc-1234-5678",
        )

        payload = self._merge(ref)

        self.assertEqual(payload["merge_method"], "squash")
        self.assertEqual(payload["sha"], "head-sha")
        self.assertEqual(
            payload["commit_title"], "Initiative tick: run the goal policy before planning (#408)"
        )
        self.assertFalse(payload["commit_title"].startswith("WIP"))
        self.assertIn("Closes #408", payload["commit_message"])
        self.assertIn("Nexus-Experiment: c0ddb4fc-1234-5678", payload["commit_message"])

    def test_long_title_is_cut_and_keeps_issue_suffix(self):
        ref = GitHubWorkRef(
            issue_number=423,
            issue_title="Squash merges of experiment PRs land on main as WIP without the issue title or number",
            branch_name="nexus/423-squash",
            experiment_id="exp-long",
        )

        payload = self._merge(ref)

        self.assertLessEqual(len(payload["commit_title"]), 72)
        self.assertTrue(payload["commit_title"].endswith(" (#423)"))
        self.assertTrue(payload["commit_title"].startswith("Squash merges of experiment PRs"))

    def test_ref_without_issue_keeps_plain_payload(self):
        ref = GitHubWorkRef(
            issue_number=0,
            issue_title="",
            branch_name="nexus/no-issue",
            experiment_id="exp-none",
        )
        self.fake.issues[0] = {"number": 0, "title": "", "state": "open"}

        payload = self._merge(ref)

        self.assertEqual(payload, {"merge_method": "squash", "sha": "head-sha"})

    def test_already_merged_pull_request_is_not_merged_again(self):
        ref = GitHubWorkRef(
            issue_number=42,
            issue_title="GitHub lifecycle",
            branch_name="nexus/42-github-lifecycle",
            pr_number=100,
        )
        self.fake.pulls.append(
            {"number": 100, "state": "closed", "merged": True, "merge_commit_sha": "old-sha"}
        )

        self.assertEqual(self.service.merge_and_complete(ref), "old-sha")
        self.assertFalse(any(call[0] == "PUT" for call in self.fake.calls))

    def test_checkpoint_commit_message_names_issue(self):
        repo = Path(self.tmp.name) / "repo"
        remote = Path(self.tmp.name) / "remote.git"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Nexus Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "nexus@example.test"], cwd=repo, check=True)
        (repo / "feature.py").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
        ).stdout.strip()
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=repo, check=True, capture_output=True)
        self.fake.refs["main"] = head
        ref = self.service.bind_experiment(
            self.service.ensure_for_source(
                SimpleNamespace(source_type="issue", source_id="42", title="GitHub lifecycle", content="#42")
            ),
            "checkpoint-exp-0123456789",
        )
        (repo / "feature.py").write_text("after\n", encoding="utf-8")

        real_git = GitHubWorkTrackingService._git
        git_calls: list[list[str]] = []

        def recording_git(args, **kwargs):
            git_calls.append(list(args))
            return real_git(args, **kwargs)

        with patch.object(GitHubWorkTrackingService, "_git", side_effect=recording_git):
            self.service.checkpoint_experiment("checkpoint-exp-0123456789", str(repo))

        commit_tree = [args for args in git_calls if args[0] == "commit-tree"]
        self.assertEqual(len(commit_tree), 1)
        message = commit_tree[0][commit_tree[0].index("-m") + 1]
        self.assertTrue(message.startswith("WIP: GitHub lifecycle (#42)"))
        self.assertIn("Nexus-Experiment: checkpoint-exp-0123456789", message)
        self.assertNotIn("WIP: Nexus experiment", message)

        remote_message = subprocess.run(
            ["git", f"--git-dir={remote}", "log", "-1", "--format=%B", ref.branch_name],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        self.assertTrue(remote_message.startswith("WIP: GitHub lifecycle (#42)"))


if __name__ == "__main__":
    unittest.main()
