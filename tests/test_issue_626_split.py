"""The planner splits a stuck or large issue into parts it can do (#626).

#158, #294 and #349 ran out of attempts; Nexus could only leave them for the
owner to rewrite. Now it opens 2-4 parts of its own and closes the parent when
they are all done.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from modules.nexus_planner import IssuePlanner, parse_choice
from tests.test_nexus_initiative import InitiativeFixture

PARTS = [{"title": "Part one", "body": "change a, test a"}, {"title": "Probe two", "body": "measure b, comment"}]


class ParseSplitTests(unittest.TestCase):
    def test_a_split_alone_is_a_valid_answer(self):
        choice = parse_choice(json.dumps({"issue": None, "reason": "too large",
                                          "split": {"issue": 232, "parts": PARTS}}), set(), {232})
        self.assertEqual((choice.issue, choice.split_issue, len(choice.split_parts)), (0, 232, 2))

    def test_a_split_of_an_unknown_issue_or_one_part_is_ignored(self):
        self.assertIsNone(parse_choice(json.dumps({"reason": "x", "split": {"issue": 9, "parts": PARTS}}), set(), {232}))
        one = {"reason": "x", "split": {"issue": 232, "parts": PARTS[:1]}}
        self.assertIsNone(parse_choice(json.dumps(one), set(), {232}))
        five = {"issue": 7, "reason": "x", "split": {"issue": 232, "parts": PARTS * 3}}
        self.assertIsNone(parse_choice(json.dumps(five), {7}, {232}).split_parts)


class SplitTickTests(unittest.TestCase):
    ISSUES = [
        {"number": 232, "author": {"login": "tire0011"}, "title": "Stuck issue", "body": ""},
        {"number": 7, "author": {"login": "tire0011"}, "title": "Free issue", "body": ""},
    ]

    def setUp(self):
        self.fx = InitiativeFixture()
        self.comments: list[tuple[int, str]] = []
        self.answer = ""
        self.created: list[tuple[str, str]] = []
        self.closed: list[tuple[int, str]] = []
        self.next_number = 700

    def _create(self, title, body):
        self.created.append((title, body))
        self.next_number += 1
        return self.next_number

    def _fail(self, exp_id, *, error, ended):
        self.fx.add_experiment(exp_id, "failed", ended=ended)
        with sqlite3.connect(self.fx.db_path) as conn:
            conn.execute("UPDATE experiments SET error = ? WHERE id = ?", (error, exp_id))
            conn.execute("INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, 232)",
                         (exp_id,))

    def _split_service(self, issues=None):
        svc = self.fx.service(
            issue_lister=lambda: [dict(i) for i in (issues or self.ISSUES)],
            issue_commenter=lambda number, text: self.comments.append((number, text)),
            planner=IssuePlanner(lambda prompt: self.answer),
            issue_creator=self._create, issue_closer=lambda n, t: self.closed.append((n, t)),
        )
        self.addCleanup(svc.close)
        return svc

    def _exhaust_232(self):
        self._fail("e1", error="first", ended="2026-10-01T00:00:00+00:00")
        self._fail("e2", error="second", ended="2026-10-02T00:00:00+00:00")

    def test_a_stuck_issue_is_split_even_when_nothing_else_is_open(self):
        self._exhaust_232()
        self.answer = json.dumps({"issue": None, "reason": "split the stuck one",
                                  "split": {"issue": 232, "parts": PARTS}})
        svc = self._split_service(issues=self.ISSUES[:1])
        svc.tick()
        self.assertEqual([t for t, _ in self.created], ["Part one", "Probe two"])
        self.assertIn("Part 1 of 2 of #232", self.created[0][1])
        self.assertIn("nexus-split-depth: 1", self.created[0][1])
        self.assertTrue(any(n == 232 and "#701, #702" in t for n, t in self.comments))

    def test_the_same_issue_is_split_only_once(self):
        self._exhaust_232()
        self.answer = json.dumps({"issue": None, "reason": "split", "split": {"issue": 232, "parts": PARTS}})
        svc = self._split_service(issues=self.ISSUES[:1])
        svc.tick()
        svc.tick()
        self.assertEqual(len(self.created), 2)

    def test_a_part_of_a_part_is_not_split_again(self):
        self._exhaust_232()
        deep = [{**self.ISSUES[0], "body": "x\n<!-- nexus-split-depth: 2 -->"}]
        self.answer = json.dumps({"issue": None, "reason": "split", "split": {"issue": 232, "parts": PARTS}})
        self._split_service(issues=deep).tick()
        self.assertEqual(self.created, [])

    def test_the_parent_closes_when_all_parts_are_closed(self):
        self._exhaust_232()
        self.answer = json.dumps({"issue": None, "reason": "split", "split": {"issue": 232, "parts": PARTS}})
        self._split_service(issues=self.ISSUES[:1]).tick()
        part = {"number": 701, "author": {"login": "tire0011"}, "title": "Part one", "body": ""}
        self._split_service(issues=[self.ISSUES[0], part]).tick()
        self.assertEqual(self.closed, [])
        self._split_service(issues=self.ISSUES[:1]).tick()
        self.assertEqual([n for n, _ in self.closed], [232])
        self._split_service(issues=self.ISSUES[:1]).tick()
        self.assertEqual(len(self.closed), 1)

    def test_a_plain_pick_still_works_alongside_a_split(self):
        self._exhaust_232()
        self.answer = json.dumps({"issue": 7, "reason": "free one", "split": {"issue": 232, "parts": PARTS}})
        decision = self._split_service().tick()
        self.assertEqual(decision.candidate.ref, "7")
        self.assertEqual(len(self.created), 2)


if __name__ == "__main__":
    unittest.main()
