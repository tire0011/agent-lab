"""Every module must import on its own, in any order.

`operations_registry` imports `mailbox_processor` at module scope, which
reaches `modules.mailbox` -> failure analysis -> `nexus_tool_router`, and that
router needs `OperationValidationError` and `normalize_integer_argument` back.
Importing the registry first happened to work; importing the router first
found the registry half-built and raised

    cannot import name 'OperationValidationError' from partially initialized
    module 'modules.operations_registry'

which is what `pytest tests/test_task_orchestrator.py` on its own did. The
whole file was uncollectable, so a developer running one test file got an
import traceback instead of a result, and the same run inside a larger
selection passed - the worst shape a failure can take.

Each import runs in its own interpreter: a cycle is about what happens on a
cold import, and by the time this test module is running, everything is
already in `sys.modules`.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Both sides of the cycle, plus the two entry points that pull the whole graph.
_MODULES = (
    "modules.operation_contract",
    "modules.operations_registry",
    "modules.nexus_tool_router",
    "modules.failure_analysis_service",
    "modules.task_orchestrator",
)


def _import_alone(module: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


class ImportCycleTests(unittest.TestCase):
    def test_each_module_imports_as_the_first_import(self):
        for module in _MODULES:
            with self.subTest(module=module):
                result = _import_alone(module)
                self.assertEqual(
                    result.returncode,
                    0,
                    f"importing {module} first failed:\n{result.stderr[-2000:]}",
                )

    def test_the_contract_module_pulls_in_nothing_of_the_project(self):
        """It can only sit under both sides of the cycle while it stays a leaf."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import modules.operation_contract; "
                "print([m for m in sys.modules "
                "if m.startswith('modules.') and m != 'modules.operation_contract'])",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip(), "[]", result.stdout)


class ReExportTests(unittest.TestCase):
    """The split must not move anyone's import path."""

    def test_the_exception_is_one_class_not_two(self):
        """Two definitions would make `except` succeed or fail by import path."""
        from modules.operation_contract import OperationValidationError as FromLeaf
        from modules.operations_registry import OperationValidationError as FromRegistry

        self.assertIs(FromLeaf, FromRegistry)
        self.assertTrue(issubclass(FromLeaf, ValueError))


if __name__ == "__main__":
    unittest.main()
