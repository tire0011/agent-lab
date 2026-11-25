"""#821: diff size of each verified experiment next to its review verdict.

Covers numstat parsing (binary files), the additive migration, the bucket
shares, the planner split guard and the authenticated route. Temp git repos
and temp databases only; no network.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules import issue_approvals
from modules import review_size_stats as rss
from modules.experiment_service import ExperimentService
from modules.experiment_service.models import STATUS_FAILED, STATUS_VERIFYING
from modules.experiment_service.service_parts.verification import VerificationMixin
from modules.nexus_mcp_tools import READ_ONLY_TOOLS
from modules.nexus_planner import IssuePlanner, planning_prompt
from modules.run_store import init_db
from modules.verification_runner import HungTest
from tests.test_experiment_service import (
    _init_git_repo,
    _make_result,
    _write_affected_files_side_effect,
)
from tests.test_nexus_initiative import TEST_REPOSITORY, InitiativeFixture

REVIEW_BLOCK = "Review by codex blocked the merge: edge case"


def git(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
    ).stdout


class NumstatParsingTests(unittest.TestCase):
    def test_binary_files_count_as_a_file_with_zero_lines(self):
        size = rss.parse_numstat("3\t1\ta.py\n-\t-\timage.png\n10\t0\tdocs/b.md\n")
        self.assertEqual((size.files, size.added, size.removed), (3, 13, 1))
        self.assertEqual(size.lines, 14)

    def test_empty_and_malformed_lines_are_ignored(self):
        self.assertEqual(rss.parse_numstat(""), rss.DiffSize(0, 0, 0))
        self.assertEqual(rss.parse_numstat("\n garbage\n2\t2\tx\n"), rss.DiffSize(1, 2, 2))

    def test_buckets(self):
        self.assertEqual([rss.bucket_of(n) for n in (0, 80, 81, 200, 201, 500, 501)],
                         ["<=80", "<=80", "81-200", "81-200", "201-500", "201-500", ">500"])


class VerificationNumstatTests(unittest.TestCase):
    """The verification path measures the staged worktree against the base."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="nexus-821-repo-")
        git(self.repo, "init", "-q")
        Path(self.repo, "a.py").write_text("one\ntwo\nthree\n")
        Path(self.repo, "logo.bin").write_bytes(b"\x00\x01\x02")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()

    def _measure(self, base):
        fake = SimpleNamespace(get_experiment=lambda _id: SimpleNamespace(base_commit_sha=base))
        return VerificationMixin._verification_numstat(fake, "e1", self.repo, MagicMock())

    def test_committed_uncommitted_untracked_and_binary_changes_count(self):
        Path(self.repo, "a.py").write_text("one\nTWO\nthree\nfour\n")
        git(self.repo, "commit", "-qam", "work")
        Path(self.repo, "logo.bin").write_bytes(b"\x00\x09\x09\x09")  # uncommitted binary
        Path(self.repo, "new.py").write_text("x = 1\ny = 2\n")  # untracked

        size = self._measure(self.base)

        self.assertEqual((size.files, size.added, size.removed), (3, 4, 1))
        # The real index stays untouched: the size is measured in a throwaway index.
        self.assertIn("?? new.py", git(self.repo, "status", "--short"))

    def test_an_unknown_base_gives_no_size(self):
        self.assertIsNone(self._measure("0" * 40))


class MigrationTests(unittest.TestCase):
    def test_migration_keeps_old_rows_and_adds_null_columns(self):
        db = str(Path(tempfile.mkdtemp(prefix="nexus-821-db-")) / "agent.db")
        init_db(db).close()
        conn = sqlite3.connect(db)
        for column in ("diff_files", "diff_added", "diff_removed"):
            conn.execute(f"ALTER TABLE experiments DROP COLUMN {column}")  # a pre-#821 database
        conn.execute(
            "INSERT INTO experiments (id, goal, status, created_at, diff_stat) "
            "VALUES ('old', 'Old goal', 'succeeded', '2026-01-01T00:00:00+00:00', ' a.py | 2 +-')"
        )
        conn.commit()
        conn.close()

        init_db(db).close()

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM experiments WHERE id = 'old'").fetchone()
        conn.close()
        self.assertEqual((row["goal"], row["status"], row["diff_stat"]),
                         ("Old goal", "succeeded", " a.py | 2 +-"))
        self.assertIsNone(row["diff_files"])
        self.assertIsNone(row["diff_added"])
        self.assertIsNone(row["diff_removed"])


def _stats_db() -> str:
    db = str(Path(tempfile.mkdtemp(prefix="nexus-821-stats-")) / "agent.db")
    init_db(db).close()
    return db


