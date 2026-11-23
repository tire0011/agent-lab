"""#783: an interrupted run whose paid implementation finished is resumed for free.

A service restart used to mark such a run terminal, so the next try was a new
paid start even though the coding agent had already finished. Now startup parks
the run with ``resume_pending=1`` (when the worktree still holds the finished
work) and the nexus tick resumes it with the existing verifier in the same
worktree, without calling a coding provider.

These tests use a temp git repo and a temp SQLite store, worker factories that
raise if called, the real verifier and the real quality evaluator. The tracked
cases use the real ``GitHubWorkTrackingService`` checkpoint code against a local
bare ``origin``; only the GitHub REST transport is emulated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules import issue_approvals
from modules.experiment_adoption_service import ExperimentAdoptionService
from modules.experiment_service import (
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_SUCCEEDED,
    STATUS_VERIFYING,
    ExperimentAlreadyActiveError,
    ExperimentService,
)
from modules.experiment_service import resume as resume_module
from modules.experiment_service.reverify import parked_candidates, reverify_parked
from modules.experiment_service.resume import (
    RESUME_EVENT,
    RESUME_FINISHED_EVENT,
    RESUME_MESSAGE,
    RESUME_RECOVERED_EVENT,
    RESUME_REF_PREFIX,
    RESUME_SKIPPED_EVENT,
    ResumeStepError,
    owner_alive,
    owner_token,
    resume_interrupted,
    resume_stats,
)
from modules.github_work_tracking import (
    _PROGRESS_MESSAGES,
    GitHubCIVerification,
    GitHubWorkRef,
    GitHubWorkTrackingError,
    GitHubWorkTrackingService,
)
from modules.measurement_health import check_pipelines, health_summary
from modules.nexus_initiative import NexusInitiativeService
from modules.nexus_status_issue import render as render_status

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
ISSUE_BRANCH = "nexus/issue-783"
REPOSITORY = "owner/repo"

FEATURE = "def answer():\n    return 42\n"
FEATURE_TEST = (
    "import unittest\n"
    "from app.feature import answer\n"
    "class FeatureTest(unittest.TestCase):\n"
    "    def test_answer(self):\n"
    "        self.assertEqual(answer(), 42)\n"
)
BROKEN_FEATURE_TEST = FEATURE_TEST.replace("42)", "41)")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True,
    )
    return proc.stdout.strip()


def _raising_factory(kind: str):
    def factory(*_args, **_kwargs):
        raise AssertionError(f"a {kind} was called during a free resume")

    return factory


def _dead_owner_token() -> str:
    """Capture a real owner token, then kill its process."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    token = owner_token(proc.pid)
    proc.kill()
    proc.wait()
    return token


class _FakeGitHub:
    """The few GitHub REST calls the checkpoint and progress code makes."""

    def __init__(self, bare: Path) -> None:
        self.bare = bare
        self.prs: list[dict] = []
        self.pr_posts = 0
        self.comments: list[str] = []
        self.ci_lookups: list[str] = []
        self.pr_updates: list[dict] = []

    def branch_sha(self, branch: str) -> str | None:
        proc = subprocess.run(
            ["git", "--git-dir", str(self.bare), "rev-parse", "--verify", f"refs/heads/{branch}"],
            text=True, capture_output=True,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None

    def __call__(self, method: str, path: str, payload=None):
        if method == "GET" and "/git/ref/heads/" in path:
            branch = urllib.parse.unquote(path.split("/git/ref/heads/", 1)[1])
            sha = self.branch_sha(branch)
            if sha is None:
                raise GitHubWorkTrackingError("HTTP 404: Not Found")
            return {"object": {"sha": sha}}
        if method == "GET" and "/actions/workflows/" in path:
            sha = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["head_sha"][0]
            self.ci_lookups.append(sha)
            return {"workflow_runs": [
                {"id": 500, "head_sha": "old-checkpoint", "event": "push",
                 "status": "completed", "conclusion": "failure"},
                {"id": 499, "head_sha": sha, "event": "pull_request",
                 "status": "completed", "conclusion": "success"},
            ]}
        if method == "GET" and "/pulls?" in path:
            return list(self.prs)
        if method == "POST" and path.endswith("/pulls"):
            self.pr_posts += 1
            pr = {"number": 7}
            self.prs.append(pr)
            return pr
        if method == "PATCH" and "/pulls/" in path:
            self.pr_updates.append(payload)
            return {}
        if method == "POST" and path.endswith("/comments"):
            self.comments.append(str(payload["body"]))
            return {}
        raise AssertionError(f"unexpected GitHub call {method} {path}")


class _Tracking(GitHubWorkTrackingService):
    """Real checkpoint/progress code; CI is unavailable, so tests run locally."""

    def wait_for_ci(self, experiment_id, **_kwargs):
        ref = self.for_experiment(experiment_id)
        return GitHubCIVerification(
            status="unavailable", commit_sha=(ref.checkpoint_sha or "") if ref else "",
            reason="no CI in tests",
        )


class ResumeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.repo = root / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "app").mkdir()
        (self.repo / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "tests" / "test_base.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        pass\n",
            encoding="utf-8",
        )
        (self.repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base_sha = _git(self.repo, "rev-parse", "HEAD")
        self.worktrees = root / "worktrees"
        self.worktrees.mkdir()
        self.db_path = str(root / "agent.db")
        self.service = ExperimentService(
            selection_worker=MagicMock(side_effect=AssertionError("selection worker called")),
            worker_factory=_raising_factory("coding provider"),
            local_worker_factory=_raising_factory("local worker"),
            plan_worker_factory=_raising_factory("plan worker"),
            db_path=self.db_path,
            repo_root=str(self.repo),
            worktrees_root=str(self.worktrees),
            test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"),
        )
        self.addCleanup(self.service.close)
        env = patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "0"})
        env.start()
        self.addCleanup(env.stop)

    # -- fixtures ---------------------------------------------------------------

    def _interrupted_run(
        self,
        *,
        committed: bool = False,
        status: str = STATUS_VERIFYING,
        implementation_ended: bool = True,
        goal: str = "GitHub Issue #783: resume interrupted experiments",
        feature_test: str = FEATURE_TEST,
        extra_files: dict[str, str] | None = None,
        with_change: bool = True,
    ) -> tuple[str, Path]:
        """A run whose coder finished (or not) when the restart hit it."""
        experiment = self.service.create_experiment(goal)
        branch = f"experiment/{experiment.id}"
        worktree = self.worktrees / experiment.id
        _git(self.repo, "worktree", "add", "-q", "-b", branch, str(worktree), self.base_sha)
        if with_change:
            (worktree / "app" / "feature.py").write_text(FEATURE, encoding="utf-8")
            (worktree / "tests" / "test_feature.py").write_text(feature_test, encoding="utf-8")
        for name, text in (extra_files or {}).items():
            path = worktree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if committed:
            _git(worktree, "add", "app/feature.py", "tests/test_feature.py")
            _git(worktree, "commit", "-q", "-m", "implemented change")
        with self.service._conn:
            self.service._conn.execute(
                """
                UPDATE experiments SET status = ?, implementation_ended_at = ?,
                       worktree_path = ?, branch_name = ?, base_commit_sha = ?
                WHERE id = ?
                """,
                (
                    status, NOW.isoformat() if implementation_ended else None,
                    str(worktree), branch, self.base_sha, experiment.id,
                ),
            )
        return experiment.id, worktree

    def _events(self, experiment_id: str, event: str) -> list[dict]:
        return [
            entry for entry in self.service.list_activity(limit=500, experiment_id=experiment_id)
            if entry.get("event") == event
        ]

    def _link_issue(self, experiment_id: str, issue: int = 783) -> None:
        tracking = GitHubWorkTrackingService(
            db_path=self.db_path, repo_root=str(self.repo), repository=REPOSITORY,
            transport=lambda *_a: {},
        )
        self.addCleanup(tracking.close)
        tracking.bind_experiment(
            GitHubWorkRef(issue_number=issue, issue_title="issue", branch_name=ISSUE_BRANCH),
            experiment_id,
        )

    def _initiative(self, **kwargs) -> NexusInitiativeService:
        initiative = NexusInitiativeService(
            db_path=self.db_path, experiment_service=self.service,
            daily_budget=5, reflection_sweep=lambda **_kw: None, **kwargs,
        )
        self.addCleanup(initiative.close)
        return initiative


