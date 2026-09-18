from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.nexus_tool_router import bind_resolved_arguments
from modules.operations_registry import OperationArgSpec, OperationValidationError, OperationsRegistry


class _IssueService:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def get_issue(self, issue_number: int):
        self.calls.append(issue_number)
        return {
            "number": issue_number,
            "title": "Issue",
            "body": "",
            "state": "open",
            "url": f"https://github.com/tire0011/agent-lab/issues/{issue_number}",
        }

    def close(self) -> None:
        pass


class IntegerArgumentCoercionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.issue_service = _IssueService()
        self.registry = OperationsRegistry(
            experiment_service=SimpleNamespace(),
            db_path=str(Path(self.tmp.name) / "agent.db"),
            repo_root=self.tmp.name,
            github_work_tracking_factory=lambda **_kwargs: self.issue_service,
        )
        self.addCleanup(self.registry.close)

    def test_integer_argument_remains_integer(self):
        result = self.registry.execute("github_issue", {"issue_number": 196})

        self.assertTrue(result.success)
        self.assertEqual(self.issue_service.calls, [196])
        self.assertIsInstance(self.issue_service.calls[0], int)

    def test_numeric_string_is_coerced_before_operation_execution(self):
        result = self.registry.execute("github_issue", {"issue_number": "196"})

        self.assertTrue(result.success)
        self.assertEqual(self.issue_service.calls, [196])
        self.assertIsInstance(self.issue_service.calls[0], int)

    def test_surrounding_whitespace_is_accepted(self):
        self.registry.execute("github_issue", {"issue_number": " 196 "})

        self.assertEqual(self.issue_service.calls, [196])

    def test_minimum_and_maximum_apply_after_coercion(self):
        with self.assertRaises(OperationValidationError):
            self.registry.execute("github_issue", {"issue_number": "0"})
        with self.assertRaises(OperationValidationError):
            self.registry._normalize_args("git_log", {"limit": "101"})

        self.assertEqual(self.issue_service.calls, [])

    def test_invalid_integer_strings_are_rejected(self):
        for value in ("abc", "1.5", "196x", "", "+-5", "--5"):
            with self.subTest(value=value), self.assertRaises(OperationValidationError):
                self.registry.execute("github_issue", {"issue_number": value})

        self.assertEqual(self.issue_service.calls, [])

    def test_extremely_long_integer_string_is_validation_error(self):
        with self.assertRaises(OperationValidationError):
            self.registry.execute("github_issue", {"issue_number": "1" * 5000})

        self.assertEqual(self.issue_service.calls, [])

    def test_float_and_bool_are_not_coerced_to_integer(self):
        for value in (196.0, True):
            with self.subTest(value=value), self.assertRaises(OperationValidationError):
                self.registry.execute("github_issue", {"issue_number": value})

        self.assertEqual(self.issue_service.calls, [])

    def test_unrelated_argument_types_keep_existing_strict_validation(self):
        with self.assertRaises(OperationValidationError):
            self.registry.execute("nexus_status", {"reference": 196})

    def test_resolver_path_rejects_ambiguous_signs_without_raising(self):
        spec = SimpleNamespace(arguments=[
            OperationArgSpec("issue_number", "integer", "GitHub issue.", required=True, minimum=1)
        ])

        for value in ("+-5", "--5", "1" * 5000):
            with self.subTest(value=value):
                bound, error = bind_resolved_arguments(
                    "read github issue 5",
                    spec,
                    {"issue_number": value},
                )
                self.assertIsNone(bound)
                self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
