from __future__ import annotations

import re
import unittest
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "ideas-to-issues.md"

# Ids from data_sources.list_ideas() on main (60aec40) of every idea still
# marked `new` or `exploring`; the other 13 of 69 were adopted/tested/rejected.
OPEN_IDEAS = {
    4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 21, 22, 23, 24, 25,
    27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44,
    45, 46, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 64, 66, 67, 68,
}
_IDEA_RE = re.compile(r"^- \*\*idea-(\d+)\*\* (.+) `(new|exploring)`$")
_DECISION_RE = re.compile(r"^  - \*\*(create|drop):\*\* (.+)$")
_ISSUE_RE = re.compile(r"^### (P\d+) (.+)$")


def _lines() -> list[str]:
    return DOC.read_text(encoding="utf-8").splitlines()


def _entries() -> list[tuple[int, str, str, str]]:
    lines = _lines()
    entries = []
    for i, line in enumerate(lines):
        idea = _IDEA_RE.match(line)
        if not idea:
            continue
        decision = _DECISION_RE.match(lines[i + 1]) if i + 1 < len(lines) else None
        if decision is None:
            raise AssertionError(f"idea-{idea.group(1)} has no decision line")
        entries.append((int(idea.group(1)), idea.group(2), decision.group(1), decision.group(2)))
    return entries


def _proposed_issues() -> dict[str, str]:
    return {m.group(1): m.group(2) for m in map(_ISSUE_RE.match, _lines()) if m}


class IdeasToIssuesDocTest(unittest.TestCase):
    def test_every_open_idea_appears_exactly_once(self):
        numbers = [entry[0] for entry in _entries()]
        self.assertEqual(len(numbers), len(set(numbers)), "duplicate idea entries")
        self.assertEqual(set(numbers), OPEN_IDEAS)

    def test_create_names_a_proposed_issue_and_drop_gives_a_reason(self):
        issues = _proposed_issues()
        for number, text, decision, detail in _entries():
            with self.subTest(idea=number):
                self.assertTrue(text.strip())
                if decision == "create":
                    self.assertIn(detail, issues)
                else:
                    self.assertTrue(detail.endswith("."))

    def test_every_proposed_issue_has_a_title_and_comes_from_an_idea(self):
        issues = _proposed_issues()
        self.assertTrue(issues)
        used = {detail for _, _, decision, detail in _entries() if decision == "create"}
        self.assertEqual(set(issues), used)
        for key, title in issues.items():
            with self.subTest(issue=key):
                self.assertTrue(title.strip())

    def test_drop_as_duplicate_names_an_issue_number(self):
        for number, _, decision, detail in _entries():
            if decision == "drop" and detail.startswith("Duplicate"):
                with self.subTest(idea=number):
                    self.assertRegex(detail, r"#\d+")


if __name__ == "__main__":
    unittest.main()
