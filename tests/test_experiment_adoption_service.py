from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from modules.experiment_adoption_service import (
    STATUS_ADOPTED,
    STATUS_ADOPTING,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_STARTED,
    STATUS_QUEUED,
    ExperimentAdoptionError,
    ExperimentAdoptionService,
)
from modules.experiment_service import ExperimentService
from modules.github_work_tracking import GitHubWorkTrackingError, GitHubWorkTrackingService


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


class GitHubLifecycleTransport:
    def __init__(self, *, origin: Path, workspace: Path, merge_should_fail: str | None = None):
        self.origin = origin
        self.workspace = workspace
        self.merge_should_fail = merge_should_fail
        self.issues: dict[int, dict[str, object]] = {}
        self.next_issue = 42
        self.refs = {"main": "main-sha"}
        self.pulls: list[dict[str, object]] = []
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))

        if method == "GET" and path.startswith("/search/issues?"):
            query = parse_qs(urlsplit(path).query).get("q", [""])[0]
            wanted = ""
            if '"' in query:
                wanted = query.split('"', 1)[1].rsplit('"', 1)[0]
            items = [
                dict(issue)
                for issue in self.issues.values()
                if issue.get("state") == "open" and issue.get("title") == wanted
            ]
            return {"items": items}

        if method == "POST" and path == "/repos/tire0011/agent-lab/issues":
            number = self.next_issue
            self.next_issue += 1
            issue = {"number": number, "title": payload["title"], "state": "open"}
            self.issues[number] = issue
            return dict(issue)

        if method == "GET" and path.startswith("/repos/tire0011/agent-lab/issues/"):
            number = int(path.rsplit("/", 1)[1])
            return dict(self.issues[number])

        if method == "PATCH" and path.startswith("/repos/tire0011/agent-lab/issues/"):
            number = int(path.rsplit("/", 1)[1])
            self.issues[number].update(payload or {})
            return dict(self.issues[number])

        if method == "GET" and "/git/ref/heads/" in path:
            branch = unquote(path.split("/git/ref/heads/", 1)[1])
            if branch not in self.refs:
                raise GitHubWorkTrackingError("GitHub HTTP 404: not found")
            return {"object": {"sha": self.refs[branch]}}

        if method == "POST" and path == "/repos/tire0011/agent-lab/git/refs":
            branch = payload["ref"].removeprefix("refs/heads/")
            self.refs[branch] = payload["sha"]
            return {"ref": payload["ref"], "object": {"sha": payload["sha"]}}

        if method == "GET" and path.startswith("/repos/tire0011/agent-lab/pulls?"):
            params = parse_qs(urlsplit(path).query)
            head = params.get("head", [None])[0]
            base = params.get("base", [None])[0]
            return [
                dict(pr)
                for pr in self.pulls
                if pr.get("state") == "open"
                and (head is None or f"tire0011:{pr['head']}" == head)
                and (base is None or pr.get("base") == base)
            ]

        if method == "POST" and path == "/repos/tire0011/agent-lab/pulls":
            pr = {
                "number": len(self.pulls) + 100,
                "state": "open",
                "head": payload["head"],
                "base": payload["base"],
                "title": payload["title"],
                "body": payload["body"],
                "draft": payload.get("draft", False),
            }
            self.pulls.append(pr)
            return dict(pr)

        if method == "GET" and "/pulls/" in path and "?" not in path:
            number = int(path.rsplit("/", 1)[1])
            pr = next((item for item in self.pulls if item["number"] == number), None)
            if pr is None:
                raise AssertionError(f"unknown PR {number}")
            return {**pr, "node_id": f"PR_{number}"}

        if method == "POST" and path == "/graphql":
            node_id = (payload or {}).get("variables", {}).get("id")
            number = int(str(node_id).removeprefix("PR_"))
            pr = next((item for item in self.pulls if item["number"] == number), None)
            if pr is None:
                raise AssertionError(f"unknown PR node {node_id}")
            pr["draft"] = False
            return {"data": {"markPullRequestReadyForReview": {"pullRequest": {"number": number, "isDraft": False}}}}

        if method == "PUT" and path.endswith("/merge"):
            number = int(path.split("/pulls/", 1)[1].split("/", 1)[0])
            pr = next((item for item in self.pulls if item["number"] == number), None)
            if pr is None:
                raise AssertionError(f"unknown PR {number}")
            if self.merge_should_fail:
                return {"merged": False, "message": self.merge_should_fail}
            sha = self._merge_branch(str(pr["head"]))
            pr["state"] = "closed"
            # Mirror what GitHub reports afterwards, so code that asks
            # "was this already merged?" sees the same evidence it would
            # see in production.
            pr["merged"] = True
            pr["merge_commit_sha"] = sha
            return {"merged": True, "sha": sha}

        raise AssertionError(f"unexpected GitHub call: {method} {path} {payload}")

    def _merge_branch(self, branch_name: str) -> str:
        if self.workspace.exists():
            git(self.workspace, "fetch", "origin")
        else:
            git(self.workspace.parent, "clone", str(self.origin), str(self.workspace))
            git(self.workspace, "config", "user.email", "test@example.com")
            git(self.workspace, "config", "user.name", "Test User")
        git(self.workspace, "fetch", "origin", branch_name, "main")
        git(self.workspace, "checkout", "-B", "main", "origin/main")
        git(self.workspace, "reset", "--hard", "origin/main")
        git(self.workspace, "clean", "-fd")
        merge_proc = git(self.workspace, "merge", "--squash", f"origin/{branch_name}", check=False)
        if merge_proc.returncode != 0:
            raise GitHubWorkTrackingError(
                merge_proc.stderr.strip() or merge_proc.stdout.strip() or "merge failed"
            )
        status = git(self.workspace, "status", "--porcelain").stdout.strip()
        if status:
            git(self.workspace, "commit", "-m", f"Merge {branch_name}")
            git(self.workspace, "push", "origin", "HEAD:main")
        sha = git(self.workspace, "rev-parse", "HEAD").stdout.strip()
        self.refs["main"] = sha
        return sha


