from __future__ import annotations

import re
import unittest
from pathlib import Path

TRIAGE_DOC = Path(__file__).resolve().parent.parent / "docs" / "issue-triage.md"

# `gh issue list --state open --limit 100` on 2026-10-24, without #336 itself
# and #322 (created within the last 7 days).
OPEN_ISSUES = {
    294, 288, 276, 264, 263, 257, 248, 239, 236, 235, 232, 230, 221, 220, 196,
    185, 179, 163, 158, 156, 150, 120, 119, 118, 117, 86, 84, 75, 74, 43, 41,
    26, 25, 24,
}
DECISIONS = {"close-done", "close-outdated", "keep"}
_ROW_RE = re.compile(r"^\| #(\d+) (.+?) \| (\S+) \| (.+) \|$")


def _rows() -> list[tuple[int, str, str, str]]:
    rows = []
    for line in TRIAGE_DOC.read_text(encoding="utf-8").splitlines():
        match = _ROW_RE.match(line)
        if match:
            number, title, decision, reason = match.groups()
            rows.append((int(number), title, decision, reason))
    return rows


class IssueTriageDocTest(unittest.TestCase):
    def test_every_open_issue_appears_exactly_once(self):
        numbers = [row[0] for row in _rows()]
        self.assertEqual(len(numbers), len(set(numbers)), "duplicate issue rows")
        self.assertEqual(set(numbers), OPEN_ISSUES)

    def test_excluded_issues_are_not_listed(self):
        numbers = {row[0] for row in _rows()}
        self.assertNotIn(336, numbers)
        self.assertNotIn(322, numbers)

    def test_every_row_has_a_valid_decision_and_a_reason(self):
        for number, title, decision, reason in _rows():
            with self.subTest(issue=number):
                self.assertTrue(title.strip())
                self.assertIn(decision, DECISIONS)
                self.assertTrue(reason.strip().endswith("."))


if __name__ == "__main__":
    unittest.main()