def _add(db, exp_id, *, lines, status="succeeded", adoption=None, reason=None, ended=None):
    ended = ended or datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db)
    added, removed = (None, None) if lines is None else (lines - lines // 3, lines // 3)
    conn.execute(
        "INSERT INTO experiments (id, goal, status, created_at, ended_at, diff_files, "
        "diff_added, diff_removed) VALUES (?, 'g', ?, ?, ?, ?, ?, ?)",
        (exp_id, status, ended, ended, None if lines is None else 1, added, removed),
    )
    if adoption is not None:
        conn.execute(
            "INSERT INTO experiment_adoptions (id, experiment_id, status, reason, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"a-{exp_id}", exp_id, adoption, reason, ended, ended),
        )
    conn.commit()
    conn.close()


class SizeVsVerdictTests(unittest.TestCase):
    def test_bucket_shares_on_hand_built_rows(self):
        db = _stats_db()
        # <=80: 2 merged, 1 review-blocked, 1 verification needs_review,
        # 1 parked by the adoption (adoption status needs_review)
        _add(db, "s1", lines=10, adoption="adopted")
        _add(db, "s2", lines=80, adoption="succeeded")
        _add(db, "s3", lines=50, adoption="failed", reason=REVIEW_BLOCK)
        _add(db, "s4", lines=5, status="needs_review")
        _add(db, "s5", lines=20, status="succeeded", adoption="needs_review",
             reason="parked by adoption: needs a human")
        # 81-200: 1 merged, 1 failed for another reason (neither share)
        _add(db, "m1", lines=81, adoption="adopted")
        _add(db, "m2", lines=200, adoption="failed", reason="Integrationskonflikt: a.py")
        # >500: 2 review-blocked
        _add(db, "l1", lines=900, adoption="failed", reason=REVIEW_BLOCK)
        _add(db, "l2", lines=501, adoption="failed", reason=REVIEW_BLOCK)
        # not counted: no size, too old, not finished
        _add(db, "u1", lines=None, adoption="adopted")
        _add(db, "old", lines=10, adoption="adopted",
             ended=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat())

        result = rss.size_vs_verdict(db, 30)

        by = {b["bucket"]: b for b in result["buckets"]}
        self.assertEqual([b["bucket"] for b in result["buckets"]], ["<=80", "81-200", "201-500", ">500"])
        self.assertEqual(by["<=80"]["count"], 5)
        self.assertAlmostEqual(by["<=80"]["merged_share"], 0.4)
        self.assertAlmostEqual(by["<=80"]["review_blocked_share"], 0.2)
        self.assertAlmostEqual(by["<=80"]["needs_review_share"], 0.4)
        self.assertEqual(by["81-200"]["count"], 2)
        self.assertAlmostEqual(by["81-200"]["merged_share"], 0.5)
        self.assertEqual(by["81-200"]["review_blocked_share"], 0.0)
        self.assertEqual(by["201-500"],
                         {"bucket": "201-500", "count": 0, "merged_share": None,
                          "review_blocked_share": None, "needs_review_share": None})
        self.assertEqual(by[">500"]["review_blocked_share"], 1.0)
        self.assertEqual((result["sized"], result["unsized"]), (9, 1))
        # A longer window takes the old row in too.
        self.assertEqual(rss.size_vs_verdict(db, 60)["sized"], 10)

    def test_verdict_of_counts_adoption_needs_review(self):
        # Verification passed, then the adoption parked the experiment: the
        # row stays in the bucket denominator and counts as needs_review.
        self.assertEqual(rss.verdict_of("succeeded", "needs_review", "parked"), "needs_review")
        self.assertEqual(rss.verdict_of("needs_review", "", ""), "needs_review")
        self.assertEqual(rss.verdict_of("succeeded", "adopted", ""), "merged")
        self.assertEqual(rss.verdict_of("succeeded", "failed", REVIEW_BLOCK), "review_blocked")
        # A generic experiment error that quotes the review prefix is not a
        # paid review block: only the adoption status and reason decide.
        self.assertEqual(rss.verdict_of("failed", "", REVIEW_BLOCK), "other")
        self.assertFalse(rss.is_review_block("", REVIEW_BLOCK))
        self.assertFalse(rss.is_review_block("needs_review", REVIEW_BLOCK))
        self.assertTrue(rss.is_review_block("failed", REVIEW_BLOCK))

    def _backfill_repo(self):
        repo = tempfile.mkdtemp(prefix="nexus-821-backfill-")
        git(repo, "init", "-q")
        Path(repo, "a.py").write_text("a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        base = git(repo, "rev-parse", "HEAD").strip()
        git(repo, "checkout", "-q", "-b", "experiment/x")
        Path(repo, "a.py").write_text("a\nb\nc\n")
        git(repo, "commit", "-qam", "work")
        sha = git(repo, "rev-parse", "HEAD").strip()
        git(repo, "checkout", "-q", "master")
        # A branch that exists only as an origin remote-tracking ref.
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/experiment/remote-only", sha],
            cwd=repo, check=True, capture_output=True, text=True,
        )
        return repo, base

    def _backfill_row(self, db, exp_id, branch, base, *, ended="2026-01-02T00:00:00+00:00"):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO experiments (id, goal, status, created_at, ended_at, branch_name, "
            "base_commit_sha) VALUES (?, 'g', 'succeeded', '2026-01-01', ?, ?, ?)",
            (exp_id, ended, branch, base),
        )
        conn.commit()
        conn.close()

    def test_backfill_from_branches_that_still_exist(self):
        repo, base = self._backfill_repo()
        db = _stats_db()
        self._backfill_row(db, "kept", "experiment/x", base)
        self._backfill_row(db, "remote", "experiment/remote-only", base)
        self._backfill_row(db, "gone", "experiment/deleted", base)
        # An unfinished row is never selected, so no checkpoint size is
        # stored before verification completes.
        self._backfill_row(db, "running", "experiment/x", base, ended=None)

        result = rss.backfill_diff_sizes(db, repo)

        self.assertEqual(result, {"filled": 2, "missing": 1, "skipped": 0})
        conn = sqlite3.connect(db)
        rows = dict((r[0], r[1:]) for r in conn.execute(
            "SELECT id, diff_files, diff_added, diff_removed FROM experiments"))
        conn.close()
        self.assertEqual(rows["kept"], (1, 2, 0))
        self.assertEqual(rows["remote"], (1, 2, 0))  # via refs/remotes/origin/...
        self.assertEqual(rows["gone"], (None, None, None))
        self.assertEqual(rows["running"], (None, None, None))
        # Running it again fills nothing: the eligible rows are sized now.
        self.assertEqual(rss.backfill_diff_sizes(db, repo),
                         {"filled": 0, "missing": 1, "skipped": 0})

    def test_backfill_rechecks_eligibility_and_base_when_writing(self):
        # A concurrent verification that stores the size (or moves the base)
        # between the backfill's SELECT and UPDATE must win: the write
        # re-checks diff_files/branch/base/ended_at and matches nothing.
        repo, base = self._backfill_repo()
        db = _stats_db()
        self._backfill_row(db, "kept", "experiment/x", base)
        real_run = subprocess.run

        def racing_run(*args, **kwargs):
            result = real_run(*args, **kwargs)
            if args and isinstance(args[0], list) and "diff" in args[0]:
                conn = sqlite3.connect(db)
                conn.execute(
                    "UPDATE experiments SET diff_files = 7, diff_added = 7, diff_removed = 7 "
                    "WHERE id = 'kept'"
                )
                conn.commit()
                conn.close()
            return result

        with patch.object(rss.subprocess, "run", side_effect=racing_run):
            result = rss.backfill_diff_sizes(db, repo)

        self.assertEqual(result, {"filled": 0, "missing": 0, "skipped": 1})
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT diff_files, diff_added, diff_removed FROM experiments WHERE id = 'kept'"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(row), (7, 7, 7))

    def test_resolve_branch_ref_prefers_local_then_origin(self):
        repo, base = self._backfill_repo()
        self.assertEqual(rss.resolve_branch_ref(repo, "experiment/x"), "refs/heads/experiment/x")
        self.assertEqual(rss.resolve_branch_ref(repo, "experiment/remote-only"),
                         "refs/remotes/origin/experiment/remote-only")
        self.assertIsNone(rss.resolve_branch_ref(repo, "experiment/deleted"))
        self.assertIsNone(rss.resolve_branch_ref(repo, ""))


class SplitGuardTests(unittest.TestCase):
    """Two large review-blocked attempts are split, not retried a third time."""

    ISSUES = [{"number": 232, "author": {"login": "tire0011"}, "title": "Wide issue", "body": ""}]

    def setUp(self):
        self.fx = InitiativeFixture()
        with sqlite3.connect(self.fx.db_path) as conn:
            for column in ("diff_files", "diff_added", "diff_removed"):
                conn.execute(f"ALTER TABLE experiments ADD COLUMN {column} INTEGER")
        self.comments: list[tuple[int, str]] = []
        self.prompts: list[str] = []
        os.environ.pop(rss.SPLIT_DIFF_LINES_ENV, None)

    def _block(self, exp_id, ended, lines):
        self.fx.add_experiment(exp_id, "succeeded", adoption="failed", ended=ended)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("UPDATE experiment_adoptions SET reason = ? WHERE experiment_id = ?",
                         (REVIEW_BLOCK, exp_id))
            if lines is not None:
                conn.execute("UPDATE experiments SET diff_files = 3, diff_added = ?, diff_removed = ? "
                             "WHERE id = ?", (lines - 10, 10, exp_id))
            conn.execute("INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, 232)",
                         (exp_id,))

    def _service(self, **extra):
        self.fx.adoption.assess_failure = lambda _id: SimpleNamespace(retryable=False)
        svc = self.fx.service(
            issue_lister=lambda: [dict(i) for i in self.ISSUES],
            issue_commenter=lambda number, text: self.comments.append((number, text)),
            **extra,
        )
        self.addCleanup(svc.close)
        return svc

    def test_two_large_review_blocks_mark_the_issue_for_a_split(self):
        self._block("e1", "2026-10-01T00:00:00+00:00", 450)
        self._block("e2", "2026-10-02T00:00:00+00:00", 301)

        blocked, exhausted = self._service()._issue_attempts()

        self.assertIn(232, blocked)
        self.assertTrue(exhausted[232]["split_guard"])
        self.assertEqual(exhausted[232]["experiment_ids"], ["e1", "e2"])
        self.assertIn("too large to do in one experiment", exhausted[232]["last_reason"])
        self.assertIn("450 and 301", exhausted[232]["last_reason"])

    def test_the_planner_sees_it_only_as_splittable_and_nothing_is_written_to_github(self):
        self._block("e1", "2026-10-01T00:00:00+00:00", 450)
        self._block("e2", "2026-10-02T00:00:00+00:00", 600)
        created = []

        def complete(prompt):
            self.prompts.append(prompt)
            return json.dumps({"issue": 232, "reason": "retry it"})

        svc = self._service(planner=IssuePlanner(complete),
                            issue_creator=lambda *a: created.append(a) or None)

        self.assertEqual(svc.tick().decision, "idle")

        self.assertEqual(self.fx.controller.issues, [])
        self.assertEqual(self.comments, [])
        self.assertEqual(created, [])
        section = self.prompts[0].split("ISSUES OUT OF ATTEMPTS:", 1)[1]
        self.assertIn("#232 Wide issue [split required]: too large to do in one experiment", section)

    def test_one_large_review_block_is_not_marked(self):
        self._block("e1", "2026-10-01T00:00:00+00:00", 900)

        blocked, exhausted = self._service()._issue_attempts()

        self.assertNotIn(232, blocked)
        self.assertNotIn(232, exhausted)

    def test_small_or_unknown_diffs_are_not_marked(self):
        for sizes in ((450, 300), (120, 900), (450, None)):
            with self.subTest(sizes=sizes):
                self.setUp()
                self._block("e1", "2026-10-01T00:00:00+00:00", sizes[0])
                self._block("e2", "2026-10-02T00:00:00+00:00", sizes[1])

                blocked, exhausted = self._service()._issue_attempts()

                self.assertNotIn(232, blocked)
                self.assertNotIn(232, exhausted)

    def test_the_threshold_comes_from_the_environment(self):
        self._block("e1", "2026-10-01T00:00:00+00:00", 450)
        self._block("e2", "2026-10-02T00:00:00+00:00", 301)
        with patch.dict(os.environ, {rss.SPLIT_DIFF_LINES_ENV: "500"}):
            _, exhausted = self._service()._issue_attempts()
        self.assertNotIn(232, exhausted)

    def test_without_size_columns_the_guard_stays_out(self):
        fx = InitiativeFixture()  # the old table has no diff columns
        self.fx = fx
        fx.add_experiment("e1", "succeeded", adoption="failed", ended="2026-10-01T00:00:00+00:00")
        fx.add_experiment("e2", "succeeded", adoption="failed", ended="2026-10-02T00:00:00+00:00")
        with sqlite3.connect(fx.db_path) as conn:
            conn.execute("UPDATE experiment_adoptions SET reason = ?", (REVIEW_BLOCK,))
            conn.executemany("INSERT INTO github_experiment_links VALUES (?, 232)", [("e1",), ("e2",)])

        blocked, exhausted = self._service()._issue_attempts()

        self.assertNotIn(232, exhausted)

    def test_guard_predicate(self):
        self.assertTrue(rss.split_guard_applies([301, 301], 300))
        self.assertTrue(rss.split_guard_applies([10, 400, 500], 300))  # the newest two count
        self.assertFalse(rss.split_guard_applies([301], 300))
        self.assertFalse(rss.split_guard_applies([300, 301], 300))
        self.assertFalse(rss.split_guard_applies([None, 900], 300))

    def test_planning_prompt_tags_only_guarded_issues(self):
        prompt = planning_prompt(
            [], exhausted=[{"number": 1, "title": "A", "last_reason": "x", "split_guard": True},
                           {"number": 2, "title": "B", "last_reason": "y"}])
        self.assertIn("#1 A [split required]: x", prompt)
        self.assertIn("#2 B: y", prompt)


class SplitGuardAdoptionTests(unittest.TestCase):
    """The guard judges paid review blocks by adoption status and reason (#821 review)."""

    ISSUES = [{"number": 232, "author": {"login": "tire0011"}, "title": "Wide issue", "body": ""}]

    def setUp(self):
        self.fx = InitiativeFixture()
        with sqlite3.connect(self.fx.db_path) as conn:
            for column in ("diff_files", "diff_added", "diff_removed"):
                conn.execute(f"ALTER TABLE experiments ADD COLUMN {column} INTEGER")
        os.environ.pop(rss.SPLIT_DIFF_LINES_ENV, None)

    def _link(self, exp_id):
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, 232)",
                (exp_id,),
            )

    def _review_block(self, exp_id, ended, lines):
        self.fx.add_experiment(exp_id, "succeeded", adoption="failed", ended=ended)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("UPDATE experiment_adoptions SET reason = ? WHERE experiment_id = ?",
                         (REVIEW_BLOCK, exp_id))
            conn.execute("UPDATE experiments SET diff_files = 3, diff_added = ?, diff_removed = ? "
                         "WHERE id = ?", (lines - 10, 10, exp_id))
        self._link(exp_id)

    def _service(self):
        self.fx.adoption.assess_failure = lambda _id: SimpleNamespace(retryable=False)
        svc = self.fx.service(issue_lister=lambda: [dict(i) for i in self.ISSUES])
        self.addCleanup(svc.close)
        return svc

    def test_a_generic_error_quoting_the_review_prefix_is_not_a_review_block(self):
        # Both attempts failed with an *experiment* error that starts with
        # "Review by " but no adoption stopped the merge: not paid review blocks.
        for exp_id, ended, lines in (("e1", "2026-10-01T00:00:00+00:00", 900),
                                     ("e2", "2026-10-02T00:00:00+00:00", 800)):
            self.fx.add_experiment(exp_id, "failed", ended=ended)
            with sqlite3.connect(self.fx.db_path) as conn:
                conn.execute("UPDATE experiments SET error = ?, diff_files = 3, diff_added = ?, "
                             "diff_removed = ? WHERE id = ?", (REVIEW_BLOCK, lines - 10, 10, exp_id))
            self._link(exp_id)

        blocked, exhausted = self._service()._issue_attempts()

        # The two failed attempts fill the ordinary cap, but the issue is
        # exhausted without the split guard: no paid review block was seen.
        self.assertIn(232, blocked)
        self.assertFalse(exhausted.get(232, {}).get("split_guard"))

    def test_three_large_review_blocks_still_get_the_split_reason(self):
        # A third historical review block already fills the attempt cap; the
        # split reason must still be applied to the exhausted issue.
        self._review_block("e1", "2026-09-30T00:00:00+00:00", 450)
        self._review_block("e2", "2026-10-01T00:00:00+00:00", 600)
        self._review_block("e3", "2026-10-02T00:00:00+00:00", 301)

        blocked, exhausted = self._service()._issue_attempts()

        self.assertIn(232, blocked)
        self.assertTrue(exhausted[232]["split_guard"])
        self.assertIn("too large to do in one experiment", exhausted[232]["last_reason"])
        # the newest two blocks are named in the reason
        self.assertIn("600 and 301", exhausted[232]["last_reason"])

    def test_active_or_succeeded_work_keeps_the_plain_block(self):
        self._review_block("e1", "2026-10-01T00:00:00+00:00", 450)
        self._review_block("e2", "2026-10-02T00:00:00+00:00", 301)
        # a succeeded, adopted experiment on the same issue
        self.fx.add_experiment("e3", "succeeded", adoption="adopted", ended="2026-10-03T00:00:00+00:00")
        self._link("e3")

        blocked, exhausted = self._service()._issue_attempts()

        self.assertIn(232, blocked)
        self.assertNotIn(232, exhausted)


