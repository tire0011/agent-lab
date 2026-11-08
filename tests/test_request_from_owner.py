"""Nexus asks the owner by issue when it needs something it cannot get (#592)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from modules.github_work_tracking import GitHubWorkTrackingError, GitHubWorkTrackingService
from modules.operations_registry import OperationsRegistry


class _GitHub:
    def __init__(self, *, open_titles=(), created_today=0):
        self.open_titles = list(open_titles)
        self.created_today = created_today
        self.posts = []

    def __call__(self, method, path, payload):
        if method == "GET" and "created" in path:
            return {"total_count": self.created_today}
        if method == "GET":
            return {"items": [{"number": 7, "title": t, "html_url": "u7"} for t in self.open_titles]}
        self.posts.append(payload)
        return {"number": 9, "html_url": "u9"}


def _service(tmp: str, github: _GitHub) -> GitHubWorkTrackingService:
    return GitHubWorkTrackingService(
        db_path=str(Path(tmp) / "db"), repo_root=tmp, repository="o/r", transport=github
    )


class NeedIssueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def test_a_need_opens_a_proposal_issue(self):
        github = _GitHub()
        issue = _service(self.tmp, github).open_need_issue("A GPU", "for a bigger model")
        self.assertEqual(issue, {"number": 9, "url": "u9", "reused": False})
        self.assertEqual(github.posts[0]["title"], "Nexus needs: A GPU")
        self.assertEqual(github.posts[0]["labels"], ["proposal"])

    def test_the_same_open_need_is_reused(self):
        github = _GitHub(open_titles=["Nexus needs: A GPU"])
        issue = _service(self.tmp, github).open_need_issue("A GPU", "again")
        self.assertTrue(issue["reused"])
        self.assertEqual(github.posts, [])

    def test_at_most_three_new_needs_a_day(self):
        github = _GitHub(created_today=3)
        with self.assertRaises(GitHubWorkTrackingError):
            _service(self.tmp, github).open_need_issue("A GPU", "body")
        self.assertEqual(github.posts, [])


class RequestFromOwnerOperationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = MagicMock()
        self.service.open_need_issue.return_value = {"number": 9, "url": "u9", "reused": False}
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 3, stdout="", stderr="refused: docker.io is on the denylist"))
        self.registry = OperationsRegistry(
            experiment_service=SimpleNamespace(),
            db_path=str(Path(tmp.name) / "agent.db"),
            repo_root=tmp.name,
            subprocess_runner=self.runner,
            github_work_tracking_factory=lambda **_kwargs: self.service,
        )
        self.addCleanup(self.registry.close)

    def test_it_opens_the_need_issue(self):
        result = self.registry.execute("request_from_owner", {"title": "A GPU", "body": "bigger model"})
        self.assertTrue(result.success, result)
        self.assertIn("issue #9 opened", result.output_lines[0])

    def test_a_github_failure_is_a_failed_result(self):
        self.service.open_need_issue.side_effect = GitHubWorkTrackingError("daily limit of 3 need issues reached")
        result = self.registry.execute("request_from_owner", {"title": "A GPU", "body": "bigger model"})
        self.assertFalse(result.success)
        self.assertIn("daily limit", result.error_summary)

    def test_a_denied_install_points_at_the_owner(self):
        result = self.registry.execute("install_tool", {"package": "docker.io"})
        self.assertFalse(result.success)
        self.assertIn("request_from_owner", result.output_lines[-1])


class MissingCapabilityTests(unittest.TestCase):
    def test_missing_capability_asks_the_owner(self):
        from modules.task_orchestrator import NexusTaskOrchestrator

        with tempfile.TemporaryDirectory() as tmp:
            registry = MagicMock(repo_root=tmp)
            orchestrator = NexusTaskOrchestrator.__new__(NexusTaskOrchestrator)
            orchestrator.operations_registry = registry
            decision = SimpleNamespace(session_id="s", message="An API key for Mistral", reason="free tier")
            orchestrator._record_missing_capability("task text", decision)
        name, args = registry.execute.call_args.args
        self.assertEqual(name, "request_from_owner")
        self.assertEqual(args["title"], "An API key for Mistral")
        self.assertIn("free tier", args["body"])


if __name__ == "__main__":
    unittest.main()
