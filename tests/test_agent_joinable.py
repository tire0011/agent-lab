"""Outside agents can join: SKILL.md, a status issue and refusals with an unlock."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules import nexus_status_issue

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


class _Gh:
    def __init__(self, issues=()):
        self.issues = list(issues)
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["issue", "list"]:
            return json.dumps(self.issues)
        return ""


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE experiments (id TEXT, status TEXT, created_at TEXT, goal TEXT)")
    conn.execute("INSERT INTO experiments VALUES ('21883e58aa', 'succeeded', '2026-11-08T22:03', 'GitHub Issue #597: loop')")
    return conn


def _update(gh, **kw):
    return nexus_status_issue.update(_conn(), "/x", budget=10, spent=3, paused=False, busy=None, now=NOW, gh=gh, **kw)


class StatusIssueTests(unittest.TestCase):
    def test_a_missing_status_issue_is_created_as_proposal_and_pinned(self):
        gh = _Gh()
        self.assertTrue(_update(gh))
        create = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        self.assertIn("proposal", create)
        self.assertIn("21883e58", create[create.index("--body") + 1])
        self.assertIn("3 of 10", create[create.index("--body") + 1])

    def test_it_is_rewritten_at_most_once_an_hour(self):
        recent = {"number": 5, "title": "Nexus status", "updatedAt": (NOW - timedelta(minutes=20)).isoformat()}
        gh = _Gh([recent])
        self.assertFalse(_update(gh))
        old = {**recent, "updatedAt": (NOW - timedelta(hours=2)).isoformat()}
        gh = _Gh([old])
        self.assertTrue(_update(gh))
        self.assertEqual(gh.calls[-1][:3], ["issue", "edit", "5"])

    def test_a_gh_failure_never_raises(self):
        def broken(argv):
            raise RuntimeError("gh down")
        self.assertFalse(_update(broken))


class SkillFileTests(unittest.TestCase):
    def test_skill_md_has_machine_readable_front_matter(self):
        text = (ROOT / "SKILL.md").read_text()
        head = text.split("---")[1]
        for key in ("name:", "description:", "status_url:", "tools_required:", "constraints:"):
            self.assertIn(key, head)


class ApprovalCommentTests(unittest.TestCase):
    def test_a_skipped_outside_issue_gets_rule_reason_and_unlock(self):
        from modules.nexus_initiative import NexusInitiativeService

        comments = []
        service = NexusInitiativeService.__new__(NexusInitiativeService)
        service._issue_commenter = lambda number, text: comments.append((number, text))
        service._comment_waiting_for_approval(230, "issue #230 has no approval record (pending)")
        number, text = comments[0]
        self.assertEqual(number, 230)
        for part in ("owner-approval-required", "no approval record", "unlock:"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()