class MarkInterruptedClassificationTests(ResumeTestBase):
    def test_finished_implementation_with_commit_is_parked_for_resume(self):
        experiment_id, _ = self._interrupted_run(committed=True)

        marked = self.service.mark_interrupted_experiments_failed()

        self.assertEqual(marked, [experiment_id])
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.error, "interrupted by service restart")
        self.assertEqual(experiment.resume_pending, 1)
        self.assertEqual(experiment.resume_count, 0)

    def test_uncommitted_diff_alone_also_parks_the_run(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        self.assertEqual(self.service.get_experiment(experiment_id).resume_pending, 1)

    def test_interrupted_during_implementation_is_not_parked(self):
        experiment_id, _ = self._interrupted_run(
            committed=True, status="implementing", implementation_ended=False,
        )
        self.service.mark_interrupted_experiments_failed()
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.resume_pending, 0)

    def test_missing_worktree_keeps_todays_behaviour(self):
        experiment_id, worktree = self._interrupted_run(committed=True)
        shutil.rmtree(worktree)

        marked = self.service.mark_interrupted_experiments_failed()

        self.assertEqual(marked, [experiment_id])
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.resume_pending, 0)

    def test_clean_worktree_without_commits_is_not_parked(self):
        experiment_id, _ = self._interrupted_run(with_change=False)
        self.service.mark_interrupted_experiments_failed()
        self.assertEqual(self.service.get_experiment(experiment_id).resume_pending, 0)

    def test_startup_leaves_a_live_tick_owned_resume_alone(self):
        # The API restarts while the nexus tick (another live process) verifies.
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        self.assertTrue(resume_module._claim(self.service, experiment_id, owner_token(), NOW.isoformat()))

        self.assertEqual(self.service.mark_interrupted_experiments_failed(), [])

        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_VERIFYING)
        self.assertEqual(experiment.resume_owner, owner_token())

    def test_startup_reparks_an_orphaned_resume_that_never_verified(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        dead = _dead_owner_token()
        self.assertFalse(owner_alive(dead))
        self.assertTrue(resume_module._claim(self.service, experiment_id, dead, NOW.isoformat()))

        self.assertEqual(self.service.mark_interrupted_experiments_failed(), [experiment_id])

        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.resume_pending, 1)
        self.assertEqual(experiment.resume_count, 0)
        self.assertIsNone(experiment.resume_owner)
        self.assertEqual(len(self._events(experiment_id, RESUME_RECOVERED_EVENT)), 1)

    def test_startup_spends_an_orphaned_resume_that_was_verifying(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        dead = _dead_owner_token()
        resume_module._claim(self.service, experiment_id, dead, NOW.isoformat())
        self.assertTrue(resume_module._start_verification(self.service, experiment_id, dead))

        self.assertEqual(self.service.mark_interrupted_experiments_failed(), [experiment_id])

        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.resume_pending, 0)
        self.assertEqual(experiment.resume_count, 1)
        self.assertIn("during verification", experiment.error)
        finished = self._events(experiment_id, RESUME_FINISHED_EVENT)
        self.assertEqual([e["to_status"] for e in finished], [STATUS_INTERRUPTED])
        # Spent: never resumed again.
        self.assertIsNone(resume_interrupted(self.service, NOW))