class AdoptionRepoFixture:
    def __init__(self, *, github_configured: bool = True, merge_should_fail: str | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.origin = root / "origin.git"
        self.worktrees = root / "worktrees"
        self.logs = root / "logs"
        self.db_path = str(root / "agent.db")
        self.lock_path = str(root / "adoption.lock")
        self.repo.mkdir()
        self.worktrees.mkdir()
        self.logs.mkdir()
        git(self.repo.parent, "init", "--bare", str(self.origin))
        git(self.repo.parent, "init", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_app.py").write_text(
            "import unittest\n\n"
            "class AppTests(unittest.TestCase):\n"
            "    def test_true(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.repo / "communication" / "outbox").mkdir(parents=True)
        (self.repo / "communication" / "outbox" / ".gitkeep").write_text("", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "init")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-u", "origin", "main")

        self.experiment_service = ExperimentService(
            selection_worker=MagicMock(),
            worker_factory=lambda _: MagicMock(),
            plan_worker_factory=lambda _: MagicMock(),
            db_path=self.db_path,
            repo_root=str(self.repo),
            worktrees_root=str(self.worktrees),
            test_python_executable=sys.executable,
            experiment_log_dir=str(self.logs),
        )
        self.github_api = GitHubLifecycleTransport(
            origin=self.origin,
            workspace=root / "github-merge-workspace",
            merge_should_fail=merge_should_fail,
        )
        self.github_api.refs["main"] = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.github_seed_tracking = GitHubWorkTrackingService(
            db_path=self.db_path,
            repo_root=str(self.repo),
            repository="tire0011/agent-lab",
            transport=self.github_api,
        )
        if github_configured:
            self.github_work_tracking = self.github_seed_tracking
        else:
            # "Unconfigured" has to mean unconfigured no matter what the
            # ambient environment holds: the service falls back to
            # GITHUB_TOKEN/GH_TOKEN, and a token in the test runner's
            # environment would silently make this branch configured.
            with patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}):
                self.github_work_tracking = GitHubWorkTrackingService(
                    db_path=self.db_path,
                    repo_root=str(self.repo),
                    repository="tire0011/agent-lab",
                )
        self.adoption_service = ExperimentAdoptionService(
            experiment_service=self.experiment_service,
            db_path=self.db_path,
            repo_root=str(self.repo),
            worktrees_root=str(self.worktrees),
            lock_path=self.lock_path,
            github_work_tracking=self.github_work_tracking,
        )

    def close(self) -> None:
        self.adoption_service.close()
        if self.github_work_tracking is not self.github_seed_tracking:
            self.github_work_tracking.close()
        self.github_seed_tracking.close()
        self.experiment_service.close()
        self.tmp.cleanup()

    def create_experiment(
        self,
        *,
        experiment_id: str,
        changed_files: list[str],
        status: str = "succeeded",
        quality_outcome: str = "pass",
        mutate: dict[str, str] | None = None,
        extra_untracked: dict[str, str] | None = None,
        link_github: bool = True,
    ) -> str:
        worktree = self.worktrees / f"exp-{experiment_id[:8]}"
        git(self.repo, "worktree", "add", "-b", f"exp-{experiment_id[:8]}", str(worktree), "HEAD")
        for rel_path, content in (mutate or {"app.py": f"VALUE = '{experiment_id[:8]}'\n"}).items():
            target = worktree / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for rel_path, content in (extra_untracked or {}).items():
            target = worktree / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        snapshot = self.adoption_service._capture_snapshot(
            str(worktree),
            self.experiment_service.get_experiment_logger(experiment_id),
            phase="test",
        )
        plan = {
            "title": "Adoptable change",
            "affected_files": changed_files,
            "change_kind": "implementation",
            "required_artifacts": changed_files,
            "acceptance_criteria": ["tests pass"],
        }
        quality = {
            "outcome": quality_outcome,
            "reason": "ok" if quality_outcome == "pass" else "no",
            "checks": [],
            "expected_files": changed_files,
            "changed_files": changed_files,
            "missing_artifacts": [],
        }
        with self.experiment_service._lock, self.experiment_service._conn:
            self.experiment_service._conn.execute(
                """
                INSERT INTO experiments (
                    id, goal, status, created_at, ended_at, plan_json, worktree_path,
                    branch_name, test_command_json, test_exit_code, git_status,
                    diff_stat, diff_text, quality_evaluation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    "Implement something small.",
                    status,
                    "2026-08-14T12:00:00Z",
                    "2026-08-14T12:10:00Z",
                    json.dumps(plan),
                    str(worktree),
                    f"exp-{experiment_id[:8]}",
                    json.dumps([sys.executable, "-m", "unittest", "discover", "-s", "tests"]),
                    0,
                    snapshot["git_status"],
                    snapshot["diff_stat"],
                    snapshot["diff_text"],
                    json.dumps(quality),
                ),
            )
        if link_github:
            ref = self.github_seed_tracking.ensure_for_source(
                SimpleNamespace(
                    source_type="task",
                    source_id=experiment_id,
                    title=f"Adoptable change {experiment_id[:8]}",
                    content="Implement something small.",
                )
            )
            self.github_seed_tracking.bind_experiment(ref, experiment_id)
        return experiment_id

    def wait_terminal(self, experiment_id: str, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.adoption_service.wait_for_thread(experiment_id, timeout=0.1)
            adoption = self.adoption_service.get(experiment_id)
            if adoption.status not in {STATUS_QUEUED, STATUS_ADOPTING}:
                return adoption
            time.sleep(0.05)
        raise AssertionError(f"adoption did not finish for {experiment_id}")



def _gate_the_verification_spawn():
    """Hold the local verification run open until the test lets it go.

    The verification test command is spawned with ``subprocess.Popen`` by the
    monitored runner (#209), so that - not ``subprocess.run`` - is where an
    adoption can be caught mid-verification. Git's own calls go through the
    same patched attribute and must pass straight through.
    """
    gate = threading.Event()
    ready = threading.Event()
    original_popen = subprocess.Popen

    def blocking_spawn(args, **kwargs):
        if isinstance(args, list) and args[:3] == [sys.executable, "-m", "unittest"]:
            ready.set()
            gate.wait(5)
        return original_popen(args, **kwargs)

    return gate, ready, blocking_spawn


class ExperimentAdoptionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = AdoptionRepoFixture()
        self.addCleanup(self.fx.close)

    def test_adopts_valid_successful_experiment(self):
        experiment_id = "a" * 32
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py", "feature.py"],
            extra_untracked={"feature.py": "FEATURE = True\n"},
        )

        recorded: list[list[str]] = []
        original_run_git = __import__("modules.experiment_adoption_service", fromlist=["_run_git"])._run_git

        def spy(args, **kwargs):
            recorded.append(list(args))
            return original_run_git(args, **kwargs)

        with patch("modules.experiment_adoption_service._run_git", side_effect=spy):
            started = self.fx.adoption_service.start_adoption(experiment_id)
            self.assertIn(started.status, {STATUS_QUEUED, STATUS_ADOPTING})
            adoption = self.fx.wait_terminal(experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.test_exit_code, 0)
        self.assertEqual(adoption.actual_files, ["app.py", "feature.py"])
        remote_files = git(self.fx.repo, "show", "origin/main:feature.py").stdout
        self.assertIn("FEATURE = True", remote_files)
        branch_sha = git(
            self.fx.repo,
            f"--git-dir={self.fx.origin}",
            "rev-parse",
            "refs/heads/nexus/42-adoptable-change-aaaaaaaa",
        ).stdout.strip()
        self.assertTrue(branch_sha)
        self.assertFalse((Path(adoption.diagnostic_worktree_path) if adoption.diagnostic_worktree_path else Path(self.fx.worktrees / "missing")).exists())

    def test_background_thread_runs_after_start_returns(self):
        experiment_id = "b" * 32
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        gate, ready, blocking_spawn = _gate_the_verification_spawn()

        with patch("modules.verification_runner.subprocess.Popen", side_effect=blocking_spawn):
            started = self.fx.adoption_service.start_adoption(experiment_id)
            self.assertIn(started.status, {STATUS_QUEUED, STATUS_ADOPTING})
            self.assertTrue(ready.wait(5))
            self.assertTrue(self.fx.adoption_service._threads[experiment_id].is_alive())
            gate.set()
            adoption = self.fx.wait_terminal(experiment_id)
        self.assertEqual(adoption.status, STATUS_ADOPTED)

    def test_idempotent_double_adopt_does_not_create_second_push(self):
        experiment_id = "c" * 32
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        first = self.fx.adoption_service.start_adoption(experiment_id)
        self.assertIn(first.status, {STATUS_QUEUED, STATUS_ADOPTING})
        adopted = self.fx.wait_terminal(experiment_id)
        before = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()
        second = self.fx.adoption_service.start_adoption(experiment_id)
        after = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()
        self.assertEqual(second.status, STATUS_ADOPTED)
        self.assertEqual(adopted.integrated_commit_sha, second.integrated_commit_sha)
        self.assertEqual(before, after)

    def test_rejects_when_quality_gate_failed(self):
        experiment_id = "d" * 32
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"], quality_outcome="needs_review")
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service.start_adoption(experiment_id)
        self.assertEqual(self.fx.adoption_service.get(experiment_id).status, STATUS_NOT_STARTED)

    def test_rejects_failed_needs_review_and_running_experiment_statuses(self):
        for prefix, status in (("e", "failed"), ("f", "needs_review"), ("1", "implementing")):
            experiment_id = prefix * 32
            self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"], status=status)
            with self.assertRaises(ExperimentAdoptionError):
                self.fx.adoption_service.start_adoption(experiment_id)
            self.assertEqual(self.fx.adoption_service.get(experiment_id).status, STATUS_NOT_STARTED)

    def test_rejects_missing_worktree(self):
        experiment_id = "7" * 32
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        worktree = self.fx.experiment_service.get_experiment(experiment_id).worktree_path
        shutil.rmtree(worktree)
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service.start_adoption(experiment_id)

    def test_rejects_changed_worktree_since_verification(self):
        experiment_id = "8" * 32
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        worktree = Path(self.fx.experiment_service.get_experiment(experiment_id).worktree_path)
        (worktree / "later.py").write_text("x = 1\n", encoding="utf-8")
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service.start_adoption(experiment_id)

    def test_outbox_file_never_staged_and_marked_for_review(self):
        experiment_id = "9" * 32
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py", "communication/outbox/blocked.md"],
            extra_untracked={"communication/outbox/blocked.md": "forbidden\n"},
        )
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service.start_adoption(experiment_id)
        adoption = self.fx.adoption_service.get(experiment_id)
        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertIn("communication/outbox", adoption.reason)
        log = git(self.fx.repo, "log", "--oneline", "origin/main").stdout
        self.assertNotIn("blocked", log)

    def test_unexpected_files_require_review(self):
        experiment_id = "a1" * 16
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py"],
            extra_untracked={"unexpected.py": "SURPRISE = True\n"},
        )
        with self.assertRaises(ExperimentAdoptionError):
            self.fx.adoption_service.start_adoption(experiment_id)
        self.assertEqual(self.fx.adoption_service.get(experiment_id).status, STATUS_NEEDS_REVIEW)

    def test_retry_adopts_the_earlier_attempts_commits_too(self):
        # #455: a retry builds on the earlier attempt's commit; adopting only
        # the retry's own commit lost that work (the reverted #410).
        experiment_id = "e7" * 16
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py"],
            mutate={"app.py": "VALUE = 'retry'\n"},
        )
        worktree = self.fx.worktrees / f"exp-{experiment_id[:8]}"
        git(worktree, "stash")
        (worktree / "first_attempt.py").write_text("FIRST = True\n", encoding="utf-8")
        git(worktree, "add", "first_attempt.py")
        git(worktree, "commit", "-m", "earlier attempt")
        git(worktree, "stash", "pop")

        self.fx.adoption_service.start_adoption(experiment_id)
        adoption = self.fx.wait_terminal(experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED, adoption.reason)
        self.assertIn("FIRST = True", git(self.fx.repo, "show", "origin/main:first_attempt.py").stdout)
        self.assertIn("retry", git(self.fx.repo, "show", "origin/main:app.py").stdout)

    def test_cherry_pick_conflict_keeps_diagnostic_worktree_and_skips_push(self):
        experiment_id = "b1" * 16
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["app.py"],
            mutate={"app.py": "VALUE = 'experiment'\n"},
        )
        (self.fx.repo / "app.py").write_text("VALUE = 'main-change'\n", encoding="utf-8")
        git(self.fx.repo, "commit", "-am", "main change")
        git(self.fx.repo, "push", "origin", "main")

        self.fx.adoption_service.start_adoption(experiment_id)
        adoption = self.fx.wait_terminal(experiment_id)
        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertIn("Integrationskonflikt: app.py", adoption.reason)
        self.assertTrue(Path(adoption.diagnostic_worktree_path).exists())

    def test_integration_test_failure_prevents_push(self):
        experiment_id = "c1" * 16
        self.fx.create_experiment(
            experiment_id=experiment_id,
            changed_files=["tests/test_app.py"],
            mutate={
                "tests/test_app.py": (
                    "import unittest\n\n"
                    "class BrokenTests(unittest.TestCase):\n"
                    "    def test_fail(self):\n"
                    "        self.assertEqual(1, 2)\n"
                )
            },
        )
        before = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()
        self.fx.adoption_service.start_adoption(experiment_id)
        adoption = self.fx.wait_terminal(experiment_id)
        after = git(self.fx.repo, "rev-parse", "origin/main").stdout.strip()
        self.assertEqual(adoption.status, STATUS_FAILED)
        self.assertEqual(before, after)
        self.assertTrue(Path(adoption.diagnostic_worktree_path).exists())

    def test_integrated_quality_evaluation_failure_prevents_push(self):
        experiment_id = "d1" * 16
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "UPDATE experiments SET plan_json = ? WHERE id = ?",
                (
                    json.dumps({
                        "title": "Mismatch plan",
                        "affected_files": ["planned.py"],
                        "change_kind": "implementation",
                        "required_artifacts": ["planned.py"],
                        "acceptance_criteria": ["tests pass"],
                    }),
                    experiment_id,
                ),
            )
        self.fx.adoption_service.start_adoption(experiment_id)
        adoption = self.fx.wait_terminal(experiment_id)
        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertIn("Qualitäts-Gate", adoption.reason)

    def test_missing_github_configuration_keeps_work_for_review_without_pushing_main(self):
        fx = AdoptionRepoFixture(github_configured=False)
        self.addCleanup(fx.close)
        experiment_id = "e1" * 16
        fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        before = git(fx.repo, "rev-parse", "origin/main").stdout.strip()

        fx.adoption_service.start_adoption(experiment_id)
        adoption = fx.wait_terminal(experiment_id)
        after = git(fx.repo, "rev-parse", "origin/main").stdout.strip()

        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertEqual(before, after)
        self.assertIn("GitHub lifecycle blocked before adoption", adoption.reason)
        experiment = fx.experiment_service.get_experiment(experiment_id)
        self.assertTrue(Path(experiment.worktree_path).exists())

    def test_github_merge_failure_sets_needs_review(self):
        fx = AdoptionRepoFixture(merge_should_fail="simulated merge failure")
        self.addCleanup(fx.close)
        experiment_id = "f1" * 16
        fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])

        fx.adoption_service.start_adoption(experiment_id)
        adoption = fx.wait_terminal(experiment_id)

        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertIn("GitHub PR konnte nicht automatisch abgeschlossen werden", adoption.reason)
        self.assertIn("simulated merge failure", adoption.reason)

    # ------------------------------------------------------------------
    # Restart recovery (Issue #125)
    #
    # A process restart is a normal, expected boundary - the refresh flow
    # restarts Nexus deliberately - so an adoption caught mid-flight must
    # be reconstructed from durable evidence, not declared interrupted.
    # ------------------------------------------------------------------

    def _restarted_service(self) -> ExperimentAdoptionService:
        """A second service instance, standing in for the restarted process."""
        restarted = ExperimentAdoptionService(
            experiment_service=self.fx.experiment_service,
            db_path=self.fx.db_path,
            repo_root=str(self.fx.repo),
            worktrees_root=str(self.fx.worktrees),
            lock_path=self.fx.lock_path,
            github_work_tracking=self.fx.github_work_tracking,
        )
        self.addCleanup(restarted.close)
        return restarted

    def _force_status(self, experiment_id: str, status: str, **columns) -> None:
        """Rewrite one adoption row, simulating what a crash left behind."""
        assignments = ", ".join(f"{name} = ?" for name in columns)
        clause = f", {assignments}" if assignments else ""
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                f"UPDATE experiment_adoptions SET status = ?{clause} WHERE experiment_id = ?",
                [status, *columns.values(), experiment_id],
            )

    def _merge_calls(self) -> list[tuple]:
        return [
            call for call in self.fx.github_api.calls
            if call[0] == "PUT" and call[1].endswith("/merge")
        ]

    def _wait_terminal_on(self, service, experiment_id: str, timeout: float = 20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            service.wait_for_thread(experiment_id, timeout=0.1)
            adoption = service.get(experiment_id)
            if adoption.status not in {STATUS_QUEUED, STATUS_ADOPTING}:
                return adoption
            time.sleep(0.05)
        raise AssertionError(f"recovered adoption did not finish for {experiment_id}")

    def test_restart_before_merge_resumes_and_merges_exactly_once(self):
        experiment_id = "d1" * 16
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        # Adoption had started but nothing was committed or merged yet.
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, created_at, updated_at, target_branch
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("row-resume", experiment_id, STATUS_ADOPTING,
                 "2026-09-06T12:00:00Z", "2026-09-06T12:00:00Z", "main"),
            )

        restarted = self._restarted_service()
        self.assertEqual(restarted.recover_after_restart(), [experiment_id])
        adoption = self._wait_terminal_on(restarted, experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(len(self._merge_calls()), 1)
        # The same adoption record was continued, not replaced.
        self.assertEqual(adoption.id, "row-resume")
        self.assertEqual(adoption.recovery_attempts, 1)

    def test_restart_after_the_commit_resumes_without_recommitting(self):
        """The worktree is already clean at this point, so the fresh-start
        path would fail ("changes have altered since verification"). Resume
        must continue from the recorded commit instead."""
        experiment_id = "d2" * 16
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        experiment = self.fx.experiment_service.get_experiment(experiment_id)
        worktree = str(experiment.worktree_path)
        git(Path(worktree), "add", "--", "app.py")
        git(Path(worktree), "commit", "--no-verify", "-m", "Nexus adoption commit")
        committed_sha = git(Path(worktree), "rev-parse", "HEAD").stdout.strip()

        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, created_at, updated_at, target_branch,
                    experiment_commit_sha, actual_files_json, expected_files_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("row-committed", experiment_id, STATUS_ADOPTING,
                 "2026-09-06T12:00:00Z", "2026-09-06T12:00:00Z", "main",
                 committed_sha, json.dumps(["app.py"]), json.dumps(["app.py"])),
            )

        restarted = self._restarted_service()
        restarted.recover_after_restart()
        adoption = self._wait_terminal_on(restarted, experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(adoption.experiment_commit_sha, committed_sha)
        self.assertEqual(len(self._merge_calls()), 1)

    def test_restart_after_merge_is_detected_and_never_merges_twice(self):
        """The merge landed but the process died before persisting it."""
        experiment_id = "d3" * 16
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        self.fx.adoption_service.start_adoption(experiment_id)
        adopted = self.fx.wait_terminal(experiment_id)
        self.assertEqual(adopted.status, STATUS_ADOPTED)
        merges_before = len(self._merge_calls())
        self.assertEqual(merges_before, 1)

        # Rewind the bookkeeping only: GitHub still shows the PR merged.
        self._force_status(experiment_id, STATUS_ADOPTING, integrated_commit_sha=None, ended_at=None)

        restarted = self._restarted_service()
        restarted.recover_after_restart()
        adoption = restarted.get(experiment_id)

        self.assertEqual(adoption.status, STATUS_ADOPTED)
        self.assertEqual(len(self._merge_calls()), merges_before)
        self.assertIn("already merged", adoption.reason or "")
        self.assertTrue(adoption.integrated_commit_sha)

    def test_repeated_recovery_of_a_merged_adoption_is_idempotent(self):
        experiment_id = "d4" * 16
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        self.fx.adoption_service.start_adoption(experiment_id)
        self.fx.wait_terminal(experiment_id)
        merges_before = len(self._merge_calls())

        for _ in range(3):
            self._force_status(experiment_id, STATUS_ADOPTING)
            restarted = self._restarted_service()
            restarted.recover_after_restart()
            self.assertEqual(restarted.get(experiment_id).status, STATUS_ADOPTED)

        self.assertEqual(len(self._merge_calls()), merges_before)

    def test_exhausted_recovery_attempts_become_needs_review(self):
        experiment_id = "d5" * 16
        self.fx.create_experiment(experiment_id=experiment_id, changed_files=["app.py"])
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, created_at, updated_at, target_branch,
                    recovery_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("row-exhausted", experiment_id, STATUS_ADOPTING,
                 "2026-09-06T12:00:00Z", "2026-09-06T12:00:00Z", "main", 3),
            )

        restarted = self._restarted_service()
        restarted.recover_after_restart()
        adoption = restarted.get(experiment_id)

        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertIn("recovery attempts", adoption.reason or "")
        self.assertEqual(self._merge_calls(), [])

    def test_only_one_interrupted_adoption_is_resumed_at_a_time(self):
        """Adoptions are serialized by design, so two stuck records are an
        inconsistency: resuming both would only make them fight over the
        adoption lock and fail each other."""
        first_id = "e1" * 16
        second_id = "e2" * 16
        self.fx.create_experiment(experiment_id=first_id, changed_files=["app.py"])
        self.fx.create_experiment(experiment_id=second_id, changed_files=["app.py"])
        with sqlite3.connect(self.fx.db_path) as conn:
            for row_id, experiment_id, created in (
                ("row-a", first_id, "2026-09-06T12:00:00Z"),
                ("row-b", second_id, "2026-09-06T12:05:00Z"),
            ):
                conn.execute(
                    """
                    INSERT INTO experiment_adoptions (
                        id, experiment_id, status, created_at, updated_at, target_branch
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (row_id, experiment_id, STATUS_ADOPTING, created, created, "main"),
                )

        restarted = self._restarted_service()
        restarted.recover_after_restart()
        self._wait_terminal_on(restarted, first_id)

        # The older record is resumed and completes; the newer one is
        # explicitly handed to a human instead of racing it.
        self.assertEqual(restarted.get(first_id).status, STATUS_ADOPTED)
        second = restarted.get(second_id)
        self.assertEqual(second.status, STATUS_NEEDS_REVIEW)
        self.assertIn("concurrent adoptions", second.reason or "")

    def test_unrecoverable_state_becomes_needs_review_not_interrupted(self):
        """A restart alone is never a failure, but a vanished experiment is
        genuinely unrecoverable and must say so."""
        experiment_id = "91" * 16  # no experiment record exists for this id
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, created_at, updated_at, target_branch
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("row1", experiment_id, STATUS_ADOPTING,
                 "2026-08-14T12:00:00Z", "2026-08-14T12:00:00Z", "main"),
            )

        restarted = self._restarted_service()
        handled = restarted.recover_after_restart()
        adoption = restarted.get(experiment_id)

        self.assertEqual(handled, [experiment_id])
        self.assertEqual(adoption.status, STATUS_NEEDS_REVIEW)
        self.assertNotEqual(adoption.status, STATUS_INTERRUPTED)
        self.assertIn("no longer available", adoption.reason or "")

    def test_parallel_adoption_is_prevented(self):
        first_id = "01" * 16
        second_id = "02" * 16
        self.fx.create_experiment(experiment_id=first_id, changed_files=["app.py"])
        self.fx.create_experiment(experiment_id=second_id, changed_files=["app.py"])
        gate, ready, blocking_spawn = _gate_the_verification_spawn()

        with patch("modules.verification_runner.subprocess.Popen", side_effect=blocking_spawn):
            self.fx.adoption_service.start_adoption(first_id)
            self.assertTrue(ready.wait(5))
            with self.assertRaises(ExperimentAdoptionError):
                self.fx.adoption_service.start_adoption(second_id)
            gate.set()
            adoption = self.fx.wait_terminal(first_id)
        self.assertEqual(adoption.status, STATUS_ADOPTED)


if __name__ == "__main__":
    unittest.main()
