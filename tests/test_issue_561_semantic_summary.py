"""#561: a semantic summary between the full tool result and the bare stub."""

from __future__ import annotations

import json
import unittest

from modules.controller_observations import controller_observation_view
from modules.observation_summary import summarize_observation
from modules.prompt_budget import compact_observations


def _size(value) -> int:
    return len(json.dumps(value, separators=(",", ":")))


def _large_issue(event_id: int = 158, extra_criteria: int = 0) -> dict:
    body = (
        "## Problem\n"
        "When a tool result is too large the controller only sees a stub.\n\n"
        "## Change\n" + ("Lots of design discussion. " * 300) + "\n\n"
        "## Acceptance criteria\n"
        "- A large github_issue result keeps title, state and criteria.\n"
        "- A large command output keeps the exit code and last lines.\n"
        + "".join(f"- Criterion {i}: " + "c" * 60 + "\n" for i in range(extra_criteria))
    )
    metadata = {
        "number": 561,
        "title": "Large tool results, part 1",
        "state": "open",
        "labels": [{"name": "nexus"}, "enhancement"],
        "url": "https://example.invalid/561",
    }
    lines = [f"issue: {json.dumps(metadata, sort_keys=True)}"]
    for index in range(0, len(body), 450):
        lines.append(f"body[{index // 450 + 1}]: {body[index:index + 450]}")
    return {
        "tool": "operation",
        "operation": "github_issue",
        "args": {"issue_number": 561},
        "grant_event_id": 3,
        "operation_schema": {"name": "github_issue"},
        "result": {
            "operation": "github_issue",
            "success": True,
            "output_lines": lines,
            "exit_code": 0,
        },
        "event_id": event_id,
    }


def _large_command_output() -> dict:
    lines = ["**Exit code:** 1", "**Duration:** 12.0s", "", "### stdout", "", "```text"]
    lines += [f"tests/test_x.py::test_{i} PASSED" for i in range(400)]
    lines += [
        "FAILED tests/test_y.py::test_broken - AssertionError",
        "ERROR: test_other (tests.test_z.Case)",
        "=== 1 failed, 400 passed in 12.0s ===",
        "```",
    ]
    return {
        "tool": "command",
        "command": "python -m pytest -q",
        "result": "\n".join(lines),
        "event_id": 77,
    }


class LargeGithubIssueTests(unittest.TestCase):
    BUDGET = 2500

    def setUp(self):
        [self.view] = controller_observation_view([_large_issue()])
        self.assertGreater(_size(self.view), self.BUDGET)
        compacted, self.omitted = compact_observations(
            [self.view], budget_chars=self.BUDGET, keep_recent_full=2
        )
        [self.item] = compacted

    def test_it_becomes_a_summary_not_a_stub(self):
        self.assertEqual(self.omitted, [])
        self.assertTrue(self.item["summarized"])
        self.assertNotIn("truncated", self.item)
        self.assertLessEqual(_size(self.item), self.BUDGET)

    def test_title_state_and_acceptance_criteria_survive(self):
        summary = self.item["summary"]
        self.assertEqual(summary["number"], 561)
        self.assertEqual(summary["title"], "Large tool results, part 1")
        self.assertEqual(summary["state"], "open")
        self.assertEqual(summary["labels"], ["nexus", "enhancement"])
        self.assertIn("only sees a stub", summary["goal"])
        self.assertIn("keeps title, state and criteria", summary["acceptance_criteria"])
        self.assertIn("exit code and last lines", summary["acceptance_criteria"])

    def test_identity_and_event_reference_are_kept(self):
        self.assertEqual(self.item["operation"], "github_issue")
        self.assertEqual(self.item["args"], {"issue_number": 561})
        self.assertEqual(self.item["status"], "succeeded")
        self.assertEqual(self.item["event_id"], 158)
        self.assertIn("158", self.item["note"])

    def test_the_bulky_design_discussion_is_not_carried(self):
        self.assertNotIn("Lots of design discussion. Lots", json.dumps(self.item))


