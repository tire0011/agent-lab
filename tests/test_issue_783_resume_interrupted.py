"""#783: an interrupted run whose paid implementation finished is resumed for free.

A service restart used to mark such a run terminal, so the next try was a new
paid start even though the coding agent had already finished. Now startup parks
the run with ``resume_pending=1`` (when the worktree still holds the finished
work) and the nexus tick resumes it: the uncommitted diff is committed and the
existing ``_verify`` plus the adoption path run in the same worktree, without
calling a coding provider.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.experiment_evaluation import EvaluationResult
from modules.experiment_service import (
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_SUCCEEDED,
    STATUS_VERIFYING,
    ExperimentService,
)
from modules.experiment_service.resume import (
    RESUME_EVENT,
    RESUME_MESSAGE,
    resume_interrupted,
)
from modules.github_work_tracking import _PROGRESS_MESSAGES
from modules.nexus_initiative import NexusInitiativeService

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True,
    )
    return proc.stdout.strip()


def _raising_worker_factory():
    def factory(_worktree_path):
        raise AssertionError("a coding provider was called during a free resume")

    return factory


class ResumeInterruptedTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "test_base.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        pass\n",
            encoding="utf-8",
        )
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base_sha = _git(self.repo, "rev-parse", "HEAD")
        self.worktrees = root / "worktrees"
        self.worktrees.mkdir()
        self.service = ExperimentService(
            selection_worker=MagicMock(),
            worker_factory=_raising_worker_factory(),
            local_worker_factory=lambda _path: (_ for _ in ()).throw(
                AssertionError("local worker called during a free resume")
            ),
            plan_worker_factory=lambda _path: (_ for _ in ()).throw(
                AssertionError("plan worker called during a free resume")
            ),
            db_path=str(root / "agent.db"),
            repo_root=str(self.repo),
            worktrees_root=str(self.worktrees),
            test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"),
        )
        self.addCleanup(self.service.close)
        # Keep the deterministic quality gate out of the way of the resume tests.
        quality = patch(
            "modules.experiment_service.evaluate_implementation",
            return_value=EvaluationResult(outcome="pass", reason="ok"),
        )
        quality.start()
        self.addCleanup(quality.stop)

    def _make_worktree(self, branch: str, *, commit_change: bool) -> Path:
        worktree = self.worktrees / branch
        _git(self.repo, "worktree", "add", "-q", "-b", branch, str(worktree), self.base_sha)
        if commit_change:
            (worktree / "feature.txt").write_text("implemented\n", encoding="utf-8")
            _git(worktree, "add", "feature.txt")
            _git(worktree, "commit", "-q", "-m", "implemented change")
        return worktree

    def _add_experiment(
        self,
        *,
        status: str = STATUS_VERIFYING,
        implementation_ended: bool = True,
        worktree: Path | None = None,
        branch: str = "exp-branch",
        base: str | None = None,
        goal: str = "GitHub Issue #783: resume interrupted experiments",
    ) -> str:
        experiment = self.service.create_experiment(goal)
        with self.service._conn:
            self.service._conn.execute(
                """
                UPDATE experiments SET status = ?, implementation_ended_at = ?,
                       worktree_path = ?, branch_name = ?, base_commit_sha = ?
                WHERE id = ?
                """,
                (
                    status,
                    NOW.isoformat() if implementation_ended else None,
                    str(worktree) if worktree is not None else None,
                    branch,
                    base if base is not None else self.base_sha,
                    experiment.id,
                ),
            )
        return experiment.id

    def _fake_verify(self, status: str):
        calls: list[tuple] = []

        def verify(experiment_id, worktree_path, elog=None, **kwargs):
            calls.append((experiment_id, worktree_path, kwargs))
            self.service._force_status(
                experiment_id, status, ended_at=NOW.isoformat(), error=None,
            )
            return self.service.get_experiment(experiment_id)

        return verify, calls


class MarkInterruptedClassificationTests(ResumeInterruptedTestBase):
    def test_finished_implementation_with_commit_is_parked_for_resume(self):
        worktree = self._make_worktree("exp-branch", commit_change=True)
        experiment_id = self._add_experiment(worktree=worktree)

        marked = self.service.mark_interrupted_experiments_failed()

        self.assertEqual(marked, [experiment_id])
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.error, "interrupted by service restart")
        self.assertEqual(experiment.resume_pending, 1)
        self.assertEqual(experiment.resume_count, 0)

    def test_uncommitted_diff_alone_also_parks_the_run(self):
        worktree = self._make_worktree("exp-branch", commit_change=False)
        (worktree / "feature.txt").write_text("uncommitted\n", encoding="utf-8")
        experiment_id = self._add_experiment(worktree=worktree)

        self.service.mark_interrupted_experiments_failed()

        self.assertEqual(self.service.get_experiment(experiment_id).resume_pending, 1)

    def test_interrupted_during_implementation_is_not_parked(self):
        worktree = self._make_worktree("exp-branch", commit_change=True)
        experiment_id = self._add_experiment(
            status="implementing", implementation_ended=False, worktree=worktree,
        )

        self.service.mark_interrupted_experiments_failed()

        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.resume_pending, 0)

    def test_missing_worktree_keeps_todays_behaviour(self):
        experiment_id = self._add_experiment(
            worktree=self.worktrees / "gone", branch="gone-branch",
        )

        marked = self.service.mark_interrupted_experiments_failed()

        self.assertEqual(marked, [experiment_id])
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.resume_pending, 0)

    def test_clean_worktree_without_commits_is_not_parked(self):
        worktree = self._make_worktree("exp-branch", commit_change=False)
        experiment_id = self._add_experiment(worktree=worktree)

        self.service.mark_interrupted_experiments_failed()

        self.assertEqual(self.service.get_experiment(experiment_id).resume_pending, 0)


class ResumeInterruptedTests(ResumeInterruptedTestBase):
    def test_finished_implementation_is_resumed_and_verified_without_worker_calls(self):
        worktree = self._make_worktree("exp-branch", commit_change=True)
        experiment_id = self._add_experiment(worktree=worktree)
        self.service.mark_interrupted_experiments_failed()

        with patch.dict("os.environ", {"NEXUS_TIMING_CHECK": "0"}):
            summary = resume_interrupted(self.service, NOW)

        self.assertIsNotNone(summary)
        self.assertEqual(summary["experiment_id"], experiment_id)
        experiment = self.service.get_experiment(experiment_id)
        # The real verifier ran the suite in the same worktree and passed.
        self.assertEqual(experiment.status, STATUS_SUCCEEDED, experiment.error)
        self.assertEqual(experiment.test_exit_code, 0)
        self.assertEqual(experiment.resume_count, 1)
        self.assertEqual(experiment.resume_pending, 0)
        # The raising worker factories were never touched: zero provider calls.
        events = [
            entry for entry in self.service.list_activity(experiment_id=experiment_id)
            if entry.get("event") == RESUME_EVENT
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], RESUME_MESSAGE)
        self.assertEqual(events[0]["from_status"], STATUS_INTERRUPTED)
        self.assertEqual(RESUME_MESSAGE, "resumed after restart, no new paid start")

    def test_resume_commits_the_uncommitted_diff_before_verifying(self):
        worktree = self._make_worktree("exp-branch", commit_change=False)
        (worktree / "feature.txt").write_text("uncommitted\n", encoding="utf-8")
        experiment_id = self._add_experiment(worktree=worktree)
        self.service.mark_interrupted_experiments_failed()
        head_before = _git(worktree, "rev-parse", "HEAD")

        verify, calls = self._fake_verify(STATUS_SUCCEEDED)
        with patch.object(self.service, "_verify", verify):
            summary = resume_interrupted(self.service, NOW)

        self.assertEqual(summary["status"], STATUS_SUCCEEDED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], experiment_id)
        self.assertEqual(calls[0][1], str(worktree))
        # The uncommitted diff was committed, not lost.
        self.assertEqual(_git(worktree, "status", "--porcelain"), "")
        self.assertNotEqual(_git(worktree, "rev-parse", "HEAD"), head_before)
        self.assertIn("feature.txt", _git(worktree, "show", "--name-only", "--format=", "HEAD"))

    def test_interrupted_during_implementation_is_not_resumed(self):
        worktree = self._make_worktree("exp-branch", commit_change=True)
        experiment_id = self._add_experiment(
            status="implementing", implementation_ended=False, worktree=worktree,
        )
        self.service.mark_interrupted_experiments_failed()

        verify, calls = self._fake_verify(STATUS_SUCCEEDED)
        with patch.object(self.service, "_verify", verify):
            summary = resume_interrupted(self.service, NOW)

        self.assertIsNone(summary)
        self.assertEqual(calls, [])
        self.assertEqual(self.service.get_experiment(experiment_id).status, STATUS_INTERRUPTED)

    def test_failed_resume_is_not_resumed_a_second_time(self):
        worktree = self._make_worktree("exp-branch", commit_change=True)
        experiment_id = self._add_experiment(worktree=worktree)
        self.service.mark_interrupted_experiments_failed()

        verify, calls = self._fake_verify(STATUS_FAILED)
        with patch.object(self.service, "_verify", verify):
            first = resume_interrupted(self.service, NOW)
            second = resume_interrupted(self.service, NOW)

        self.assertEqual(first["status"], STATUS_FAILED)
        self.assertIsNone(second)
        self.assertEqual(len(calls), 1)
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_FAILED)
        self.assertEqual(experiment.resume_count, 1)
        self.assertEqual(experiment.resume_pending, 0)

    def test_missing_worktree_falls_back_and_is_not_resumed(self):
        experiment_id = self._add_experiment(
            worktree=self.worktrees / "gone", branch="gone-branch",
        )
        self.service.mark_interrupted_experiments_failed()

        verify, calls = self._fake_verify(STATUS_SUCCEEDED)
        with patch.object(self.service, "_verify", verify):
            summary = resume_interrupted(self.service, NOW)

        self.assertIsNone(summary)
        self.assertEqual(calls, [])
        experiment = self.service.get_experiment(experiment_id)
        self.assertEqual(experiment.status, STATUS_INTERRUPTED)
        self.assertEqual(experiment.resume_pending, 0)

    def test_at_most_one_resume_per_call(self):
        first_worktree = self._make_worktree("branch-one", commit_change=True)
        second_worktree = self._make_worktree("branch-two", commit_change=True)
        first_id = self._add_experiment(worktree=first_worktree, branch="branch-one", goal="one")
        second_id = self._add_experiment(worktree=second_worktree, branch="branch-two", goal="two")
        self.service.mark_interrupted_experiments_failed()

        verify, calls = self._fake_verify(STATUS_SUCCEEDED)
        with patch.object(self.service, "_verify", verify):
            first = resume_interrupted(self.service, NOW)
            second = resume_interrupted(self.service, NOW)
            third = resume_interrupted(self.service, NOW)

        self.assertEqual(first["experiment_id"], first_id)
        self.assertEqual(second["experiment_id"], second_id)
        self.assertIsNone(third)
        self.assertEqual(len(calls), 2)

    def test_passing_resume_queues_the_normal_adoption_and_posts_the_comment(self):
        worktree = self._make_worktree("exp-branch", commit_change=True)
        experiment_id = self._add_experiment(worktree=worktree)
        self.service.mark_interrupted_experiments_failed()

        adoption = SimpleNamespace(start_adoption=MagicMock())
        tracking = SimpleNamespace(post_progress=MagicMock(return_value=True))
        wrapper = SimpleNamespace(
            experiment_service=self.service, adoption_service=adoption,
            github_work_tracking=tracking,
        )
        verify, calls = self._fake_verify(STATUS_SUCCEEDED)
        with patch.object(self.service, "_verify", verify):
            summary = resume_interrupted(wrapper, NOW)

        self.assertEqual(summary["status"], STATUS_SUCCEEDED)
        self.assertEqual(summary.get("adoption"), "queued")
        adoption.start_adoption.assert_called_once_with(experiment_id)
        tracking.post_progress.assert_called_once_with(experiment_id, RESUME_EVENT)
        self.assertEqual(
            _PROGRESS_MESSAGES[RESUME_EVENT], "resumed after restart, no new paid start",
        )
        # A second tick must not resume (or adopt) the same run again.
        with patch.object(self.service, "_verify", verify):
            self.assertIsNone(resume_interrupted(wrapper, NOW))
        adoption.start_adoption.assert_called_once_with(experiment_id)

    def test_failing_resume_does_not_queue_adoption(self):
        worktree = self._make_worktree("exp-branch", commit_change=True)
        experiment_id = self._add_experiment(worktree=worktree)
        self.service.mark_interrupted_experiments_failed()

        adoption = SimpleNamespace(start_adoption=MagicMock())
        wrapper = SimpleNamespace(experiment_service=self.service, adoption_service=adoption)
        verify, _calls = self._fake_verify(STATUS_FAILED)
        with patch.object(self.service, "_verify", verify):
            summary = resume_interrupted(wrapper, NOW)

        self.assertEqual(summary["status"], STATUS_FAILED)
        adoption.start_adoption.assert_not_called()


class PendingResumeAttemptAccountingTests(ResumeInterruptedTestBase):
    def _link_issue(self, experiment_id: str, issue: int = 783) -> None:
        with self.service._conn:
            self.service._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS github_experiment_links (
                    experiment_id TEXT PRIMARY KEY,
                    issue_number INTEGER NOT NULL,
                    issue_title TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    pr_number INTEGER,
                    checkpoint_sha TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.service._conn.execute(
                """
                INSERT INTO github_experiment_links
                    (experiment_id, issue_number, issue_title, branch_name,
                     created_at, updated_at)
                VALUES (?, ?, 'issue', 'branch', ?, ?)
                """,
                (experiment_id, issue, NOW.isoformat(), NOW.isoformat()),
            )

    def test_pending_resume_does_not_count_as_a_used_attempt(self):
        ids = []
        for index in range(3):  # over _MAX_ISSUE_ATTEMPTS once the resumes are spent
            worktree = self._make_worktree(f"branch-{index}", commit_change=True)
            ids.append(self._add_experiment(
                worktree=worktree, branch=f"branch-{index}", goal=f"GitHub Issue #783 part {index}",
            ))
        self.service.mark_interrupted_experiments_failed()
        for experiment_id in ids:
            self.assertEqual(self.service.get_experiment(experiment_id).resume_pending, 1)
            self._link_issue(experiment_id)

        initiative = NexusInitiativeService(
            db_path=self.service.db_path, experiment_service=self.service,
            daily_budget=5, reflection_sweep=lambda **kwargs: None,
        )
        self.addCleanup(initiative.close)
        blocked, exhausted = initiative._issue_attempts()

        # While the resumes are still pending, none of them is a used attempt.
        self.assertNotIn(783, blocked)
        self.assertNotIn(783, exhausted)

        # Once the resumes are spent (resume_pending cleared), the interrupted
        # runs count again and exhaust the issue's attempts.
        with self.service._conn:
            self.service._conn.execute("UPDATE experiments SET resume_pending = 0")
        blocked, exhausted = initiative._issue_attempts()
        self.assertIn(783, exhausted)
        self.assertEqual(sorted(exhausted[783]["experiment_ids"]), sorted(ids))


if __name__ == "__main__":
    unittest.main()
