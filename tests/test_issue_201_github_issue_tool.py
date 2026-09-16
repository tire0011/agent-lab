from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.github_work_tracking import GitHubWorkTrackingService
from modules.nexus_tool_router import InMemoryToolGrantLedger, NexusToolRouter
from modules.operations_registry import OperationValidationError, OperationsRegistry


class _IssueService:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.closed = False

    def get_issue(self, issue_number: int):
        self.calls.append(issue_number)
        return {
            "number": issue_number,
            "title": "Expose GitHub Issue retrieval",
            "body": "Acceptance criteria from the Issue",
            "body_truncated": False,
            "state": "open",
            "url": f"https://github.com/tire0011/agent-lab/issues/{issue_number}",
            "author": "tire0011",
            "labels": ["enhancement"],
            "assignees": [],
            "created_at": "2026-09-16T00:00:00Z",
            "updated_at": "2026-09-16T00:00:00Z",
        }

    def close(self) -> None:
        self.closed = True


class GitHubIssueCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.issue_service = _IssueService()
        self.registry = OperationsRegistry(
            experiment_service=SimpleNamespace(),
            db_path=str(Path(self.tmp.name) / "agent.db"),
            repo_root=self.tmp.name,
            github_work_tracking_factory=lambda **_kwargs: self.issue_service,
        )
        self.addCleanup(self.registry.close)

    def test_transport_fetch_is_get_only_and_returns_bounded_structured_data(self):
        calls = []

        def transport(method, path, payload):
            calls.append((method, path, payload))
            return {
                "number": 184,
                "title": "Task specification",
                "body": "x" * 20_000,
                "state": "open",
                "html_url": "https://github.com/tire0011/agent-lab/issues/184",
                "user": {"login": "nexus"},
                "labels": [{"name": "enhancement"}],
            }

        service = GitHubWorkTrackingService(
            db_path=str(Path(self.tmp.name) / "github.db"),
            repo_root=self.tmp.name,
            repository="tire0011/agent-lab",
            transport=transport,
        )
        self.addCleanup(service.close)

        issue = service.get_issue(184)

        self.assertEqual(calls, [("GET", "/repos/tire0011/agent-lab/issues/184", None)])
        self.assertEqual(issue["number"], 184)
        self.assertEqual(issue["title"], "Task specification")
        self.assertEqual(issue["state"], "open")
        self.assertTrue(issue["body_truncated"])
        self.assertEqual(len(issue["body"]), 16_000)

    def test_issue_is_discoverable_and_callable_through_shared_router(self):
        async def resolve(_prompt, **_kwargs):
            return json.dumps({
                "status": "found",
                "operation": "github_issue",
                "arguments": {"issue_number": 184},
                "reason": "The capability reads one GitHub Issue.",
            }), "local"

        router = NexusToolRouter(
            operations_registry=self.registry,
            schema_completion=resolve,
            grant_ledger=InMemoryToolGrantLedger(),
        )
        candidates = router.candidate_operations("get details for GitHub issue 184")
        self.assertIn("github_issue", [spec.name for spec in candidates])
        self.assertFalse(next(spec for spec in candidates if spec.name == "github_issue").mutates_state)

        found = asyncio.run(router.resolve_find_tool(
            "read GitHub issue #184",
            session_id="solve-184",
            current_task="solve github issue #184",
        ))
        self.assertEqual(found["status"], "found")
        self.assertEqual(found["resolved_arguments"], {"issue_number": 184})

        observation = router.execute_operation({
            "operation": "github_issue",
            "args": found["resolved_arguments"],
            "session_id": "solve-184",
        })
        self.assertTrue(observation["result"]["success"])
        self.assertIn("Acceptance criteria", "\n".join(observation["result"]["output_lines"]))
        self.assertEqual(self.issue_service.calls, [184])
        self.assertTrue(self.issue_service.closed)

    def test_invalid_issue_number_is_rejected_before_transport(self):
        with self.assertRaises(OperationValidationError):
            self.registry.execute("github_issue", {"issue_number": 0})
        self.assertEqual(self.issue_service.calls, [])

    def test_api_failure_is_an_explicit_tool_failure(self):
        class _FailingIssueService(_IssueService):
            def get_issue(self, issue_number: int):
                raise RuntimeError(f"GitHub HTTP 503 while reading Issue {issue_number}")

        service = _FailingIssueService()
        registry = OperationsRegistry(
            experiment_service=SimpleNamespace(),
            db_path=str(Path(self.tmp.name) / "failure.db"),
            repo_root=self.tmp.name,
            github_work_tracking_factory=lambda **_kwargs: service,
        )
        self.addCleanup(registry.close)

        result = registry.execute("github_issue", {"issue_number": 184})

        self.assertFalse(result.success)
        self.assertIn("GitHub HTTP 503", result.error_summary)
        self.assertTrue(service.closed)


if __name__ == "__main__":
    unittest.main()