class ResumeInterruptedTests(ResumeTestBase):
    def test_committed_work_is_verified_as_the_full_delta_without_worker_calls(self):
        experiment_id, worktree = self._interrupted_run(committed=True)
        committed_head = _git(worktree, "rev-parse", "HEAD")
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW)

        self.assertTrue(summary["resumed"])
        self.assertEqual(summary["experiment_id"], experiment_id)
        experiment = self.service.get_experiment(experiment_id)
        # Real verifier and real quality evaluator; the raising worker factories
        # were never touched: zero provider calls.
        self.assertEqual(experiment.status, STATUS_SUCCEEDED, experiment.error)
        self.assertEqual(experiment.test_exit_code, 0)
        self.assertEqual(experiment.quality_evaluation["outcome"], "pass")
        self.assertIn("app/feature.py", experiment.quality_evaluation["changed_files"])
        self.assertIn("app/feature.py", experiment.diff_text)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), committed_head)
        self.assertEqual((worktree / "app" / "feature.py").read_text(), FEATURE)
        self.assertIsNone(summary["resume_commit_sha"])
        self.assertEqual(experiment.resume_snapshot_sha, committed_head)
        self.assertEqual(experiment.resume_count, 1)
        self.assertEqual(experiment.resume_pending, 0)
        self.assertIsNone(experiment.resume_owner)
        started = self._events(experiment_id, RESUME_EVENT)
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["message"], RESUME_MESSAGE)
        self.assertEqual(started[0]["from_status"], STATUS_INTERRUPTED)
        finished = self._events(experiment_id, RESUME_FINISHED_EVENT)
        self.assertEqual([e["to_status"] for e in finished], [STATUS_SUCCEEDED])
        self.assertEqual(RESUME_MESSAGE, "resumed after restart, no new paid start")

    def test_uncommitted_diff_is_committed_without_secrets_and_verified_in_full(self):
        experiment_id, worktree = self._interrupted_run(
            extra_files={".env": "TOKEN=do-not-publish\n", "state/run.json": "{}\n"},
        )
        head_before = _git(worktree, "rev-parse", "HEAD")
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW)

        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_SUCCEEDED, experiment.error)
        self.assertIn("app/feature.py", experiment.quality_evaluation["changed_files"])
        commit = summary["resume_commit_sha"]
        self.assertEqual(_git(worktree, "rev-parse", f"{RESUME_REF_PREFIX}{experiment_id}"), commit)
        files = _git(worktree, "show", "--name-only", "--format=", commit).splitlines()
        self.assertIn("app/feature.py", files)
        self.assertIn("tests/test_feature.py", files)
        self.assertNotIn(".env", files)
        self.assertNotIn("state/run.json", files)
        # HEAD and the worktree files are untouched; nothing was lost.
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), head_before)
        self.assertTrue((worktree / ".env").exists())

    def test_interrupted_during_implementation_is_not_resumed(self):
        experiment_id, _ = self._interrupted_run(
            committed=True, status="implementing", implementation_ended=False,
        )
        self.service.mark_interrupted_experiments_failed()

        with patch.object(self.service, "_verify") as verify:
            self.assertIsNone(resume_interrupted(self.service, NOW))

        verify.assert_not_called()
        self.assertEqual(self.service.get_experiment(experiment_id).status, STATUS_INTERRUPTED)
        self.assertEqual(self._events(experiment_id, RESUME_EVENT), [])

    def test_failed_resume_is_not_resumed_a_second_time_and_then_counts(self):
        experiment_id, _ = self._interrupted_run(feature_test=BROKEN_FEATURE_TEST)
        self.service.mark_interrupted_experiments_failed()
        self._link_issue(experiment_id)
        initiative = self._initiative()
        blocked, _ = initiative._issue_attempts()
        self.assertIn(783, blocked)  # reserved while pending

        first = resume_interrupted(self.service, NOW)
        with patch.object(self.service, "_verify") as verify:
            second = resume_interrupted(self.service, NOW)

        self.assertEqual(first["status"], STATUS_FAILED)
        self.assertIsNone(second)
        verify.assert_not_called()
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_FAILED)
        self.assertEqual(experiment.resume_count, 1)
        # Now the attempt counts: the issue is free for a retry, one attempt used.
        blocked, exhausted = initiative._issue_attempts()
        self.assertNotIn(783, blocked)
        self.assertNotIn(783, exhausted)
        self.assertEqual(
            [e["to_status"] for e in self._events(experiment_id, RESUME_FINISHED_EVENT)],
            [STATUS_FAILED],
        )

    def test_missing_worktree_at_resume_time_falls_back_truthfully(self):
        experiment_id, worktree = self._interrupted_run(committed=True)
        self.service.mark_interrupted_experiments_failed()
        self.assertEqual(self.service.get_experiment(experiment_id).resume_pending, 1)
        shutil.rmtree(worktree)
        tracking = MagicMock()

        with patch.object(self.service, "_verify") as verify:
            summary = resume_interrupted(self.service, NOW, github_work_tracking=tracking)

        verify.assert_not_called()
        tracking.post_progress.assert_not_called()
        self.assertFalse(summary["resumed"])
        self.assertEqual(summary["skipped"][0]["reason"], "worktree missing")
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.resume_pending, 0)
        # No resume is reported for a run that was not resumed.
        self.assertEqual(self._events(experiment_id, RESUME_EVENT), [])
        self.assertEqual(len(self._events(experiment_id, RESUME_SKIPPED_EVENT)), 1)
        self.assertIsNone(resume_interrupted(self.service, NOW))

    def test_at_most_one_resume_per_call(self):
        first_id, _ = self._interrupted_run(goal="GitHub Issue #901: one")
        second_id, _ = self._interrupted_run(goal="GitHub Issue #902: two")
        self.service.mark_interrupted_experiments_failed()

        first = resume_interrupted(self.service, NOW)
        self.assertEqual(self.service.get_experiment(second_id).resume_pending, 1)
        second = resume_interrupted(self.service, NOW)
        third = resume_interrupted(self.service, NOW)

        self.assertEqual(first["experiment_id"], first_id)
        self.assertEqual(second["experiment_id"], second_id)
        self.assertIsNone(third)

    def test_failed_commit_stops_the_resume_with_a_recorded_failure(self):
        experiment_id, worktree = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        real = resume_module._git_or_raise

        def failing(args, *rest, **kwargs):
            if args[0] == "commit-tree":
                raise ResumeStepError("could not commit the resume snapshot: disk full")
            return real(args, *rest, **kwargs)

        with patch.object(resume_module, "_git_or_raise", failing), \
                patch.object(self.service, "_verify") as verify:
            summary = resume_interrupted(self.service, NOW)

        verify.assert_not_called()
        self.assertEqual(summary["status"], STATUS_INTERRUPTED)
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertIn("resume stopped before verification", experiment.error)
        self.assertIn("disk full", experiment.error)
        self.assertEqual(experiment.resume_pending, 0)
        self.assertIsNone(experiment.resume_owner)
        skipped = self._events(experiment_id, RESUME_SKIPPED_EVENT)
        self.assertEqual([e["to_status"] for e in skipped], [STATUS_INTERRUPTED])
        self.assertIn("disk full", skipped[0]["message"])
        self.assertFalse(self._events(experiment_id, RESUME_EVENT))
        # Nothing was committed and the worktree still holds the work.
        self.assertTrue((worktree / "app" / "feature.py").exists())
        self.assertIsNone(resume_interrupted(self.service, NOW))

    def test_staging_failure_is_recorded_without_starting_verification(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        git = resume_module._git_or_raise

        def fail_add(args, *rest, **kwargs):
            if args[0] == "add":
                raise ResumeStepError("could not stage the resume snapshot")
            return git(args, *rest, **kwargs)

        with patch.object(resume_module, "_git_or_raise", fail_add), \
                patch.object(self.service, "_verify") as verify:
            summary = resume_interrupted(self.service, NOW)

        run = self.service.get_experiment(experiment_id)
        self.assertFalse(summary["resumed"])
        self.assertEqual((run.status, run.resume_pending, run.resume_count), (STATUS_INTERRUPTED, 0, 0))
        self.assertIn("could not stage", run.error)
        self.assertFalse(self._events(experiment_id, RESUME_EVENT))
        verify.assert_not_called()

    def test_failed_timing_check_cannot_reverify_a_spent_resume(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        with patch.object(self.service, "_run_timing_check", return_value=(
            STATUS_NEEDS_REVIEW, "timing check failed closed: fixture",
        )):
            summary = resume_interrupted(self.service, NOW)

        self.assertEqual(summary["status"], STATUS_NEEDS_REVIEW)
        self.assertEqual(parked_candidates(self.service, self.base_sha), [])
        self.assertIsNone(reverify_parked(experiment_id, service=self.service, main_sha=self.base_sha))

    def test_revoked_issue_is_not_resumed_and_its_worktree_is_untouched(self):
        experiment_id, worktree = self._interrupted_run(committed=True)
        self.service.mark_interrupted_experiments_failed()
        self._link_issue(experiment_id)
        initiative = self._initiative(repository=REPOSITORY)
        issue_approvals.record_decision(
            initiative._conn, repository=REPOSITORY, issue_number=783,
            status=issue_approvals.STATUS_REVOKED,
        )
        initiative._conn.commit()
        head_before = _git(worktree, "rev-parse", "HEAD")

        with patch.object(self.service, "_verify") as verify:
            summary = resume_interrupted(
                self.service, NOW, approval_block=initiative._resume_approval_block,
            )

        verify.assert_not_called()
        self.assertFalse(summary["resumed"])
        self.assertIn("revoked", summary["skipped"][0]["reason"])
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), head_before)
        missing = subprocess.run(
            ["git", "rev-parse", "--verify", f"{RESUME_REF_PREFIX}{experiment_id}"],
            cwd=worktree, capture_output=True,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(self.service.get_experiment(experiment_id).resume_pending, 0)

    def test_failed_activity_write_rolls_the_claim_back(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        real = resume_module.insert_activity

        def failing(conn, **kwargs):
            if kwargs.get("event") == RESUME_EVENT:
                raise RuntimeError("disk I/O error")
            return real(conn, **kwargs)

        with patch.object(resume_module, "insert_activity", failing):
            with self.assertRaises(RuntimeError):
                resume_interrupted(self.service, NOW)

        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.resume_pending, 1)
        self.assertEqual(experiment.resume_count, 0)
        self.assertIsNone(experiment.resume_owner)
        # The next tick resumes it normally.
        self.assertEqual(resume_interrupted(self.service, NOW)["status"], STATUS_SUCCEEDED)

    def test_process_exit_between_claim_and_verification_is_recovered(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        # A tick claimed it and was killed before verification started.
        self.assertTrue(
            resume_module._claim(self.service, experiment_id, _dead_owner_token(), NOW.isoformat())
        )
        self.assertEqual(self.service.get_experiment(experiment_id).status, STATUS_VERIFYING)

        summary = resume_interrupted(self.service, NOW)

        self.assertEqual(summary["experiment_id"], experiment_id)
        self.assertEqual(summary["status"], STATUS_SUCCEEDED)
        self.assertEqual(len(self._events(experiment_id, RESUME_RECOVERED_EVENT)), 1)
        self.assertEqual(len(self._events(experiment_id, RESUME_EVENT)), 1)
        stats = resume_stats(self.service._conn, datetime.now(timezone.utc))
        self.assertEqual(stats["resumed"], 1)
        self.assertEqual(stats["saved_paid_starts"], 1)

    def test_a_resume_that_keeps_dying_before_verification_stops(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        for _ in range(resume_module._MAX_RESUME_CLAIMS):
            self.assertTrue(resume_module._claim(
                self.service, experiment_id, _dead_owner_token(), NOW.isoformat(),
            ))
            resume_module.recover_orphaned_resumes(self.service, NOW)

        with patch.object(self.service, "_verify") as verify:
            resume_interrupted(self.service, NOW)

        verify.assert_not_called()
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.resume_pending, 0)

    def test_concurrent_claim_runs_only_once(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        self.assertTrue(resume_module._claim(self.service, experiment_id, owner_token(), NOW.isoformat()))

        # A second tick while the first one (alive) still verifies.
        self.assertFalse(resume_module._claim(self.service, experiment_id, owner_token(), NOW.isoformat()))
        with patch.object(self.service, "_verify") as verify:
            self.assertIsNone(resume_interrupted(self.service, NOW))
        verify.assert_not_called()

    def test_logger_failure_releases_claim_for_next_tick(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()

        with patch.object(self.service, "get_experiment_logger", side_effect=OSError("log unavailable")):
            with self.assertRaisesRegex(OSError, "log unavailable"):
                resume_interrupted(self.service, NOW)

        run = self.service.get_experiment(experiment_id)
        self.assertEqual((run.status, run.resume_pending, run.resume_count), (STATUS_INTERRUPTED, 1, 0))
        self.assertIsNone(run.resume_owner)
        self.assertFalse(self._events(experiment_id, RESUME_EVENT))
        self.assertEqual(resume_interrupted(self.service, NOW)["status"], STATUS_SUCCEEDED)

    def test_terminal_write_survives_failed_finish_event(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        insert = resume_module.insert_activity

        def fail_finish(conn, **kwargs):
            if kwargs["event"] == RESUME_FINISHED_EVENT:
                raise OSError("finish event unavailable")
            return insert(conn, **kwargs)

        with patch.object(resume_module, "insert_activity", fail_finish):
            with self.assertRaisesRegex(OSError, "finish event unavailable"):
                resume_interrupted(self.service, NOW)
        run = self.service.get_experiment(experiment_id)
        self.assertEqual(run.status, STATUS_SUCCEEDED)
        self.assertIsNotNone(run.resume_owner)
        self.assertFalse(self._events(experiment_id, RESUME_FINISHED_EVENT))

        # Wait for the finish transaction before offering the run for adoption.
        initiative = self._initiative()
        self.assertEqual(initiative._integration_candidates(), [])
        # A later tick finalizes the existing verdict even in the same process.
        initiative.tick()
        self.assertEqual([c.ref for c in initiative._integration_candidates()], [experiment_id])
        self.assertIsNone(self.service.get_experiment(experiment_id).resume_owner)
        self.assertEqual(resume_stats(self.service._conn, NOW)["saved_paid_starts"], 1)
        self.assertEqual(len(self._events(experiment_id, RESUME_FINISHED_EVENT)), 1)
        self.assertIsNone(resume_interrupted(self.service, NOW))

    def test_failed_verdict_write_preserves_owner_for_reconciliation(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        with patch.object(self.service, "_verify", side_effect=RuntimeError("verification crashed")), \
                patch.object(self.service, "_transition", side_effect=OSError("database unavailable")):
            with self.assertRaisesRegex(OSError, "database unavailable"):
                resume_interrupted(self.service, NOW)

        run = self.service.get_experiment(experiment_id)
        self.assertEqual(run.status, STATUS_VERIFYING)
        self.assertIsNotNone(run.resume_owner)
        self._initiative().tick()
        run = self.service.get_experiment(experiment_id)
        self.assertEqual((run.status, run.resume_pending, run.resume_count), (STATUS_INTERRUPTED, 0, 1))
        self.assertIsNone(run.resume_owner)
        self.assertEqual(len(self._events(experiment_id, RESUME_FINISHED_EVENT)), 1)

    def test_crashed_verifier_goes_to_review(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()

        with patch.object(self.service, "_verify", side_effect=RuntimeError("boom")):
            summary = resume_interrupted(self.service, NOW)

        self.assertEqual(summary["status"], STATUS_NEEDS_REVIEW)
        experiment = self.service.get_experiment(experiment_id)
        self.assertIn("resume verification error", experiment.error)
        self.assertIsNone(experiment.resume_owner)
        self.assertIsNone(resume_interrupted(self.service, NOW))

    def test_passing_resume_is_left_to_the_integration_scheduler(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        adoption = MagicMock()

        resume_interrupted(self.service, NOW)

        adoption.start_adoption.assert_not_called()
        initiative = self._initiative(adoption_service=adoption)
        refs = [c.ref for c in initiative._integration_candidates()]
        self.assertEqual(refs, [experiment_id])


class TrackedResumeTests(ResumeTestBase):
    """The normal GitHub checkpoint, diagnostics and adoption validation."""

    def setUp(self) -> None:
        super().setUp()
        self.bare = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        _git(self.repo, "remote", "add", "origin", str(self.bare))
        _git(self.repo, "push", "-q", "origin", "main", f"main:refs/heads/{ISSUE_BRANCH}")
        _git(self.repo, "fetch", "-q", "origin")
        self.github = _FakeGitHub(self.bare)
        self.tracking = _Tracking(
            db_path=self.db_path, repo_root=str(self.repo), repository=REPOSITORY,
            transport=self.github,
        )
        self.addCleanup(self.tracking.close)
        self.adoption = ExperimentAdoptionService(
            experiment_service=self.service, db_path=self.db_path, repo_root=str(self.repo),
            worktrees_root=str(self.worktrees), lock_path=str(self.root / "adoption.lock"),
            github_work_tracking=self.tracking,
        )
        self.addCleanup(self.adoption.close)

    def _tracked_run(self, **kwargs) -> tuple[str, Path]:
        experiment_id, worktree = self._interrupted_run(**kwargs)
        self.tracking.bind_experiment(
            GitHubWorkRef(issue_number=783, issue_title="Resume", branch_name=ISSUE_BRANCH),
            experiment_id,
        )
        return experiment_id, worktree

    def _assert_adoptable(self, experiment_id: str) -> None:
        validation = self.adoption._validate_startable(experiment_id, persist_review=False)
        self.assertIn("app/feature.py", validation["actual_files"])

    def test_resume_publishes_the_checkpoint_and_passes_adoption_validation(self):
        experiment_id, _ = self._tracked_run(extra_files={"state/run.json": "{}\n"})
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(summary["status"], STATUS_SUCCEEDED, experiment.error)
        checkpoint = self.tracking.for_experiment(experiment_id).checkpoint_sha
        self.assertEqual(self.github.branch_sha(ISSUE_BRANCH), checkpoint)
        published = _git(self.repo, "--git-dir", str(self.bare), "show", "--name-only",
                         "--format=", checkpoint).splitlines()
        self.assertIn("app/feature.py", published)
        self.assertNotIn("state/run.json", published)
        self.assertEqual(self.github.pr_posts, 1)
        self.assertTrue(any(RESUME_MESSAGE in body for body in self.github.comments))
        self.assertEqual(_PROGRESS_MESSAGES[RESUME_EVENT], RESUME_MESSAGE)
        self._assert_adoptable(experiment_id)

    def test_real_exact_checkpoint_ci_and_diagnostics_are_used(self):
        experiment_id, _ = self._tracked_run(committed=True)
        self.service.mark_interrupted_experiments_failed()
        real_wait = GitHubWorkTrackingService.wait_for_ci.__get__(self.tracking)

        with patch.object(self.tracking, "wait_for_ci", real_wait), \
                patch(
                    "modules.experiment_service.service_parts.verification.run_verification_test_monitored",
                    side_effect=AssertionError("authoritative CI should avoid local fallbacks"),
                ):
            summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        run = self.service.get_experiment(experiment_id)
        self.assertEqual(summary["status"], STATUS_SUCCEEDED, run.error)
        checkpoint = self.github.branch_sha(ISSUE_BRANCH)
        self.assertEqual(self.github.ci_lookups, [checkpoint])
        self.assertEqual(run.test_command[-1], checkpoint)
        self.assertTrue(any(
            "Verification status: `succeeded`" in update.get("body", "")
            for update in self.github.pr_updates
        ))
        self._assert_adoptable(experiment_id)

    def test_whitespace_in_committed_delta_is_checked(self):
        experiment_id, worktree = self._tracked_run()
        (worktree / "app" / "feature.py").write_text(FEATURE.replace("42", "42  "))
        _git(worktree, "add", "app/feature.py", "tests/test_feature.py")
        _git(worktree, "commit", "-q", "-m", "commit with trailing whitespace")
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        self.assertEqual(summary["status"], STATUS_NEEDS_REVIEW)
        self.assertIn("trailing whitespace", self.service.get_experiment(experiment_id).error)
        self.assertIsNone(resume_interrupted(self.service, NOW, github_work_tracking=self.tracking))

    def test_committed_excluded_history_does_not_reach_issue_branch(self):
        experiment_id, worktree = self._tracked_run(committed=True)
        (worktree / ".env").write_text("excluded fixture\n")
        _git(worktree, "add", ".env")
        _git(worktree, "commit", "-q", "-m", "accidental excluded file")
        _git(worktree, "rm", ".env")
        _git(worktree, "commit", "-q", "-m", "remove excluded file from the tip")
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        self.assertEqual(summary["status"], STATUS_NEEDS_REVIEW)
        self.assertIn("excluded publication paths", self.service.get_experiment(experiment_id).error)
        self.assertEqual(self.github.branch_sha(ISSUE_BRANCH), self.base_sha)
        self.assertEqual(self.github.pr_posts, 0)

    def test_root_secret_files_never_reach_the_checkpoint(self):
        experiment_id, _ = self._tracked_run(
            extra_files={".env": "TOKEN=do-not-publish\n", ".venv/lib.py": "x = 1\n"},
        )
        self.service.mark_interrupted_experiments_failed()

        resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        checkpoint = self.tracking.for_experiment(experiment_id).checkpoint_sha
        published = _git(self.repo, "--git-dir", str(self.bare), "ls-tree", "-r",
                         "--name-only", checkpoint).splitlines()
        self.assertIn("app/feature.py", published)
        self.assertNotIn(".env", published)
        self.assertNotIn(".venv/lib.py", published)

    def test_remote_tip_change_after_history_check_blocks_publication(self):
        experiment_id, worktree = self._tracked_run(committed=True)
        branch_ref = f"refs/heads/{ISSUE_BRANCH}"
        head = _git(worktree, "rev-parse", "HEAD")
        _git(worktree, "push", "-q", "origin", f"{head}:{branch_ref}")
        (worktree / "app" / "feature.py").write_text(FEATURE + "# finished implementation\n")
        self.service.mark_interrupted_experiments_failed()
        prepare = self.tracking._resume_checkpoint

        def rewind_after_inspection(*args):
            checkpoint = prepare(*args)
            _git(self.repo, "--git-dir", str(self.bare), "update-ref", branch_ref, self.base_sha, head)
            return checkpoint

        with patch.object(self.tracking, "_resume_checkpoint", rewind_after_inspection):
            summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        self.assertEqual(summary["status"], STATUS_NEEDS_REVIEW)
        self.assertEqual(self.github.branch_sha(ISSUE_BRANCH), self.base_sha)
        self.assertEqual(self.github.pr_posts, 0)

    def test_checkpoint_published_before_the_restart_is_reused(self):
        experiment_id, worktree = self._tracked_run()
        before = self.tracking.checkpoint_experiment(experiment_id, str(worktree)).checkpoint_sha
        self.assertEqual(self.github.pr_posts, 1)
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        self.assertEqual(summary["status"], STATUS_SUCCEEDED,
                         self.service.get_experiment(experiment_id).error)
        # Same checkpoint, same PR: no duplicate.
        self.assertEqual(self.tracking.for_experiment(experiment_id).checkpoint_sha, before)
        self.assertEqual(self.github.branch_sha(ISSUE_BRANCH), before)
        self.assertEqual(self.github.pr_posts, 1)
        self._assert_adoptable(experiment_id)

    def test_committed_work_is_checkpointed_and_adoptable(self):
        experiment_id, _ = self._tracked_run(committed=True)
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(summary["status"], STATUS_SUCCEEDED, experiment.error)
        self.assertIn("app/feature.py", experiment.quality_evaluation["changed_files"])
        checkpoint = self.tracking.for_experiment(experiment_id).checkpoint_sha
        self.assertEqual(self.github.branch_sha(ISSUE_BRANCH), checkpoint)
        self._assert_adoptable(experiment_id)

    def test_staged_only_changes_reach_real_verification_checkpoint_and_adoption(self):
        experiment_id, worktree = self._tracked_run()
        _git(worktree, "add", "app/feature.py", "tests/test_feature.py")
        (worktree / "app" / "feature.py").write_text("def answer(): return -1\n")
        (worktree / "tests" / "test_feature.py").unlink()
        index_path = Path(_git(worktree, "rev-parse", "--git-path", "index"))
        index_before = index_path.read_bytes()
        head_before = _git(worktree, "rev-parse", "HEAD")
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        run = self.service.get_experiment(experiment_id)
        self.assertEqual(summary["status"], STATUS_SUCCEEDED, run.error)
        checkpoint = self.github.branch_sha(ISSUE_BRANCH)
        self.assertEqual(_git(self.repo, "show", f"{checkpoint}:app/feature.py"), FEATURE.strip())
        self.assertEqual(_git(self.repo, "show", f"{checkpoint}:tests/test_feature.py"), FEATURE_TEST.strip())
        self.assertEqual(index_path.read_bytes(), index_before)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), head_before)
        self.assertFalse((worktree / "tests" / "test_feature.py").exists())
        self.assertIn("return -1", (worktree / "app" / "feature.py").read_text())
        self._assert_adoptable(experiment_id)

    def test_committed_path_matching_ignore_rule_reaches_verification_and_adoption(self):
        experiment_id, worktree = self._tracked_run()
        (worktree / ".gitignore").write_text("__pycache__/\napp/feature.py\n")
        _git(worktree, "add", ".gitignore", "tests/test_feature.py")
        _git(worktree, "add", "-f", "app/feature.py")
        _git(worktree, "commit", "-q", "-m", "tracked feature and ignore rule")
        head = _git(worktree, "rev-parse", "HEAD")
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        run = self.service.get_experiment(experiment_id)
        self.assertEqual(summary["status"], STATUS_SUCCEEDED, run.error)
        self.assertEqual(self.github.branch_sha(ISSUE_BRANCH), head)
        self.assertIn("app/feature.py", run.quality_evaluation["changed_files"])
        self._assert_adoptable(experiment_id)

    def test_literal_filenames_in_snapshot_and_normal_checkpoint(self):
        experiment_id, worktree = self._tracked_run(extra_files={"*": "literal wildcard\n", ".env": "fixture\n"})
        checkpoint = self.tracking.checkpoint_experiment(experiment_id, str(worktree)).checkpoint_sha
        self.assertIn("*", _git(self.repo, "ls-tree", "-r", "--name-only", checkpoint).splitlines())
        self.assertNotIn(".env", _git(self.repo, "ls-tree", "-r", "--name-only", checkpoint).splitlines())
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        run = self.service.get_experiment(experiment_id)
        self.assertEqual(summary["status"], STATUS_SUCCEEDED, run.error)
        files = _git(self.repo, "ls-tree", "-r", "--name-only", summary["resume_commit_sha"]).splitlines()
        self.assertIn("*", files)
        self.assertNotIn(".env", files)
        self.assertEqual(self.github.pr_posts, 1)

    def test_replacing_a_file_with_a_directory_does_not_stage_its_secret(self):
        experiment_id, worktree = self._tracked_run()
        replaced = worktree / "tests" / "test_base.py"
        replaced.unlink()
        replaced.mkdir()
        (replaced / "allowed.txt").write_text("allowed fixture\n")
        (replaced / ".env").write_text("excluded fixture\n")
        original = self.tracking.checkpoint_experiment(experiment_id, str(worktree)).checkpoint_sha
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        run = self.service.get_experiment(experiment_id)
        self.assertEqual(summary["status"], STATUS_SUCCEEDED, run.error)
        for sha in (original, summary["resume_commit_sha"]):
            files = _git(self.repo, "ls-tree", "-r", "--name-only", sha).splitlines()
            self.assertIn("tests/test_base.py/allowed.txt", files)
            self.assertNotIn("tests/test_base.py/.env", files)

    def test_retry_preserves_merge_ancestry_and_uses_post_merge_baseline(self):
        experiment_id, worktree = self._tracked_run(goal="GitHub Issue #783: tests only")
        (worktree / "app" / "feature.py").unlink()
        (worktree / "tests" / "test_feature.py").write_text("def test_retry(): pass\n")
        (worktree / "issue.txt").write_text("previous issue work\n")
        _git(worktree, "add", "issue.txt")
        _git(worktree, "commit", "-q", "-m", "earlier issue attempt")
        issue_sha = _git(worktree, "rev-parse", "HEAD")
        _git(worktree, "push", "-q", "origin", f"{issue_sha}:refs/heads/{ISSUE_BRANCH}")
        (self.repo / "app" / "upstream.py").write_text("upstream = True\n")
        _git(self.repo, "add", "app/upstream.py")
        _git(self.repo, "commit", "-q", "-m", "unrelated upstream change")
        main_sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "push", "-q", "origin", "main")
        # Construct the actual two-parent topology without altering main.
        _git(worktree, "checkout", main_sha, "--", "app/upstream.py")
        merged = _git(
            worktree, "-c", "user.email=nexus@agent-lab.local", "commit-tree",
            _git(worktree, "write-tree"), "-p", issue_sha, "-p", main_sha,
            "-m", "Merge branch 'main' of origin",
        )
        _git(worktree, "reset", "--soft", merged)
        with self.service._conn:
            self.service._conn.execute(
                "UPDATE experiments SET base_commit_sha = ?, plan_json = ? WHERE id = ?",
                (issue_sha, '{"change_kind":"tests_only"}', experiment_id),
            )
        self.service.mark_interrupted_experiments_failed()

        summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        run = self.service.get_experiment(experiment_id)
        self.assertEqual(summary["status"], STATUS_SUCCEEDED, run.error)
        self.assertEqual(run.implementation_base_sha, merged)
        self.assertEqual(run.quality_evaluation["changed_files"], ["tests/test_feature.py"])
        checkpoint = self.github.branch_sha(ISSUE_BRANCH)
        self.assertEqual(_git(worktree, "rev-parse", f"{checkpoint}^"), merged)
        self.assertEqual(_git(worktree, "rev-parse", f"{merged}^2"), main_sha)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), merged)
        validation = self.adoption._validate_startable(experiment_id, persist_review=False)
        self.assertEqual(validation["actual_files"], ["tests/test_feature.py"])
        self.assertEqual(validation["committed_commit_sha"], checkpoint)

    def test_missing_after_claim_has_no_resume_comment_or_event(self):
        experiment_id, worktree = self._tracked_run()
        self.service.mark_interrupted_experiments_failed()
        claim = resume_module._claim

        def claim_then_remove(*args):
            claimed = claim(*args)
            shutil.rmtree(worktree)
            return claimed

        with patch.object(resume_module, "_claim", claim_then_remove):
            summary = resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        self.assertFalse(summary["resumed"])
        self.assertEqual(summary["status"], STATUS_INTERRUPTED)
        self.assertFalse(self._events(experiment_id, RESUME_EVENT))
        self.assertFalse(self.github.comments)

    def test_resume_comment_is_posted_once_even_when_recovered(self):
        experiment_id, _ = self._tracked_run()
        self.service.mark_interrupted_experiments_failed()
        resume_module._claim(self.service, experiment_id, _dead_owner_token(), NOW.isoformat())
        self.tracking.post_progress(experiment_id, RESUME_EVENT)

        resume_interrupted(self.service, NOW, github_work_tracking=self.tracking)

        self.assertEqual(sum(RESUME_MESSAGE in body for body in self.github.comments), 1)


class PublicationExclusionTests(unittest.TestCase):
    def test_root_dotfiles_are_matched_by_the_exclusions(self):
        # lstrip("./") used to turn ".env" into "env", so root secrets passed.
        publishable = GitHubWorkTrackingService._publishable_path
        for path in (".env", "./.env", ".env.local", ".venv/lib.py", "state/x.json", "id.pem"):
            self.assertFalse(publishable(path), path)
        for path in ("app/x.py", "./app/x.py", ".github/workflows/tests.yml", ".gitignore"):
            self.assertTrue(publishable(path), path)


class PendingResumeAccountingTests(ResumeTestBase):
    def test_pending_resume_reserves_the_issue_without_counting_as_an_attempt(self):
        ids = []
        for index in range(3):  # more than _MAX_ISSUE_ATTEMPTS once spent
            experiment_id, _ = self._interrupted_run(goal=f"GitHub Issue #783 part {index}")
            ids.append(experiment_id)
        self.service.mark_interrupted_experiments_failed()
        for experiment_id in ids:
            self.assertEqual(self.service.get_experiment(experiment_id).resume_pending, 1)
            self._link_issue(experiment_id)
        initiative = self._initiative()

        blocked, exhausted = initiative._issue_attempts()

        # Reserved against a replacement start, but no attempt is used up.
        self.assertIn(783, blocked)
        self.assertNotIn(783, exhausted)

        # Once the resumes are spent, the interrupted runs count again.
        with self.service._conn:
            self.service._conn.execute("UPDATE experiments SET resume_pending = 0")
        blocked, exhausted = initiative._issue_attempts()
        self.assertIn(783, exhausted)
        self.assertEqual(sorted(exhausted[783]["experiment_ids"]), sorted(ids))

    def test_one_failed_resume_keeps_the_issue_reserved_for_the_other_pending_one(self):
        failing_id, _ = self._interrupted_run(
            goal="GitHub Issue #783 first", feature_test=BROKEN_FEATURE_TEST,
        )
        waiting_id, _ = self._interrupted_run(goal="GitHub Issue #783 second")
        self.service.mark_interrupted_experiments_failed()
        self._link_issue(failing_id)
        self._link_issue(waiting_id)

        self.assertEqual(resume_interrupted(self.service, NOW)["status"], STATUS_FAILED)

        blocked, _ = self._initiative()._issue_attempts()
        self.assertIn(783, blocked)
        self.assertEqual(self.service.get_experiment(waiting_id).resume_pending, 1)

    def test_create_experiment_does_not_replace_a_pending_resume(self):
        experiment_id, worktree = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()

        with self.assertRaises(ExperimentAlreadyActiveError) as raised:
            self.service.create_experiment("GitHub Issue #783: resume interrupted experiments")
        self.assertEqual(raised.exception.experiment_id, experiment_id)

        shutil.rmtree(worktree)
        resume_interrupted(self.service, NOW)  # falls back: no longer pending
        self.service.create_experiment("GitHub Issue #783: resume interrupted experiments")

    def test_retention_cleanup_keeps_a_pending_worktree(self):
        experiment_id, worktree = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        later = datetime.now(timezone.utc) + timedelta(days=30)

        self.assertEqual(self.service.cleanup_expired_worktrees(1, now=later).removed, [])
        self.assertTrue(worktree.is_dir())

        with self.service._conn:
            self.service._conn.execute(
                "UPDATE experiments SET resume_pending = 0 WHERE id = ?", (experiment_id,),
            )
        self.assertEqual(self.service.cleanup_expired_worktrees(1, now=later).removed, [experiment_id])


class InitiativeGuardTests(ResumeTestBase):
    def test_next_tick_recovers_killed_pre_verification_owner_before_stall_and_busy(self):
        experiment_id, _ = self._interrupted_run()
        self.service.mark_interrupted_experiments_failed()
        old = datetime.now(timezone.utc) - timedelta(days=1)
        token = f"{_dead_owner_token()}/{'a' * 32}"
        scratch = self.root / "interrupted-scratch"
        scratch.mkdir()
        _git(self.repo, "worktree", "add", "--detach", str(scratch / "worktree"), self.base_sha)
        self.assertTrue(resume_module._claim(self.service, experiment_id, token, old.isoformat()))
        with self.service._conn:
            self.service._conn.execute(
                "UPDATE activity_log SET timestamp = ? WHERE entity_id = ?",
                (old.isoformat(), experiment_id),
            )
        initiative = self._initiative(
            interrupted_resumer=lambda **kw: resume_interrupted(self.service, NOW, **kw),
        )

        scratch_path = resume_module._scratch_path
        with patch.object(resume_module, "_scratch_path", side_effect=lambda t: scratch if t == token else scratch_path(t)):
            initiative.tick()

        self.assertFalse(scratch.exists())
        self.assertNotIn(str(scratch), _git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(self.service.get_experiment(experiment_id).status, STATUS_SUCCEEDED)
        self.assertEqual(len(self._events(experiment_id, RESUME_RECOVERED_EVENT)), 1)
        self.assertEqual(len(self._events(experiment_id, RESUME_EVENT)), 1)

    def test_tick_runs_the_resume_behind_the_guards(self):
        resumer = MagicMock(return_value=None)
        launcher = MagicMock()
        initiative = self._initiative(
            interrupted_resumer=resumer, reverification_launcher=launcher,
        )

        # Paused by the owner: no resume.
        with initiative._conn:
            initiative._conn.execute(
                "INSERT INTO service_settings (key, value, updated_at) VALUES ('nexus.paused', '1', ?)",
                (NOW.isoformat(),),
            )
        initiative.tick()
        resumer.assert_not_called()

        # Busy: no resume.
        with initiative._conn:
            initiative._conn.execute("DELETE FROM service_settings WHERE key = 'nexus.paused'")
        running = self.service.create_experiment("busy experiment")
        with self.service._conn:
            self.service._conn.execute(
                "UPDATE experiments SET status = 'verifying', ended_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), running.id),
            )
        self.assertEqual(initiative.tick().decision, "busy")
        resumer.assert_not_called()

        # Free: exactly one call, with the approval check, before planning.
        with self.service._conn:
            self.service._conn.execute("UPDATE experiments SET status = 'failed' WHERE id = ?", (running.id,))
        initiative.tick()
        resumer.assert_called_once()
        self.assertEqual(
            resumer.call_args.kwargs["approval_block"], initiative._resume_approval_block,
        )
        launcher.assert_called_once()

    def test_a_tick_that_resumed_defers_the_parked_reverification(self):
        launcher = MagicMock()
        initiative = self._initiative(
            interrupted_resumer=MagicMock(return_value={"resumed": True, "status": "succeeded"}),
            reverification_launcher=launcher,
        )
        initiative.tick()
        launcher.assert_not_called()

    def test_a_broken_resumer_does_not_break_the_tick(self):
        initiative = self._initiative(interrupted_resumer=MagicMock(side_effect=RuntimeError("x")))
        self.assertNotEqual(initiative.tick().decision, "error")


class ResumeMeasurementTests(ResumeTestBase):
    def test_status_and_measurement_health_count_resumes_and_saved_starts(self):
        passing_id, _ = self._interrupted_run(goal="GitHub Issue #901: pass")
        failing_id, _ = self._interrupted_run(
            goal="GitHub Issue #902: fail", feature_test=BROKEN_FEATURE_TEST,
        )
        self.service.mark_interrupted_experiments_failed()
        resume_interrupted(self.service, NOW)
        resume_interrupted(self.service, NOW)
        # An old resume outside the 14-day window does not count.
        with self.service._conn:
            self.service._conn.execute(
                "INSERT INTO activity_log (timestamp, entity_type, entity_id, event, message) "
                "VALUES (?, 'experiment', 'old', ?, ?)",
                ((datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
                 RESUME_EVENT, RESUME_MESSAGE),
            )
        now = datetime.now(timezone.utc)

        stats = resume_stats(self.service._conn, now)
        self.assertEqual(stats["resumed"], 2)
        self.assertEqual(stats["saved_paid_starts"], 1)
        self.assertEqual(stats["outcomes"], {STATUS_FAILED: 1, STATUS_SUCCEEDED: 1})

        health = check_pipelines(self.service._conn, now)["interrupted_resumes"]
        self.assertEqual(health["status"], "ok")
        self.assertNotIn("count", health)  # never read as a stuck streak
        self.assertEqual(health["saved_paid_starts"], 1)
        summary = health_summary("interrupted_resumes", health)
        self.assertIn("2 interrupted run(s) resumed in the last 14 days", summary)
        self.assertIn("1 passed verification (saved paid starts)", summary)

        body = render_status(self.service._conn, now=now, budget=1, spent=0, paused=False, busy=None)
        self.assertIn("**Free resumes:** 2 interrupted run(s) resumed", body)
        self.assertIn(passing_id[:8], body)
        self.assertIn(failing_id[:8], body)


if __name__ == "__main__":
    unittest.main()
