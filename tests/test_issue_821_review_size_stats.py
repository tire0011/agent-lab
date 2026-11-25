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
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules import review_size_stats as rss
from modules.experiment_service.service_parts.verification import VerificationMixin
from modules.nexus_mcp_tools import READ_ONLY_TOOLS
from modules.nexus_planner import IssuePlanner, planning_prompt
from modules.run_store import init_db
from tests.test_nexus_initiative import InitiativeFixture

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
        # <=80: 2 merged, 1 review-blocked, 1 needs_review
        _add(db, "s1", lines=10, adoption="adopted")
        _add(db, "s2", lines=80, adoption="succeeded")
        _add(db, "s3", lines=50, adoption="failed", reason=REVIEW_BLOCK)
        _add(db, "s4", lines=5, status="needs_review")
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
        self.assertEqual(by["<=80"]["count"], 4)
        self.assertAlmostEqual(by["<=80"]["merged_share"], 0.5)
        self.assertAlmostEqual(by["<=80"]["review_blocked_share"], 0.25)
        self.assertAlmostEqual(by["<=80"]["needs_review_share"], 0.25)
        self.assertEqual(by["81-200"]["count"], 2)
        self.assertAlmostEqual(by["81-200"]["merged_share"], 0.5)
        self.assertEqual(by["81-200"]["review_blocked_share"], 0.0)
        self.assertEqual(by["201-500"],
                         {"bucket": "201-500", "count": 0, "merged_share": None,
                          "review_blocked_share": None, "needs_review_share": None})
        self.assertEqual(by[">500"]["review_blocked_share"], 1.0)
        self.assertEqual((result["sized"], result["unsized"]), (8, 1))
        # A longer window takes the old row in too.
        self.assertEqual(rss.size_vs_verdict(db, 60)["sized"], 9)

    def test_backfill_from_a_branch_that_still_exists(self):
        repo = tempfile.mkdtemp(prefix="nexus-821-backfill-")
        git(repo, "init", "-q")
        Path(repo, "a.py").write_text("a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        base = git(repo, "rev-parse", "HEAD").strip()
        git(repo, "checkout", "-q", "-b", "experiment/x")
        Path(repo, "a.py").write_text("a\nb\nc\n")
        git(repo, "commit", "-qam", "work")
        db = _stats_db()
        conn = sqlite3.connect(db)
        conn.executemany(
            "INSERT INTO experiments (id, goal, status, created_at, branch_name, base_commit_sha) "
            "VALUES (?, 'g', 'succeeded', '2026-01-01', ?, ?)",
            [("kept", "experiment/x", base), ("gone", "experiment/deleted", base)],
        )
        conn.commit()
        conn.close()

        self.assertEqual(rss.backfill_diff_sizes(db, repo), {"filled": 1, "missing": 1})

        conn = sqlite3.connect(db)
        rows = dict((r[0], r[1:]) for r in conn.execute(
            "SELECT id, diff_files, diff_added, diff_removed FROM experiments"))
        conn.close()
        self.assertEqual(rows["kept"], (1, 2, 0))
        self.assertEqual(rows["gone"], (None, None, None))


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