class SplitApprovalTests(unittest.TestCase):
    """The parent's approval is validated before a split is offered and again
    before children are created (#821 review)."""

    ISSUE = {"number": 232, "author": {"login": "outside-agent"},
             "title": "Wide issue", "body": "original body"}

    def setUp(self):
        self.fx = InitiativeFixture()
        with sqlite3.connect(self.fx.db_path) as conn:
            for column in ("diff_files", "diff_added", "diff_removed"):
                conn.execute(f"ALTER TABLE experiments ADD COLUMN {column} INTEGER")
        os.environ.pop(rss.SPLIT_DIFF_LINES_ENV, None)
        for exp_id, ended, lines in (("e1", "2026-10-01T00:00:00+00:00", 450),
                                     ("e2", "2026-10-02T00:00:00+00:00", 301)):
            self.fx.add_experiment(exp_id, "succeeded", adoption="failed", ended=ended)
            with sqlite3.connect(self.fx.db_path) as conn:
                conn.execute("UPDATE experiment_adoptions SET reason = ? WHERE experiment_id = ?",
                             (REVIEW_BLOCK, exp_id))
                conn.execute("UPDATE experiments SET diff_files = 3, diff_added = ?, diff_removed = ? "
                             "WHERE id = ?", (lines - 10, 10, exp_id))
                conn.execute("INSERT INTO github_experiment_links (experiment_id, issue_number) "
                             "VALUES (?, 232)", (exp_id,))
        self.prompts: list[str] = []
        self.comments: list[str] = []

    def _service(self, issues, *, issue_creator=None, planner=None):
        from tests.test_nexus_initiative import approve_issue

        self.fx.adoption.assess_failure = lambda _id: SimpleNamespace(retryable=False)
        svc = self.fx.service(
            issue_creator=issue_creator or MagicMock(),
            issue_commenter=lambda n, t: self.comments.append(t),
            planner=planner,
        )
        # No automatic re-approval on every listing: the tests seed the
        # registry themselves and then change its state.
        svc._issue_lister = lambda: [dict(i) for i in issues]
        self.addCleanup(svc.close)
        return svc

    def _capture_planner(self):
        def complete(prompt):
            self.prompts.append(prompt)
            return json.dumps({"issue": None, "reason": "nothing fits"})
        return IssuePlanner(complete)

    def _approve(self, issue=None):
        from tests.test_nexus_initiative import approve_issue

        approve_issue(self.fx.db_path, issue or self.ISSUE)

    def _revoke(self):
        conn = sqlite3.connect(self.fx.db_path)
        issue_approvals.ensure_issue_approvals_table(conn)
        issue_approvals.record_decision(
            conn, repository=TEST_REPOSITORY, issue_number=232, status="revoked",
        )
        conn.close()

    def test_approved_parent_is_offered_and_split_children_are_created_once(self):
        self._approve()
        creator = MagicMock(side_effect=[301, 302])

        def complete(prompt):
            self.prompts.append(prompt)
            return json.dumps({
                "issue": None, "reason": "too large for one experiment",
                "split": {"issue": 232, "parts": [
                    {"title": "p1", "body": "b1"}, {"title": "p2", "body": "b2"},
                ]},
            })

        svc = self._service([self.ISSUE], issue_creator=creator, planner=IssuePlanner(complete))

        self.assertEqual(svc.tick().decision, "idle")
        self.assertEqual(creator.call_count, 2)
        self.assertIn("[split required]", self.prompts[0])
        detail = svc._splits()[232][1]
        self.assertEqual(detail["children"], [301, 302])
        self.assertEqual(len(detail["plan"]), 2)
        self.assertTrue(any("#301, #302" in c for c in self.comments))

        # Running the tick again creates nothing new and comments nothing twice.
        self.assertEqual(svc.tick().decision, "idle")
        self.assertEqual(creator.call_count, 2)
        self.assertEqual(len(self.comments), 1)

    def test_revoked_approval_is_not_offered_and_blocks_child_creation(self):
        self._approve()
        self._revoke()
        creator = MagicMock()
        svc = self._service([self.ISSUE], issue_creator=creator, planner=self._capture_planner())

        svc.tick()

        section = self.prompts[0].split("ISSUES OUT OF ATTEMPTS:", 1)[1]
        self.assertIn("#232 Wide issue", section)
        self.assertNotIn("[split required]", section)
        svc._split_issue(232, [("p1", "b1"), ("p2", "b2")], "too large", [])
        creator.assert_not_called()

    def test_an_edit_after_approval_is_not_offered_for_a_split(self):
        self._approve()
        edited = dict(self.ISSUE, body="edited after approval")
        creator = MagicMock()
        svc = self._service([edited], issue_creator=creator, planner=self._capture_planner())

        svc.tick()

        section = self.prompts[0].split("ISSUES OUT OF ATTEMPTS:", 1)[1]
        self.assertNotIn("[split required]", section)
        svc._split_issue(232, [("p1", "b1"), ("p2", "b2")], "too large", [])
        creator.assert_not_called()


