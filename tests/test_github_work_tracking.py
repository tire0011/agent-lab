from __future__ import annotations

import os
import tempfile
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import unquote
from urllib.request import Request

from modules.github_work_tracking import (
    _CrossHostRedirectDropsToken,
    GitHubWorkTrackingError,
    GitHubWorkTrackingService,
    failing_test_names,
)


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
        self.comments = []
        self.fail_comments = False
        self.jobs = []
        self.job_log = ""
        self.calls = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))

        if method == "POST" and path.endswith("/comments"):
            if self.fail_comments:
                raise GitHubWorkTrackingError("GitHub HTTP 502: bad gateway")
            number = int(path.split("/issues/", 1)[1].split("/", 1)[0])
            self.comments.append((number, payload["body"]))
            return {"id": len(self.comments)}

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

        if method == "GET" and path.endswith("/jobs"):
            if isinstance(self.jobs, Exception):
                raise self.jobs
            return {"jobs": self.jobs}

        if method == "GET" and path.endswith("/logs"):
            if isinstance(self.job_log, Exception):
                raise self.job_log
            return self.job_log

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

    def test_existing_issue_branch_is_fetched_as_next_experiment_base_and_checkpoints(self):
        repo, _, ref, _ = self._checkpoint_repo()
        self.service.repo_root = str(repo)
        (repo / "continued.py").write_text("first experiment\n", encoding="utf-8")
        subprocess.run(["git", "add", "continued.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "first experiment"],
            cwd=repo, check=True, capture_output=True,
        )
        issue_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            text=True, capture_output=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "push", "origin", f"HEAD:refs/heads/{ref.branch_name}"],
            cwd=repo, check=True, capture_output=True,
        )
        self.fake.refs[ref.branch_name] = issue_sha

        base_sha = self.service.experiment_base_sha("checkpoint-exp")
        self.assertEqual(base_sha, issue_sha)

        (repo / "second.py").write_text("second experiment\n", encoding="utf-8")
        published = self.service.checkpoint_experiment("checkpoint-exp", str(repo))

        self.assertIsNotNone(published.checkpoint_sha)
        remote_text = subprocess.run(
            ["git", f"--git-dir={Path(self.tmp.name) / 'remote.git'}", "show",
             f"{ref.branch_name}:continued.py"],
            check=True, text=True, capture_output=True,
        ).stdout
        self.assertEqual(remote_text, "first experiment\n")

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

    def test_capability_issue_records_recovery_and_deduplicates(self):
        first = self.service.ensure_capability_issue(
            capability="repository text-search capability",
            missing_executable="rg",
            attempted_command="rg -n VERSION .",
            recovery="repository text search: rg -> grep -R -E",
            origin="session-120",
        )
        title = self.fake.issues[first["number"]]["title"]

        def search_with_existing(method, path, payload=None):
            if method == "GET" and path.startswith("/search/issues?"):
                issue = self.fake.issues[first["number"]]
                return {"items": [{**issue, "html_url": "https://example/43"}]}
            return self.fake(method, path, payload)

        self.service._transport = search_with_existing
        second = self.service.ensure_capability_issue(
            capability="repository text-search capability",
            missing_executable="rg",
            attempted_command="rg TODO",
            recovery="search_repository_text",
        )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["number"], first["number"])
        self.assertEqual(title, "Provide repository text-search capability when rg is unavailable")

    def test_controller_task_reuses_issue_from_goal_header(self):
        self.fake.issues[220] = {
            "number": 220,
            "title": "Mailbox: accept all commands",
            "state": "open",
        }
        source = SimpleNamespace(
            source_type="task",
            source_id="controller-220",
            title="Nexus controller task",
            content="GitHub Issue #220: Mailbox: accept all commands\n\nDetails.",
        )

        with patch.object(self.service, "_reuse_issue", wraps=self.service._reuse_issue) as reuse, \
             patch.object(self.service, "_create_issue", wraps=self.service._create_issue) as create:
            ref = self.service.ensure_for_source(source)

        self.assertEqual(ref.issue_number, 220)
        reuse.assert_called_once_with(220)
        create.assert_not_called()

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

    def test_issue_closed_by_the_merge_itself_is_not_an_error(self):
        source = SimpleNamespace(
            source_type="issue",
            source_id="42",
            title="GitHub lifecycle",
            content="GitHub Issue #42: GitHub lifecycle",
        )
        ref = self.service.bind_experiment(self.service.ensure_for_source(source), "exp-raced")
        ref = self.service.ensure_pull_request(ref, title="Implement", body="Fixes #42")
        fake = self.fake
        original = fake.__call__

        def racing(method, path, payload=None):
            # "Fixes #42" closed the issue first; GitHub refuses the second close.
            if method == "PATCH" and path.endswith("/issues/42") and (payload or {}).get("state") == "closed":
                fake.issues[42]["state"] = "closed"
                raise GitHubWorkTrackingError(
                    'GitHub HTTP 422: {"message":"This issue was closed by another request."}'
                )
            return original(method, path, payload)

        self.service._request = racing
        self.assertEqual(self.service.merge_and_complete(ref, expected_head_sha="head-sha"), "merged-sha")

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

    def test_issue_progress_comments_once_per_state_with_id_and_pr(self):
        source = SimpleNamespace(
            source_type="issue",
            source_id="42",
            title="GitHub lifecycle",
            content="GitHub Issue #42: GitHub lifecycle",
        )
        ref = self.service.bind_experiment(self.service.ensure_for_source(source), "exp-progress")

        self.assertTrue(self.service.post_progress("exp-progress", "started"))
        self.assertFalse(self.service.post_progress("exp-progress", "started"))
        self.service.ensure_pull_request(ref, title="WIP", body="pending", draft=True)
        self.fake.fail_comments = True
        self.assertFalse(
            self.service.post_progress("exp-progress", "needs_review", detail="Reason: gate unsure")
        )
        self.fake.fail_comments = False
        self.assertTrue(
            self.service.post_progress("exp-progress", "needs_review", detail="Reason: gate unsure")
        )
        self.assertFalse(self.service.post_progress("exp-progress", "needs_review"))
        self.assertFalse(self.service.post_progress("unlinked-exp", "started"))

        self.assertEqual([number for number, _ in self.fake.comments], [42, 42])
        started, review = (body for _, body in self.fake.comments)
        self.assertIn("Nexus experiment exp-progress started", started)
        self.assertIn(ref.branch_name, started)
        self.assertIn("Nexus experiment exp-progress requires review.", review)
        self.assertIn(f"Pull request: #{ref.pr_number}", review)
        self.assertIn("Reason: gate unsure", review)

    def test_finished_pipeline_maps_to_issue_progress_outcome(self):
        from modules.experiment_service.service import _final_issue_progress

        def outcome(status, **fields):
            return _final_issue_progress(
                SimpleNamespace(**{"status": status, "test_output": None, "error": None, **fields})
            )

        self.assertEqual(
            outcome("succeeded", test_output="===== 18 passed in 0.4s ====="),
            (
                ("verification_passed", "Tests: 18 passed in 0.4s."),
                ("completed", None),
            ),
        )
        self.assertEqual(outcome("needs_review", error="gate unsure"), (("needs_review", "Reason: gate unsure"),))
        self.assertEqual(outcome("timed_out", error="watchdog"), (("failed", "Reason: watchdog"),))
        self.assertEqual(outcome("cancelled"), ())

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



