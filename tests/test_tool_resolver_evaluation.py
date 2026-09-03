from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from modules.tool_resolver_evaluation import (
    EVALUATION_CASES,
    _summarize,
    evaluate_tool_resolver,
)


class ToolResolverEvaluationTests(unittest.TestCase):
    def test_evaluation_matrix_includes_found_and_not_found_ground_truth(self):
        expected_operations = {
            case.expected_operation
            for case in EVALUATION_CASES
            if case.expected_operation
        }
        expected_statuses = {case.expected_status for case in EVALUATION_CASES}

        self.assertIn("git_status", expected_operations)
        self.assertIn("local_model_status", expected_operations)
        self.assertIn("not_found", expected_statuses)

    def test_summary_reports_aggregate_and_per_category_accuracy(self):
        report = _summarize(
            mode="deterministic",
            cases=[
                {
                    "expected_status": "found",
                    "selection_correctness": "correct",
                    "status": "found",
                },
                {
                    "expected_status": "found",
                    "selection_correctness": "incorrect",
                    "status": "found",
                },
                {
                    "expected_status": "not_found",
                    "selection_correctness": "correct",
                    "status": "not_found",
                },
            ],
        )

        self.assertEqual(report["summary"]["total_cases"], 3)
        self.assertEqual(report["summary"]["correct"], 2)
        self.assertEqual(report["summary"]["incorrect"], 1)
        self.assertEqual(report["summary"]["accuracy"], 2 / 3)
        self.assertEqual(report["summary"]["tool_selection_accuracy"], 1 / 2)
        self.assertEqual(report["summary"]["not_found_accuracy"], 1.0)

    def test_deterministic_evaluation_records_selected_vs_expected_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(
                evaluate_tool_resolver(
                    repo_root=Path(__file__).resolve().parents[1],
                    db_path=Path(tmp) / "eval.db",
                    mode="deterministic",
                )
            )

        self.assertEqual(result["summary"]["total_cases"], len(EVALUATION_CASES))
        self.assertGreater(result["summary"]["tool_selection_accuracy"], 0)
        self.assertIsNotNone(result["summary"]["not_found_accuracy"])
        self.assertTrue(
            all(case["selection_correctness"] != "unknown" for case in result["cases"])
        )
        self.assertTrue(
            all(
                "expected_status" in case and "selected_operation" in case
                for case in result["cases"]
            )
        )


if __name__ == "__main__":
    unittest.main()
