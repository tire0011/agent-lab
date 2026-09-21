from __future__ import annotations

import copy
import unittest

from modules.controller_observations import controller_observation_view


class ControllerObservationViewTests(unittest.TestCase):
    def test_operation_view_removes_execution_metadata_and_preserves_raw_input(self):
        raw = [{
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": 231},
            "grant_event_id": 12,
            "operation_schema": {"operation": "github_issue"},
            "metrics": {"resolver_ms": 4},
            "result": {
                "success": True,
                "output_lines": ["title: Refactor controller observations"],
                "exit_code": 0,
            },
            "event_id": 34,
        }]
        original = copy.deepcopy(raw)

        viewed = controller_observation_view(raw)

        self.assertEqual(raw, original)
        self.assertNotIn("grant_event_id", viewed[0])
        self.assertNotIn("operation_schema", viewed[0])
        self.assertNotIn("metrics", viewed[0])
        self.assertNotIn("result", viewed[0])
        self.assertEqual(viewed[0]["status"], "succeeded")
        self.assertEqual(
            viewed[0]["output_lines"],
            ["title: Refactor controller observations"],
        )

    def test_failed_operation_keeps_failure_semantics(self):
        viewed = controller_observation_view([{
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": 231},
            "result": {
                "success": False,
                "exit_code": 1,
                "error_summary": "temporary failure",
            },
        }])

        self.assertEqual(viewed[0]["status"], "failed")
        self.assertEqual(viewed[0]["exit_code"], 1)
        self.assertEqual(viewed[0]["error_summary"], "temporary failure")

    def test_failure_is_removed_when_same_call_succeeds_later(self):
        observations = [
            {
                "tool": "operation",
                "operation": "github_issue",
                "args": {"issue_number": "231"},
                "result": {"success": False, "error_summary": "bad input"},
            },
            {
                "tool": "operation",
                "operation": "github_issue",
                "args": {"issue_number": 231},
                "result": {"success": True, "output_lines": ["issue body"]},
                "event_id": 88,
            },
        ]

        viewed = controller_observation_view(observations)

        self.assertEqual(len(viewed), 1)
        self.assertEqual(viewed[0]["status"], "succeeded")
        self.assertEqual(viewed[0]["event_id"], 88)

    def test_duplicate_refusal_points_to_prior_success(self):
        observations = [
            {
                "tool": "operation",
                "operation": "github_issue",
                "args": {"issue_number": 231},
                "result": {"success": True, "output_lines": ["issue body"]},
                "event_id": 91,
            },
            {
                "tool": "operation",
                "operation": "github_issue",
                "args": {"issue_number": "231"},
                "event": "duplicate_tool_call",
                "recovery": "choose another action",
            },
        ]

        viewed = controller_observation_view(observations)

        self.assertEqual(viewed[1]["already_succeeded_in_event"], 91)
        self.assertIn("event 91", viewed[1]["recovery"])

    def test_found_tool_drops_consumed_schema_but_non_operation_tools_pass_through(self):
        find_tool = {
            "tool": "find_tool",
            "status": "found",
            "operation": {"operation": "github_issue"},
            "arguments_schema": {"issue_number": {"type": "integer"}},
            "resolved_arguments": {"issue_number": 231},
            "grant_event_id": 1,
        }
        memory = {
            "tool": "memory_search",
            "query": "controller observations",
            "results": [{"content": "keep me"}],
        }

        viewed = controller_observation_view([find_tool, memory])

        self.assertNotIn("operation", viewed[0])
        self.assertNotIn("arguments_schema", viewed[0])
        self.assertNotIn("grant_event_id", viewed[0])
        self.assertEqual(viewed[1], memory)

    def test_chunked_output_is_reassembled_for_controller(self):
        viewed = controller_observation_view([{
            "tool": "operation",
            "operation": "github_issue",
            "args": {"issue_number": 231},
            "result": {
                "success": True,
                "output_lines": [
                    "body[1]: first ",
                    "body[2]: second",
                    "state: open",
                ],
            },
        }])

        self.assertEqual(
            viewed[0]["output_lines"],
            ["body: first second", "state: open"],
        )


if __name__ == "__main__":
    unittest.main()
