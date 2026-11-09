"""A blocking review names every problem at once, not only the first.

#598 and #511 each ran out of attempts because every review found one new
edge case; the retry fixed that one and the next review found the next.
"""

from __future__ import annotations

import json
import unittest

from modules.nexus_reviewer import parse_review, review_prompt


class ReviewListsAllProblemsTests(unittest.TestCase):
    def test_the_prompt_asks_for_every_blocking_problem(self):
        prompt = review_prompt("task", "diff", can_read_files=True)
        self.assertIn("list EVERY blocking problem", prompt)
        self.assertIn('"problems"', prompt)

    def test_all_problems_reach_the_block_reason(self):
        raw = json.dumps({
            "verdict": "block", "summary": "Adds gate rechecks.",
            "problems": ["link is stored only after handle_issue returns", "a failed start counts as an attempt"],
        })
        verdict, summary = parse_review(raw)
        self.assertEqual(verdict, "block")
        self.assertIn("(1) link is stored only after handle_issue returns", summary)
        self.assertIn("(2) a failed start counts as an attempt", summary)

    def test_an_approval_or_an_old_style_answer_is_unchanged(self):
        self.assertEqual(parse_review('{"verdict": "approve", "summary": "fine", "problems": []}'),
                         ("approve", "fine"))
        self.assertEqual(parse_review('{"verdict": "block", "summary": "bug in x"}'), ("block", "bug in x"))


class FullReasonIsKeptTests(unittest.TestCase):
    def test_a_long_review_reason_is_stored_whole(self):
        # It was cut at 500 chars, so the retry saw only half a finding.
        from unittest.mock import MagicMock

        from modules.experiment_adoption_service import ExperimentAdoptionService

        service = ExperimentAdoptionService.__new__(ExperimentAdoptionService)
        service._set_status = MagicMock()
        service._report_issue_progress = MagicMock()
        service.get = MagicMock()
        reason = "Review by codex blocked the merge: " + "x" * 2000 + " (2) the last problem"
        service._finish_failure("exp", reason=reason)
        self.assertTrue(service._set_status.call_args.kwargs["reason"].endswith("(2) the last problem"))


if __name__ == "__main__":
    unittest.main()