class SplitProgressTests(unittest.TestCase):
    """An interrupted split persists partial progress and resumes without
    duplicates; the parent closes only after the full split exists (#821 review)."""

    ISSUE = {"number": 232, "author": {"login": "tire0011"}, "title": "Wide issue", "body": ""}

    def setUp(self):
        self.fx = InitiativeFixture()
        with sqlite3.connect(self.fx.db_path) as conn:
            for column in ("diff_files", "diff_added", "diff_removed"):
                conn.execute(f"ALTER TABLE experiments ADD COLUMN {column} INTEGER")
        for exp_id, ended, lines in (("e1", "2026-10-01T00:00:00+00:00", 450),
                                     ("e2", "2026-10-02T00:00:00+00:00", 301)):
            self.fx.add_experiment(exp_id, "succeeded", adoption="failed", ended=ended)
            with sqlite3.connect(self.fx.db_path) as conn:
                conn.execute("UPDATE experiment_adoptions SET reason = ? WHERE experiment_id = ?",
                             (REVIEW_BLOCK, exp_id))
                conn.execute("UPDATE experiments SET diff_files = 3, diff_added = ?, diff_removed = ? "
                             "WHERE id = ?", (lines - 10, 10, exp_id))
                conn.execute("INSERT INTO github_experiment_links (experiment_id, issue_number) "
                             "VALUES (?, 232)", (exp_id,))
        from tests.test_nexus_initiative import approve_issue

        approve_issue(self.fx.db_path, self.ISSUE)
        self.fx.adoption.assess_failure = lambda _id: SimpleNamespace(retryable=False)
        self.creator = MagicMock()
        self.closer = MagicMock()
        self.comments: list[str] = []
        self.svc = self.fx.service(
            issue_lister=lambda: [dict(self.ISSUE)],
            issue_creator=self.creator,
            issue_closer=self.closer,
            issue_commenter=lambda n, t: self.comments.append(t),
        )
        self.addCleanup(self.svc.close)

    def test_interrupted_split_resumes_without_duplicates_and_closes_only_when_complete(self):
        self.svc._issue_candidates()  # populates the exhausted entry (approval valid)
        self.creator.side_effect = [301, RuntimeError("github down"), 302, 303]
        parts = [("p1", "b1"), ("p2", "b2"), ("p3", "b3")]

        # The second child creation fails: only the first part exists.
        self.svc._split_issue(232, parts, "too large", [])
        detail = self.svc._splits()[232][1]
        self.assertEqual(detail["children"], [301])
        self.assertEqual(detail["parts"], 3)
        self.assertEqual(len(detail["plan"]), 3)

        # The parent must not close after only the recorded subset.
        self.svc._close_finished_splits({232})
        self.closer.assert_not_called()

        # Resume creates only the missing parts; the failed one is retried,
        # the created one is never duplicated.
        self.svc._resume_incomplete_splits()
        detail = self.svc._splits()[232][1]
        self.assertEqual(detail["children"], [301, 302, 303])
        titles = [c.args[0] for c in self.creator.call_args_list]
        self.assertEqual(titles, ["p1", "p2", "p2", "p3"])
        self.assertTrue(any("#301, #302, #303" in c for c in self.comments))

        # Running the resume or the split again changes nothing.
        self.svc._resume_incomplete_splits()
        self.svc._split_issue(232, parts, "too large", [])
        self.assertEqual(self.creator.call_count, 4)
        self.assertEqual(len(self.comments), 1)

        # Now the full split exists: the parent closes once all parts are closed.
        self.svc._close_finished_splits({232})
        self.closer.assert_called_once()
        self.assertEqual(self.svc._splits()[232][1]["closed"], True)
        # Closing again is a no-op.
        self.svc._close_finished_splits({232})
        self.closer.assert_called_once()

    def test_legacy_split_record_without_a_plan_still_closes(self):
        self.svc._issue_candidates()
        tick_id = "legacy-tick"
        self.svc._record(
            __import__("modules.nexus_initiative", fromlist=["InitiativeDecision"]).InitiativeDecision(
                "split",
                candidate=__import__("modules.nexus_initiative", fromlist=["Candidate"]).Candidate(
                    "open_issue", "232", "", "reason"),
                detail=json.dumps({"children": [301], "closed": False}),
                tick_id=tick_id,
            ),
        )
        # No stored plan: nothing to resume, and no new children either.
        self.svc._resume_incomplete_splits()
        self.svc._split_issue(232, [("p1", "b1"), ("p2", "b2")], "reason", [])
        self.creator.assert_not_called()
        # The old record closes when its recorded child is closed.
        self.svc._close_finished_splits({232})
        self.closer.assert_called_once()


