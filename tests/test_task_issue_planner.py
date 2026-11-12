import json
import unittest

from modules.task_issue_planner import plan_issue


VALID_BODY = """## Goal
Implement the requested change.

## Current state
The placeholder is used today.

## Plan
Add and test the planner.

## Acceptance criteria
- A valid issue draft is returned.
"""


class FakeProvider:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.answer


class TaskIssuePlannerTests(unittest.TestCase):
    def test_unusable_answer_makes_exactly_one_call(self):
        first = FakeProvider("not JSON")
        unused_fallback = FakeProvider(json.dumps({"title": "wrong", "body": VALID_BODY}))

        self.assertIsNone(plan_issue("change it", {"state": "known"}, first))

        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(unused_fallback.calls), 0)

    def test_missing_required_section_is_rejected(self):
        provider = FakeProvider(json.dumps({
            "title": "A useful title",
            "body": VALID_BODY.replace("## Plan", "## Steps"),
        }))

        self.assertIsNone(plan_issue("change it", "facts", provider))
        self.assertEqual(len(provider.calls), 1)

    def test_valid_answer_is_parsed(self):
        provider = FakeProvider("```json\n" + json.dumps({
            "title": "  Plan a mailbox task  ",
            "body": VALID_BODY,
        }) + "\n```")

        self.assertEqual(
            plan_issue("change it", ["one fact"], provider),
            {"title": "Plan a mailbox task", "body": VALID_BODY.strip()},
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("one fact", provider.calls[0])

    def test_unavailable_or_exhausted_provider_is_not_called(self):
        unavailable = FakeProvider("unused")
        unavailable.available = False
        exhausted = FakeProvider("unused")
        exhausted.budget_exhausted = True

        self.assertIsNone(plan_issue("change it", {}, unavailable))
        self.assertIsNone(plan_issue("change it", {}, exhausted))
        self.assertEqual(unavailable.calls, [])
        self.assertEqual(exhausted.calls, [])


if __name__ == "__main__":
    unittest.main()
