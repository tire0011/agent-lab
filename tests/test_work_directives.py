from __future__ import annotations

import unittest

from modules.work_directives import parse_work_directives


class GoalHeaderDirectiveTests(unittest.TestCase):
    def test_real_controller_issue_body_reuses_original_issue(self):
        body = """
GitHub Issue #220: Mailbox: accept all commands without an approval step

Implement the mailbox change and preserve the existing task lifecycle.

Example from the VM

The controller created GitHub Issue #390 and linked experiment 56747fa9 to it.
The original work remains tracked in issue #220.
"""

        self.assertEqual(parse_work_directives(body).issue_number, 220)

    def test_later_issue_mention_does_not_override_header(self):
        directives = parse_work_directives(
            "GitHub Issue #220: Accept all commands\n\n"
            "This follows the mailbox groundwork discussed in issue #196."
        )

        self.assertEqual(directives.issue_number, 220)

    def test_plain_text_without_header_or_target_stays_unset(self):
        directives = parse_work_directives(
            "Review the mailbox behavior described in issue #196."
        )

        self.assertIsNone(directives.issue_number)

    def test_conflicting_explicit_target_remains_ambiguous(self):
        directives = parse_work_directives(
            "GitHub Issue #220: Accept all commands\n\nImplement issue #196 instead."
        )

        self.assertIsNone(directives.issue_number)


if __name__ == "__main__":
    unittest.main()
