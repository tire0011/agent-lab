from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from modules.tool_resolver_evaluation import (
    EVALUATION_CASES,
    evaluate_all,
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
        self.assertTrue(
            any(case.id == "git-branch-protection" for case in EVALUATION_CASES)
        )

    def test_summary_reports_aggregate_and_per_category_accuracy(self):
        report = _summarize(
            mode="deterministic",
            cases=[
                {
                    "expected_status": "found",
                    "expected_operation": "git_status",
                    "expected_operation_in_candidates": True,
                    "selection_correctness": "correct",
                    "status": "found",
                    "resolver_invoked": True,
                },
                {
                    "expected_status": "found",
                    "expected_operation": "git_log",
                    "expected_operation_in_candidates": True,
                    "selection_correctness": "incorrect",
                    "status": "found",
                    "resolver_invoked": True,
                },
                {
                    "expected_status": "not_found",
                    "selection_correctness": "correct",
                    "status": "not_found",
                    "resolver_invoked": False,
                },
                {
                    "expected_status": "not_found",
                    "selection_correctness": "unknown",
                    "status": "unavailable",
                    "candidate_count": 2,
                    "resolver_invoked": True,
                },
            ],
        )

        self.assertEqual(report["summary"]["total_cases"], 4)
        self.assertEqual(report["summary"]["correct"], 2)
        self.assertEqual(report["summary"]["incorrect"], 1)
        self.assertIsNone(report["summary"]["accuracy"])
        self.assertIsNone(report["summary"]["tool_selection_accuracy"])
        self.assertIsNone(report["summary"]["not_found_accuracy"])
        self.assertEqual(report["summary"]["candidate_expected_present_rate"], 1.0)
        self.assertEqual(report["summary"]["negative_cases_with_candidates"], 1)

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
        self.assertIsNone(result["summary"]["llm_selection_accuracy"])
        self.assertGreater(result["summary"]["candidate_expected_present_rate"], 0)
        self.assertGreaterEqual(result["summary"]["negative_cases_with_candidates"], 1)
        self.assertTrue(
            all(case["selection_correctness"] != "unknown" for case in result["cases"])
        )
        self.assertTrue(
            all(
                "expected_status" in case and "selected_operation" in case
                and "candidate_operations" in case
                for case in result["cases"]
            )
        )

    def test_all_mode_separates_local_llm_from_deterministic_candidate_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(
                evaluate_all(
                    repo_root=Path(__file__).resolve().parents[1],
                    db_path=Path(tmp) / "eval.db",
                )
            )

        self.assertEqual(result["mode"], "all")
        self.assertIn("local_llm_evaluation", result)
        self.assertIn("deterministic_candidate_check", result)
        self.assertIsNone(
            result["deterministic_candidate_check"]["summary"]["llm_selection_accuracy"]
        )
        self.assertGreaterEqual(
            result["deterministic_candidate_check"]["summary"]["negative_cases_with_candidates"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
