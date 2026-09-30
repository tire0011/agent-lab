from __future__ import annotations

import os
import tempfile
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

from modules.github_work_tracking import GitHubWorkTrackingError, GitHubWorkTrackingService


class FakeGitHub:
    def __init__(self):
        self.issues = {
            42: {"number": 42, "title": "GitHub lifecycle", "state": "open"},
        }
        self.next_issue = 43
        self.refs = {"main": "main-sha"}
        self.commit_parents = {}
        self.pulls = []
        self.workflow_runs = []
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

        if method == "GET" and path.startswith("/repos/tire0011/agent-lab/commits/"):
            sha = path.rsplit("/", 1)[1]
            parent_sha = self.commit_parents.get(sha, self.refs["main"])
            return {"sha": sha, "parents": [{"sha": parent_sha}]}

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

        if method == "GET" and "/actions/workflows/" in path:
            return {"workflow_runs": [dict(run) for run in self.workflow_runs]}

        if method == "POST" and path == "/repos/tire0011/agent-lab/pulls":
            pr = {
                "number": len(self.pulls) + 100,
                "state": "open",
                "head": payload["head"],
                "base": payload["base"],
                "draft": bool(payload.get("draft")),
            }
            self.pulls.append(pr)
            return dict(pr)

        if method == "PATCH" and "/pulls/" in path:
            number = int(path.rsplit("/", 1)[1])
            for pr in self.pulls:
                if pr["number"] == number:
                    pr.update(payload or {})
                    return dict(pr)
            raise AssertionError(f"unknown PR {number}")

        if method == "GET" and "/pulls/" in path and "?" not in path:
            number = int(path.rsplit("/", 1)[1])
            for pr in self.pulls:
                if pr["number"] == number:
                    return {**pr, "node_id": f"PR_{number}"}
            raise AssertionError(f"unknown PR {number}")

        if method == "PUT" and path.endswith("/merge"):
            number = int(path.split("/pulls/", 1)[1].split("/", 1)[0])
            for pr in self.pulls:
                if pr["number"] == number:
                    pr["state"] = "closed"
                    pr["merged"] = True
                    pr["merge_commit_sha"] = "merged-sha"
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

    def _checkpoint_repo(self):
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
                SimpleNamespace(
                    source_type="task", source_id="checkpoint", title="Checkpoint", content="Checkpoint"
                )
            ),
            "checkpoint-exp",
        )
        return repo, remote, ref, head

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

    def test_unconfigured_service_blocks_before_creating_issue(self):
        # The token falls back to the environment, so the test runner's own
        # GITHUB_TOKEN/GH_TOKEN would otherwise make this service configured
        # and the assertion would test a live API call instead of the guard.
        with patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}):
            service = GitHubWorkTrackingService(
                db_path=str(Path(self.tmp.name) / "unconfigured.db"),
                repo_root=self.tmp.name,
                repository="tire0011/agent-lab",
            )
        try:
            with self.assertRaises(GitHubWorkTrackingError) as ctx:
                service.ensure_for_source(
                    SimpleNamespace(
                        source_type="task",
                        source_id="blocked",
                        title="Blocked",
                        content="Do not start",
                    )
                )
            self.assertIn("GITHUB_TOKEN/GH_TOKEN", str(ctx.exception))
        finally:
            service.close()

    def test_legacy_recovery_reuses_experiment_identity(self):
        experiment = SimpleNamespace(id="legacy-exp", goal="Recover this change")

        first = self.service.recover_experiment(experiment)
        second = self.service.recover_experiment(experiment)

        self.assertEqual(first.issue_number, second.issue_number)
        self.assertEqual(first.branch_name, second.branch_name)
        self.assertEqual(self.service.for_experiment("legacy-exp").branch_name, first.branch_name)

    def test_checkpoint_pushes_publishable_files_and_preserves_worktree(self):
        repo, remote, ref, _ = self._checkpoint_repo()
        (repo / "feature.py").write_text("after\n", encoding="utf-8")
        (repo / "communication").mkdir()
        (repo / "communication" / "runtime.md").write_text("runtime\n", encoding="utf-8")

        published = self.service.checkpoint_experiment("checkpoint-exp", str(repo))

        self.assertEqual(published.pr_number, 100)
        self.assertTrue(self.fake.pulls[0]["draft"])
        remote_text = subprocess.run(
            ["git", f"--git-dir={remote}", "show", f"{ref.branch_name}:feature.py"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        self.assertEqual(remote_text, "after\n")
        missing_runtime = subprocess.run(
            ["git", f"--git-dir={remote}", "cat-file", "-e", f"{ref.branch_name}:communication/runtime.md"],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(missing_runtime.returncode, 0)
        status = subprocess.run(
            ["git", "status", "--short"], cwd=repo, check=True, text=True, capture_output=True
        ).stdout
        self.assertIn("feature.py", status)
        self.assertIn("communication/", status)

    def test_checkpoint_uses_worktree_head_when_issue_branch_is_only_behind(self):
        repo, remote, _, _ = self._checkpoint_repo()
        (repo / "committed.py").write_text("already committed\n", encoding="utf-8")
        subprocess.run(["git", "add", "committed.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "experiment progress"], cwd=repo, check=True, capture_output=True)
        worktree_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
        ).stdout.strip()
        (repo / "feature.py").write_text("after\n", encoding="utf-8")

        self.service.checkpoint_experiment("checkpoint-exp", str(repo))

        checkpoint_sha = self.service.for_experiment("checkpoint-exp").checkpoint_sha
        parent = subprocess.run(
            ["git", f"--git-dir={remote}", "rev-parse", f"{checkpoint_sha}^"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        self.assertEqual(parent, worktree_head)

    def test_checkpoint_rejects_issue_branch_commit_missing_from_worktree(self):
        repo, _, ref, base_sha = self._checkpoint_repo()
        (repo / "committed.py").write_text("experiment progress\n", encoding="utf-8")
        subprocess.run(["git", "add", "committed.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "experiment progress"], cwd=repo, check=True, capture_output=True)
        (repo / "feature.py").write_text("after\n", encoding="utf-8")
        branch_sha = subprocess.run(
            ["git", "commit-tree", f"{base_sha}^{{tree}}", "-p", base_sha, "-m", "branch-only commit"],
            cwd=repo,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        self.fake.refs[ref.branch_name] = branch_sha

        with self.assertRaisesRegex(GitHubWorkTrackingError, "manual reconciliation required"):
            self.service.checkpoint_experiment("checkpoint-exp", str(repo))

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

    def test_failed_verification_is_written_to_existing_draft_pr(self):
        source = SimpleNamespace(
            source_type="issue",
            source_id="42",
            title="GitHub lifecycle",
            content="GitHub Issue #42: GitHub lifecycle",
        )
        ref = self.service.bind_experiment(
            self.service.ensure_for_source(source), "exp-failed"
        )
        self.service.ensure_pull_request(
            ref,
            title="WIP lifecycle",
            body="Verification pending",
            draft=True,
        )

        self.service.record_verification(
            "exp-failed",
            status="failed",
            command=["python", "-m", "unittest"],
            exit_code=1,
            error="tests failed (exit code 1)",
            output="FAILED test_example",
        )

        body = self.fake.pulls[0]["body"]
        self.assertIn("python -m unittest", body)
        self.assertIn("Exit code: `1`", body)
        self.assertIn("FAILED test_example", body)

    def _bind_checkpoint(self, sha: str = "checkpoint-sha"):
        ref = self.service.bind_experiment(
            self.service.ensure_for_source(
                SimpleNamespace(
                    source_type="task",
                    source_id=f"ci-{sha}",
                    title="CI verification",
                    content="Verify with CI",
                )
            ),
            f"experiment-{sha}",
        )
        ref.checkpoint_sha = sha
        self.service._persist_experiment(ref)
        return ref

    def test_exact_checkpoint_ci_success_is_authoritative(self):
        ref = self._bind_checkpoint()
        self.fake.workflow_runs = [
            {
                "id": 17,
                "run_attempt": 1,
                "head_sha": ref.checkpoint_sha,
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.example/runs/17",
            }
        ]

        result = self.service.wait_for_ci(ref.experiment_id, poll_interval_seconds=0.01)

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.commit_sha, ref.checkpoint_sha)
        self.assertEqual(result.run_id, 17)

    def test_completed_ci_failure_is_authoritative(self):
        ref = self._bind_checkpoint("failed-sha")
        self.fake.workflow_runs = [
            {
                "id": 18,
                "run_attempt": 1,
                "head_sha": ref.checkpoint_sha,
                "event": "push",
                "status": "completed",
                "conclusion": "failure",
            }
        ]

        result = self.service.wait_for_ci(ref.experiment_id, poll_interval_seconds=0.01)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.conclusion, "failure")

    def test_repaired_checkpoint_parent_previous_checkpoint_is_authoritative(self):
        # #210: a repair commit's direct parent is the previous checkpoint
        # commit, not `main`. That must not stop wait_for_ci from
        # recognizing CI for the exact repaired SHA.
        ref = self._bind_checkpoint("repaired-sha")
        self.fake.commit_parents["repaired-sha"] = "previous-checkpoint-sha"
        self.fake.workflow_runs = [
            {
                "id": 21,
                "run_attempt": 1,
                "head_sha": ref.checkpoint_sha,
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]

        result = self.service.wait_for_ci(ref.experiment_id, poll_interval_seconds=0.01)

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.commit_sha, ref.checkpoint_sha)

    def test_repaired_checkpoint_persisted_evidence_reusable_when_main_unchanged(self):
        # Review follow-up on #210/#211: the base persisted alongside a CI
        # verdict must be the current target-branch head, not the
        # checkpoint commit's git parent. A repair checkpoint's parent is
        # the previous checkpoint commit - if that were persisted as
        # `base_sha`, `recorded_ci_for` would reject the verdict on every
        # restart even though `main` never moved.
        ref = self._bind_checkpoint("repaired-sha-2")
        self.fake.commit_parents["repaired-sha-2"] = "previous-checkpoint-sha-2"
        self.fake.workflow_runs = [
            {
                "id": 24,
                "run_attempt": 1,
                "head_sha": ref.checkpoint_sha,
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]

        result = self.service.wait_for_ci(ref.experiment_id, poll_interval_seconds=0.01)
        self.assertEqual(result.status, "passed")

        current_main = self.fake.refs["main"]
        reused = self.service.recorded_ci_for(ref.checkpoint_sha, base_sha=current_main)

        self.assertIsNotNone(reused)
        self.assertEqual(reused.status, "passed")

    def test_ci_result_for_different_commit_is_ignored(self):
        ref = self._bind_checkpoint("wanted-sha")
        self.fake.workflow_runs = [
            {
                "id": 19,
                "run_attempt": 1,
                "head_sha": "different-sha",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]

        result = self.service.wait_for_ci(
            ref.experiment_id,
            start_timeout_seconds=0,
            poll_interval_seconds=0.01,
        )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("no matching", result.reason)

    def test_incomplete_ci_uses_local_fallback(self):
        ref = self._bind_checkpoint("queued-sha")
        self.fake.workflow_runs = [
            {
                "id": 20,
                "run_attempt": 1,
                "head_sha": ref.checkpoint_sha,
                "event": "push",
                "status": "in_progress",
                "conclusion": None,
            }
        ]

        result = self.service.wait_for_ci(
            ref.experiment_id,
            completion_timeout_seconds=0,
            poll_interval_seconds=0.01,
        )

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.run_id, 20)

    def test_pull_request_run_success_is_authoritative(self):
        # #210: CI triggered by `pull_request` (the normal case once the
        # checkpoint lives on a PR branch) is just as authoritative as
        # `push`.
        ref = self._bind_checkpoint("pr-triggered-sha")
        self.fake.workflow_runs = [
            {
                "id": 22,
                "run_attempt": 1,
                "head_sha": ref.checkpoint_sha,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            }
        ]

        result = self.service.wait_for_ci(ref.experiment_id, poll_interval_seconds=0.01)

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.commit_sha, ref.checkpoint_sha)

    def test_push_run_success_is_authoritative(self):
        ref = self._bind_checkpoint("push-triggered-sha")
        self.fake.workflow_runs = [
            {
                "id": 23,
                "run_attempt": 1,
                "head_sha": ref.checkpoint_sha,
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]

        result = self.service.wait_for_ci(ref.experiment_id, poll_interval_seconds=0.01)

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.commit_sha, ref.checkpoint_sha)

    def test_no_ci_run_within_start_timeout_is_unavailable_for_local_fallback(self):
        ref = self._bind_checkpoint("no-run-sha")
        self.fake.workflow_runs = []

        result = self.service.wait_for_ci(
            ref.experiment_id,
            start_timeout_seconds=0,
            poll_interval_seconds=0.01,
        )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("no matching", result.reason)


if __name__ == "__main__":
    unittest.main()