_PYTEST_LOG = "\n".join(
    [
        "2026-11-01T08:25:01.1234567Z ##[group]Run python scripts/run_tests_ci.py",
        "2026-11-01T08:25:30.1234567Z tests/test_a.py:12: in test_one",
        "2026-11-01T08:25:31.1234567Z =========== short test summary info ===========",
        "2026-11-01T08:25:31.2234567Z FAILED tests/test_a.py::ATest::test_one - AssertionError: 1 != 2",
        "2026-11-01T08:25:31.3234567Z ERROR: Could not find a version that satisfies pkg",
        "2026-11-01T08:25:32.1234567Z 1 failed, 200 passed in 72.91s",
        "2026-11-01T08:25:32.2234567Z ##[error]Process completed with exit code 1.",
        "2026-11-01T08:25:33.1234567Z Post job cleanup.",
    ]
)


class FailureDetailTests(unittest.TestCase):
    """What a red run says about itself reaches the verdict (#472)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.fake = FakeGitHub()
        self.fake.jobs = [
            {
                "id": 7,
                "name": "lint",
                "conclusion": "success",
                "steps": [{"name": "Lint", "conclusion": "success"}],
            },
            {
                "id": 8,
                "name": "unittest",
                "conclusion": "failure",
                "steps": [
                    {"name": "Checkout", "conclusion": "success"},
                    {"name": "Run tests", "conclusion": "failure"},
                ],
            },
        ]
        self.fake.job_log = _PYTEST_LOG
        self.service = GitHubWorkTrackingService(
            db_path=str(Path(self.temp_dir.name) / "agent.db"),
            repo_root=self.temp_dir.name,
            repository="tire0011/agent-lab",
            transport=self.fake,
        )
        self.addCleanup(self.service.close)

    def _run(self, conclusion="failure"):
        self.fake.workflow_runs = [
            {
                "id": 55,
                "run_attempt": 1,
                "head_sha": "a" * 40,
                "event": "push",
                "status": "completed",
                "conclusion": conclusion,
            }
        ]
        return self.service.wait_for_commit_ci(
            "a" * 40, experiment_id="exp-1", poll_interval_seconds=0.01
        )

    def test_a_failed_run_names_the_step_and_the_tests(self):
        result = self._run()

        self.assertEqual(result.status, "failed")
        self.assertIn("Failed job: unittest", result.failure_detail)
        self.assertIn("Failed step: Run tests", result.failure_detail)
        self.assertIn("FAILED tests/test_a.py::ATest::test_one", result.failure_detail)
        self.assertIn("1 failed, 200 passed", result.failure_detail)
        self.assertEqual(
            [call[1] for call in self.fake.calls[-2:]],
            [
                "/repos/tire0011/agent-lab/actions/runs/55/jobs",
                "/repos/tire0011/agent-lab/actions/jobs/8/logs",
            ],
        )

    def test_the_log_tail_drops_timestamps_and_post_job_cleanup(self):
        detail = self._run().failure_detail

        self.assertNotIn("2026-11-01T", detail)
        self.assertNotIn("Post job cleanup", detail)

    def test_unittest_output_is_recognized_too(self):
        self.fake.job_log = (
            "FAIL: test_two (tests.test_b.BTest.test_two)\n"
            "ERROR: tests.test_c (unittest.loader._FailedTest.tests.test_c)\n"
        )

        self.assertEqual(
            failing_test_names(self._run().failure_detail),
            [
                "test_two (tests.test_b.BTest.test_two)",
                "tests.test_c (unittest.loader._FailedTest.tests.test_c)",
            ],
        )

    def test_a_pytest_collection_error_names_the_broken_file(self):
        self.assertEqual(
            failing_test_names("____ ERROR collecting tests/test_d.py ____\n"),
            ["tests/test_d.py"],
        )

    def test_a_line_that_only_looks_like_a_failure_names_no_test(self):
        self.assertEqual(
            failing_test_names(
                "ERROR: Could not find a version that satisfies pkg\n"
                "ERROR:root:log record\n"
                "ERROR while reading config\n"
            ),
            [],
        )

    def test_a_failed_lookup_keeps_the_verdict(self):
        for broken in ("jobs", "job_log"):
            with self.subTest(broken=broken):
                self.setUp()
                setattr(self.fake, broken, GitHubWorkTrackingError("GitHub HTTP 404"))

                result = self._run()

                self.assertEqual(result.status, "failed")
                self.assertIsNone(result.failure_detail)

    def test_a_malformed_response_keeps_the_verdict(self):
        for jobs in ([], [{"conclusion": "failure"}], ["not a job"]):
            with self.subTest(jobs=jobs):
                self.setUp()
                self.fake.jobs = jobs

                result = self._run()

                self.assertEqual(result.status, "failed")
                self.assertIsNone(result.failure_detail)

    def test_an_oversized_log_is_capped_and_keeps_the_test_names(self):
        filler = "\n".join(f"line {index} " + "x" * 500 for index in range(5000))
        self.fake.job_log = f"FAILED tests/test_a.py::test_one - boom\n{filler}\n"

        detail = self._run().failure_detail

        self.assertLessEqual(len(detail), 8000)
        self.assertIn("tests/test_a.py::test_one", detail)
        self.assertIn("line 4999", detail)

    def test_many_failing_tests_still_fit_the_cap(self):
        self.fake.job_log = "\n".join(
            f"FAILED tests/test_a.py::test_{index}_{'y' * 400}" for index in range(500)
        )

        self.assertLessEqual(len(self._run().failure_detail), 8000)

    def test_a_secret_in_the_log_is_redacted(self):
        token = "ghp_" + "a1b2c3d4" * 5
        self.fake.job_log = (
            f"FAILED tests/test_a.py::test_one - bad {token}\n"
            "api_key=abcdefgh12345678\n"
            "##[error]Process completed with exit code 1.\n"
        )

        detail = self._run().failure_detail

        self.assertNotIn(token, detail)
        self.assertNotIn("abcdefgh12345678", detail)
        self.assertIn("[REDACTED]", detail)

    def test_a_secret_is_not_kept_by_cutting_it_in_half(self):
        token = "ghp_" + "a1b2c3d4" * 5
        self.fake.job_log = f"{token}\n" + "x" * 20000

        self.assertNotIn(token[:12], self._run().failure_detail)

    def test_a_passed_run_makes_no_extra_request(self):
        result = self._run(conclusion="success")

        self.assertEqual(result.status, "passed")
        self.assertIsNone(result.failure_detail)
        self.assertEqual(
            [call for call in self.fake.calls if "/jobs" in call[1]], []
        )


class JobLogRequestTests(unittest.TestCase):
    def _service(self, directory):
        return GitHubWorkTrackingService(
            db_path=str(Path(directory) / "agent.db"),
            repo_root=directory,
            repository="tire0011/agent-lab",
            token="test-token",
        )

    def test_a_job_log_comes_back_as_text(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(directory)
            self.addCleanup(service.close)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b"plain \xff log"

            with patch("modules.github_work_tracking._GITHUB_OPENER.open", return_value=response):
                log = service._request("GET", "/repos/o/r/actions/jobs/8/logs")

        self.assertIn("plain", log)

    def test_the_token_does_not_follow_a_redirect_to_another_host(self):
        request = Request(
            "https://api.github.com/repos/o/r/actions/jobs/8/logs",
            headers={"Authorization": "Bearer test-token"},
        )

        redirected = _CrossHostRedirectDropsToken().redirect_request(
            request, None, 302, "Found", {}, "https://logs.blob.core.windows.net/x?sig=y"
        )

        self.assertFalse(redirected.has_header("Authorization"))

    def test_the_token_follows_a_redirect_inside_github(self):
        request = Request(
            "https://api.github.com/repos/o/r/actions/runs/1/jobs",
            headers={"Authorization": "Bearer test-token"},
        )

        redirected = _CrossHostRedirectDropsToken().redirect_request(
            request, None, 301, "Moved", {}, "https://api.github.com/repositories/9/actions/runs/1/jobs"
        )

        self.assertTrue(redirected.has_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