class VerificationDiffSizeTests(unittest.TestCase):
    """The measured size is persisted right after staging and the write is
    guarded, so interruptions and reruns behave (#821 review)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo_root = root / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)
        selection_worker = MagicMock()
        selection_worker.run.return_value = _make_result(final_message="Research summary: x")
        plan_worker = MagicMock()
        plan_worker.run.return_value = _make_result()
        implementation_worker = MagicMock()
        implementation_worker.run.side_effect = _write_affected_files_side_effect()
        self.service = ExperimentService(
            selection_worker=selection_worker,
            plan_worker_factory=lambda scratch: plan_worker,
            worker_factory=lambda path: implementation_worker,
            db_path=str(root / "agent.db"),
            repo_root=str(self.repo_root),
            worktrees_root=str(root / "worktrees"),
        )
        self.addCleanup(self.service.close)

    def _row(self, exp_id):
        with self.service._lock:
            return self.service._conn.execute(
                "SELECT status, diff_files, diff_added, diff_removed FROM experiments WHERE id = ?",
                (exp_id,),
            ).fetchone()

    def test_store_and_clear_are_guarded_by_the_running_verification(self):
        exp = self.service.create_experiment(goal="g")
        self.service._force_status(exp.id, STATUS_VERIFYING)
        self.service._store_diff_size(exp.id, rss.DiffSize(2, 10, 5))
        self.assertEqual(tuple(self._row(exp.id))[1:], (2, 10, 5))
        # A terminal state (external stop, another attempt's verdict) blocks
        # the write: the guard cannot overwrite newer evidence.
        self.service._force_status(exp.id, STATUS_FAILED, ended_at="2026-01-01T00:00:00+00:00")
        self.service._store_diff_size(exp.id, rss.DiffSize(9, 9, 9))
        self.service._clear_diff_size(exp.id)
        self.assertEqual(tuple(self._row(exp.id))[1:], (2, 10, 5))
        # While verifying, None stores NULL and clear drops the values.
        self.service._force_status(exp.id, STATUS_VERIFYING)
        self.service._store_diff_size(exp.id, None)
        self.assertEqual(tuple(self._row(exp.id))[1:], (None, None, None))
        self.service._store_diff_size(exp.id, rss.DiffSize(1, 4, 1))
        self.service._clear_diff_size(exp.id)
        self.assertEqual(tuple(self._row(exp.id))[1:], (None, None, None))

    def test_size_survives_a_hung_test_and_a_stale_size_is_cleared(self):
        exp = self.service.create_experiment(goal="g")
        with self.service._lock, self.service._conn:
            self.service._conn.execute(
                "UPDATE experiments SET diff_files = 99, diff_added = 99, diff_removed = 99 "
                "WHERE id = ?",
                (exp.id,),
            )
        with patch(
            "modules.experiment_service.service_parts.verification.run_verification_test_monitored",
            side_effect=HungTest("stalled"),
        ):
            self.service.run_experiment(exp.id)

        row = self._row(exp.id)
        # The crash is still the verdict, but the measured size survived: it
        # was persisted immediately after staging, before the raise.
        self.assertEqual(row[0], STATUS_FAILED)
        self.assertIsNotNone(row[2])
        self.assertNotEqual(row[2], 99)
        self.assertGreater(row[2], 0)
        self.assertEqual(row[1], 1)  # the stub file is the one changed file

    def test_normal_run_stores_the_measured_size(self):
        exp = self.service.create_experiment(goal="g")
        result = self.service.run_experiment(exp.id)
        row = self._row(exp.id)
        self.assertEqual(row[0], result.status)
        self.assertEqual(row[1], 1)  # the one changed file
        self.assertGreaterEqual(row[2], 1)  # added lines
        self.assertGreaterEqual(row[3], 0)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.db = _stats_db()
        _add(self.db, "s1", lines=10, adoption="adopted")
        service = MagicMock()
        service.db_path = self.db
        mailbox = MagicMock()
        mailbox.recover_after_restart.return_value = None
        api.app.dependency_overrides[api.get_experiment_service] = lambda: service
        api.app.dependency_overrides[api.get_codex_service] = lambda: MagicMock()
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)

    def test_401_without_or_with_a_wrong_key(self):
        for headers in ({}, {"x-api-key": "wrong"}):
            self.assertEqual(self.client.get("/review-size-stats", headers=headers).status_code, 401)

    def test_returns_the_buckets_with_the_key(self):
        response = self.client.get("/review-size-stats?days=7", headers={"x-api-key": api.API_KEY})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["days"], 7)
        self.assertEqual(body["buckets"][0]["count"], 1)
        self.assertEqual(body["buckets"][0]["merged_share"], 1.0)

    def test_is_a_read_tool(self):
        self.assertIn("review_size_stats", READ_ONLY_TOOLS)


if __name__ == "__main__":
    unittest.main()
