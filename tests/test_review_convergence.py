"""Review blockades converge (2026-11-22: 66 blocked vs 56 adopted; #782 and
#763 took 3-4 rounds, each review finding a new edge case). From the second
block on an issue, only task, bug and regression problems block; edge cases
become a follow-up issue and outside work a question to the owner."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import modules.nexus_reviewer as nexus_reviewer
from modules.nexus_reviewer import NexusReviewer, ReviewResult, converge, parse_problems, parse_review


def _answer(*problems):
    return json.dumps({"verdict": "block", "summary": "Adds the bounded wait.",
                       "problems": [{"kind": k, "problem": t} for k, t in problems]})


class ParseTests(unittest.TestCase):
    def test_kinds_are_parsed_and_plain_strings_count_as_bugs(self):
        raw = json.dumps({"verdict": "block", "summary": "s",
                          "problems": ["old style", {"kind": "edge_case", "problem": "rare"},
                                       {"kind": "nonsense", "problem": "x"}]})
        self.assertEqual(parse_problems(raw), [("bug", "old style"), ("edge_case", "rare"), ("bug", "x")])
        self.assertIn("(2) rare", parse_review(raw)[1])

    def test_first_block_always_blocks(self):
        self.assertEqual(converge("block", "s", [("edge_case", "rare")], 0)[0], "block")

    def test_later_block_with_only_edge_cases_approves_and_defers_them(self):
        verdict, summary, deferred = converge(
            "block", "s Blocking problems: (1) rare", [("edge_case", "rare"), ("elsewhere", "mailbox client")], 1)
        self.assertEqual(verdict, "approve")
        self.assertIn("deferred to follow-up", summary)
        self.assertEqual(deferred, [("edge_case", "rare"), ("elsewhere", "mailbox client")])

    def test_later_block_with_a_real_bug_still_blocks(self):
        self.assertEqual(converge("block", "s", [("edge_case", "rare"), ("regression", "drops check")], 3)[0],
                         "block")


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "agent.db")
        patcher = mock.patch.object(nexus_reviewer, "_diff", return_value="diff --git a/x b/x")
        patcher.start()
        self.addCleanup(patcher.stop)
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE github_experiment_links (issue_number INTEGER, experiment_id TEXT)")
            conn.executemany("INSERT INTO github_experiment_links VALUES (?, ?)", [(797, "e1"), (797, "e2")])

    def test_the_second_attempt_at_an_issue_converges(self):
        answer = _answer(("edge_case", "a late worker start can outrun the deadline"))
        reviewer = NexusReviewer(db_path=self.db, runners={"codex": lambda p, w: answer})
        with mock.patch.object(nexus_reviewer, "REVIEWERS", ("codex",)):
            first = reviewer.review(experiment_id="e1", goal="g", worktree_path=".", base_sha="")
            second = reviewer.review(experiment_id="e2", goal="g", worktree_path=".", base_sha="")
        self.assertEqual(first.verdict, "block")
        self.assertEqual(second.verdict, "approve")
        self.assertEqual(second.deferred, [("edge_case", "a late worker start can outrun the deadline")])

    def test_the_prompt_asks_for_problem_kinds(self):
        prompt = nexus_reviewer.review_prompt("g", "d", can_read_files=True)
        for kind in ("task", "bug", "regression", "edge_case", "elsewhere"):
            self.assertIn(f'"{kind}"', prompt)


class FollowUpTests(unittest.TestCase):
    def test_deferred_findings_open_a_follow_up_and_an_owner_question(self):
        from modules.experiment_adoption_parts import github_pr

        tracking = mock.Mock()
        tracking.open_issue.return_value = {"number": 900}
        tracking.open_need_issue.return_value = {"number": 901}
        holder = SimpleNamespace(github_work_tracking=tracking)
        result = ReviewResult("codex", "approve", "s", [("edge_case", "rare"), ("elsewhere", "mailbox client")])
        github_pr.GitHubPrMixin._open_review_follow_ups(holder, SimpleNamespace(issue_number=797, pr_number=12), result, None)
        title, body = tracking.open_issue.call_args[0]
        self.assertIn("#797", title)
        self.assertIn("- rare", body)
        self.assertIn("mailbox client", tracking.open_need_issue.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
