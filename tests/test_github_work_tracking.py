from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

from modules.github_work_tracking import GitHubWorkTrackingError, GitHubWorkTrackingService


class FakeGitHub:
    def __init__(self):
        self.issues = {
            42: {"number": 42, "title": "GitHub lifecycle", "state": "open"},
        }
        self.next_issue = 43
        self.refs = {"main": "main-sha"}
        self.pulls = []
        self.calls = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))

        if method == "GET" and path.startswith("/repos/tire0011/agent-lab/issues/"):
            number = int(path.rsplit("/", 1)[1])
            return dict(self.issues[number])

        if method == "PATCH" and path.startswith("/repos/tire0011/agent-lab/issues/"):
            number = int(path.rsplit("/", 1)[1])
            self.issues[number].update(payload or {})
            return dict(self.issues[number])

        if method == "GET" and path.startswith("/search/issues?"):
            return {"items": []}

        if method == "POST" and path == "/repos/tire0011/agent-lab/issues":
            number = self.next_issue
            self.next_issue += 1
            issue = {
                "number": number,
                "title": payload["title"],
                "state": "open",
            }
            self.issues[number] = issue
            return dict(issue)

        if method == "GET" and "/git/ref/heads/" in path:
            encoded = path.split("/git/ref/heads/", 1)[1]
            branch = unquote(encoded)
            if branch not in self.refs:
                raise GitHubWorkTrackingError("GitHub HTTP 404: not found")
            return {"object": {"sha": self.refs[branch]}}

        if method == "POST" and path == "/repos/tire0011/agent-lab/git/refs":
            branch = payload["ref"].removeprefix("refs/heads/")
            self.refs[branch] = payload["sha"]
            return {"ref": payload["ref"], "object": {"sha": payload["sha"]}}

        if method == "GET" and path.startswith("/repos/tire0011/agent-lab/pulls?"):
            return [dict(pr) for pr in self.pulls if pr.get("state") == "open"]

        if method == "POST" and path == "/repos/tire0011/agent-lab/pulls":
            pr = {
                "number": len(self.pulls) + 100,
                "state": "open",
                "head": payload["head"],
                "base": payload["base"],
            }
            self.pulls.append(pr)
            return dict(pr)

        if method == "PUT" and path.endswith("/merge"):
            number = int(path.split("/pulls/", 1)[1].split("/", 1)[0])
            for pr in self.pulls:
                if pr["number"] == number:
                    pr["state"] = "closed"
                    return {"merged": True, "sha": "merged-sha"}
            raise AssertionError(f"unknown PR {number}")

        raise AssertionError(f"unexpected GitHub call: {method} {path} {payload}")


class GitHubWorkTrackingTests(unittest.TestCase):
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

    def test_creates_missing_database_parent_directory(self):
        nested_db = Path(self.tmp.name) / "state" / "nested" / "nexus.db"
        service = GitHubWorkTrackingService(
            db_path=str(nested_db),
            repo_root=self.tmp.name,
            repository="tire0011/agent-lab",
            transport=self.fake,
        )
        try:
            self.assertTrue(nested_db.exists())
        finally:
            service.close()

    def test_existing_issue_is_reused_and_two_experiments_keep_same_work_identity(self):
        source = SimpleNamespace(
            source_type="issue",
            source_id="42",
            title="GitHub lifecycle",
            content="GitHub Issue #42: GitHub lifecycle",
        )

        ref = self.service.ensure_for_source(source)
        first = self.service.bind_experiment(ref, "exp-one")
        second_ref = self.service.ensure_for_source(source)
        second = self.service.bind_experiment(second_ref, "exp-two")

        self.assertEqual(first.issue_number, 42)
        self.assertEqual(first.branch_name, "nexus/42-github-lifecycle")
        self.assertEqual(second.branch_name, first.branch_name)
        self.assertEqual(self.service.for_experiment("exp-one").issue_number, 42)
        self.assertEqual(self.service.for_experiment("exp-two").issue_number, 42)
        issue_gets = [
            call for call in self.fake.calls
            if call[0] == "GET" and call[1].endswith("/issues/42")
        ]
        self.assertEqual(len(issue_gets), 1)

    def test_task_without_issue_creates_issue_and_branch(self):
        source = SimpleNamespace(
            source_type="task",
            source_id="task-1",
            title="Add durable tracking",
            content="Add durable tracking before implementation.",
        )

        ref = self.service.ensure_for_source(source)

        self.assertEqual(ref.issue_number, 43)
        self.assertEqual(ref.branch_name, "nexus/43-add-durable-tracking")
        self.assertIn(ref.branch_name, self.fake.refs)
        self.assertTrue(any(call[0:2] == ("POST", "/repos/tire0011/agent-lab/issues") for call in self.fake.calls))

    def test_pr_merge_closes_issue_and_keeps_experiment_link(self):
        source = SimpleNamespace(
            source_type="issue",
            source_id="42",
            title="GitHub lifecycle",
            content="GitHub Issue #42: GitHub lifecycle",
        )
        ref = self.service.bind_experiment(self.service.ensure_for_source(source), "exp-merge")

        ref = self.service.ensure_pull_request(
            ref,
            title="Implement lifecycle",
            body="Fixes #42\n\nNexus experiment: exp-merge",
        )
        merge_sha = self.service.merge_and_complete(ref, expected_head_sha="head-sha")

        self.assertEqual(merge_sha, "merged-sha")
        self.assertEqual(self.fake.issues[42]["state"], "closed")
        stored = self.service.for_experiment("exp-merge")
        self.assertEqual(stored.pr_number, 100)
        self.assertEqual(stored.branch_name, "nexus/42-github-lifecycle")


if __name__ == "__main__":
    unittest.main()