class LargeCommandOutputTests(unittest.TestCase):
    BUDGET = 2000

    def test_exit_code_last_lines_and_failing_tests_survive(self):
        obs = _large_command_output()
        self.assertGreater(_size(obs), self.BUDGET)
        [item], omitted = compact_observations([obs], budget_chars=self.BUDGET)
        self.assertEqual(omitted, [])
        self.assertLessEqual(_size(item), self.BUDGET)
        summary = item["summary"]
        self.assertEqual(summary["exit_code"], 1)
        self.assertIn("=== 1 failed, 400 passed in 12.0s ===", summary["last_lines"])
        self.assertIn("tests/test_y.py::test_broken - AssertionError", summary["failing_tests"])
        self.assertIn("test_other (tests.test_z.Case)", summary["failing_tests"])
        self.assertEqual(item["command"], "python -m pytest -q")
        self.assertEqual(item["event_id"], 77)

    def test_an_operation_result_keeps_its_own_exit_code(self):
        obs = {
            "tool": "operation",
            "operation": "service_logs",
            "args": {"unit": "nexus"},
            "status": "failed",
            "exit_code": 3,
            "output_lines": [f"log line {i} " + "x" * 50 for i in range(200)],
            "event_id": 5,
        }
        [item], _ = compact_observations([obs], budget_chars=1500)
        self.assertEqual(item["summary"]["exit_code"], 3)
        self.assertEqual(item["summary"]["last_lines"][-1], obs["output_lines"][-1])
        self.assertEqual(item["status"], "failed")


class ListResultTests(unittest.TestCase):
    def test_a_search_keeps_the_count_and_the_first_items(self):
        obs = {
            "tool": "memory_search",
            "query": "prior work",
            "results": [{"source": "doc", "content": f"hit {i} " + "z" * 400} for i in range(40)],
            "event_id": 9,
        }
        [item], omitted = compact_observations([obs], budget_chars=3000)
        self.assertEqual(omitted, [])
        self.assertEqual(item["summary"]["count"], 40)
        self.assertIn("hit 0 ", item["summary"]["first_results"][0])
        self.assertLess(len(item["summary"]["first_results"]), 40)


class SummaryDoesNotFitTests(unittest.TestCase):
    def test_falls_back_to_the_stub_with_operation_args_status_and_event(self):
        [view] = controller_observation_view([_large_issue(event_id=42)])
        self.assertIsNone(summarize_observation(view, 250))
        [stub], omitted = compact_observations([view], budget_chars=250)
        self.assertEqual(omitted, [0])
        self.assertTrue(stub["truncated"])
        self.assertNotIn("summarized", stub)
        self.assertEqual(stub["operation"], "github_issue")
        self.assertEqual(stub["args"], {"issue_number": 561})
        self.assertEqual(stub["status"], "succeeded")
        self.assertEqual(stub["event_id"], 42)

    def test_a_result_without_summarisable_content_is_stubbed(self):
        self.assertIsNone(summarize_observation({"tool": "delegate", "advice": 1}, 5000))


class PassThroughTests(unittest.TestCase):
    def test_a_result_that_fits_is_passed_through_unchanged(self):
        [view] = controller_observation_view([_large_issue()])
        compacted, omitted = compact_observations([view], budget_chars=_size(view) + 10)
        self.assertEqual(omitted, [])
        self.assertEqual(compacted, [view])
        self.assertIs(compacted[0], view)

    def test_the_input_is_not_mutated_when_summarised(self):
        obs = _large_command_output()
        before = json.dumps(obs, sort_keys=True)
        compact_observations([obs], budget_chars=1500)
        self.assertEqual(json.dumps(obs, sort_keys=True), before)


class DeterminismTests(unittest.TestCase):
    def test_the_same_input_gives_the_same_summary(self):
        [view] = controller_observation_view([_large_issue()])
        self.assertEqual(summarize_observation(view, 1200), summarize_observation(view, 1200))

    def test_a_tighter_budget_shrinks_the_summary_instead_of_skipping_it(self):
        [view] = controller_observation_view([_large_issue(extra_criteria=40)])
        roomy = summarize_observation(view, 5000)
        tight = summarize_observation(view, 900)
        self.assertIsNotNone(tight)
        self.assertLessEqual(_size(tight), 900)
        self.assertLess(_size(tight), _size(roomy))
        self.assertEqual(tight["summary"]["title"], "Large tool results, part 1")


if __name__ == "__main__":
    unittest.main()
