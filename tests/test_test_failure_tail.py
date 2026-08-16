"""Regression tests for the bounded, redacted test-failure tail feature.

Covers:
- Failed traceback tail is visible and final assertion/error lines survive.
- Output is tail-bounded (max 30 lines) and size-bounded (max 4000 chars).
- Secrets and sensitive paths are redacted.
- ``experiment_show`` operation contains the bounded diagnostic tail.
- Successful / no-output experiments do not produce a failure block.
- Authenticated ``GET /experiments/{id}`` exposes ``test_failure_tail``.
- UI ``showExperimentDetail`` uses ``textContent`` only (no innerHTML).
"""

from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules import redaction
from tests.test_activity_api import SECRET, experiment


# ---------------------------------------------------------------------------
# Unit tests: redaction.test_failure_tail
# ---------------------------------------------------------------------------

class TestFailureTailRedactionUnit(unittest.TestCase):
    """Unit tests for ``redaction.test_failure_tail``."""

    def test_none_or_empty_returns_empty_list(self):
        self.assertEqual(redaction.test_failure_tail(None), [])
        self.assertEqual(redaction.test_failure_tail(""), [])
        self.assertEqual(redaction.test_failure_tail("   \n  "), [])

    def test_final_lines_of_traceback_survive(self):
        output = "\n".join([
            "Traceback (most recent call last):",
            '  File "tests/test_foo.py", line 42, in test_bar',
            "    self.assertEqual(1, 2)",
            "AssertionError: 1 != 2",
        ])
        tail = redaction.test_failure_tail(output)
        self.assertTrue(any("AssertionError" in line for line in tail))
        self.assertGreater(len(tail), 0)

    def test_bounded_to_30_lines(self):
        output = "\n".join(f"line {i}" for i in range(60))
        tail = redaction.test_failure_tail(output)
        self.assertLessEqual(len(tail), 30)

    def test_size_bounded_at_4000_chars(self):
        long_line = "A" * 600  # each line > 500 chars but redact truncates to 500
        output = "\n".join([long_line] * 40)
        tail = redaction.test_failure_tail(output)
        total = sum(len(line) + 1 for line in tail)
        self.assertLessEqual(total, 4002)  # small slack for edge of last line

    def test_secrets_redacted(self):
        output = f"api_key=supersecret\ntoken=abc123\nAssertionError: bad"
        tail = redaction.test_failure_tail(output)
        serialized = json.dumps(tail)
        self.assertNotIn("supersecret", serialized)
        self.assertNotIn("abc123", serialized)

    def test_absolute_paths_redacted(self):
        output = "/agent/modules/worker.py:42: AssertionError"
        tail = redaction.test_failure_tail(output)
        self.assertNotIn("/agent", json.dumps(tail))

    def test_tail_preserves_only_final_lines(self):
        lines = [f"early line {i}" for i in range(50)] + ["FINAL AssertionError"]
        output = "\n".join(lines)
        tail = redaction.test_failure_tail(output)
        self.assertTrue(any("FINAL AssertionError" in line for line in tail))
        self.assertFalse(any("early line 0" in line for line in tail))

    def test_long_lines_end_marker_always_present(self):
        """With 30 lines each near 500 chars, the last line's marker must survive."""
        long_lines = [("X" * 490 + f" line{i:02d}") for i in range(29)]
        long_lines.append("END_MARKER_UNIQUE_LAST_LINE")
        output = "\n".join(long_lines)
        tail = redaction.test_failure_tail(output)
        self.assertTrue(
            any("END_MARKER_UNIQUE_LAST_LINE" in line for line in tail),
            f"End marker not found; tail={tail}",
        )


# ---------------------------------------------------------------------------
# API: authenticated GET /experiments/{id} exposes test_failure_tail
# ---------------------------------------------------------------------------

class ExperimentDetailTailApiTests(unittest.TestCase):
    def setUp(self):
        self.traceback = (
            "Traceback (most recent call last):\n"
            '  File "tests/test_foo.py", line 10, in test_bar\n'
            "    self.assertTrue(False)\n"
            f"AssertionError: False is not true  api_key={SECRET}\n"
        )
        self.failed_exp = experiment(
            id="aabbccddeeff0011",
            status="failed",
            test_exit_code=1,
            test_output=self.traceback,
        )
        self.success_exp = experiment(
            id="1122334455667788",
            status="succeeded",
            test_exit_code=0,
            test_output="OK\n1 test passed",
        )
        self.service = MagicMock()
        adoption = MagicMock()
        adoption.status = "not_started"
        adoption.to_dict.return_value = {}

        adoption_service = MagicMock()
        adoption_service.get.return_value = adoption

        api.app.dependency_overrides[api.get_experiment_service] = lambda: self.service
        api.app.dependency_overrides[api.get_experiment_adoption_service] = lambda: adoption_service
        api.app.dependency_overrides[api.get_codex_service] = lambda: MagicMock()
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: MagicMock()
        self.client = TestClient(api.app)
        self.addCleanup(api.app.dependency_overrides.clear)

    def _get(self, exp):
        self.service.resolve_experiment.return_value = exp
        return self.client.get(
            f"/experiments/{exp.id}",
            headers={"x-api-key": "test-key"},
        )

    def test_failed_experiment_exposes_test_failure_tail(self):
        response = self._get(self.failed_exp)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("test_failure_tail", body)
        self.assertIsInstance(body["test_failure_tail"], list)
        self.assertGreater(len(body["test_failure_tail"]), 0)

    def test_failed_experiment_tail_contains_assertion_error(self):
        response = self._get(self.failed_exp)
        tail = response.json()["test_failure_tail"]
        self.assertTrue(any("AssertionError" in line for line in tail))

    def test_failed_experiment_tail_secrets_redacted(self):
        response = self._get(self.failed_exp)
        serialized = json.dumps(response.json()["test_failure_tail"])
        self.assertNotIn(SECRET, serialized)

    def test_successful_experiment_has_empty_tail(self):
        response = self._get(self.success_exp)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        tail = body.get("test_failure_tail", [])
        self.assertEqual(tail, [])

    def test_no_raw_test_output_exposed(self):
        response = self._get(self.failed_exp)
        # The test_failure_tail field itself must not contain the raw secret
        tail = response.json().get("test_failure_tail", [])
        self.assertNotIn(SECRET, json.dumps(tail))


# ---------------------------------------------------------------------------
# Operations registry: experiment_show contains bounded diagnostic tail
# ---------------------------------------------------------------------------

class ExperimentShowOperationTailTests(unittest.TestCase):
    def setUp(self):
        import subprocess
        import tempfile
        from pathlib import Path
        from modules.operations_registry import OperationsRegistry

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
        (repo / "README.md").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

        self.traceback = (
            "Traceback (most recent call last):\n"
            "  File 'tests/test_x.py', line 5\n"
            "    self.assertTrue(False)\n"
            f"AssertionError: False is not true  api_key={SECRET}\n"
        )
        self.failed_exp = SimpleNamespace(
            id="aabbccdd11223344",
            status="failed",
            created_at="2026-08-14T10:00:00Z",
            ended_at="2026-08-14T10:01:00Z",
            test_command=["python", "-m", "unittest"],
            test_exit_code=1,
            test_output=self.traceback,
            git_status=" M modules/x.py\n",
            error=None,
        )
        self.success_exp = SimpleNamespace(
            id="ccddee1122334455",
            status="succeeded",
            created_at="2026-08-14T10:00:00Z",
            ended_at="2026-08-14T10:01:00Z",
            test_command=["python", "-m", "unittest"],
            test_exit_code=0,
            test_output="OK\n1 test passed",
            git_status=" M modules/y.py\n",
            error=None,
        )
        log_path = root / "exp.jsonl"
        log_path.touch()

        exp_service = MagicMock()
        exp_service.experiment_log_path.return_value = log_path
        meminfo = root / "meminfo"
        meminfo.write_text(
            "MemTotal: 1000 kB\nMemFree: 500 kB\nMemAvailable: 700 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
        )

        def resolve(experiment_id):
            if experiment_id in (self.failed_exp.id, self.failed_exp.id[:8]):
                return self.failed_exp
            return self.success_exp

        exp_service.resolve_experiment.side_effect = resolve

        self.registry = OperationsRegistry(
            experiment_service=exp_service,
            db_path=str(root / "agent.db"),
            repo_root=str(repo),
            mailbox_lock_path=str(root / "mailbox.lock"),
            meminfo_path=str(meminfo),
        )
        self.addCleanup(self.registry.close)

    def test_failed_experiment_show_contains_tail(self):
        result = self.registry.execute("experiment_show", {"experiment_id": self.failed_exp.id[:8]})
        self.assertTrue(result.success)
        output = json.dumps(result.output_lines)
        self.assertIn("test_failure_tail", output)

    def test_failed_experiment_show_contains_assertion_error(self):
        result = self.registry.execute("experiment_show", {"experiment_id": self.failed_exp.id[:8]})
        output = json.dumps(result.output_lines)
        self.assertIn("AssertionError", output)

    def test_failed_experiment_show_secrets_redacted(self):
        result = self.registry.execute("experiment_show", {"experiment_id": self.failed_exp.id[:8]})
        output = json.dumps(result.output_lines)
        self.assertNotIn(SECRET, output)

    def test_successful_experiment_show_no_tail_section(self):
        result = self.registry.execute("experiment_show", {"experiment_id": self.success_exp.id[:8]})
        self.assertTrue(result.success)
        output = json.dumps(result.output_lines)
        self.assertNotIn("test_failure_tail", output)


# ---------------------------------------------------------------------------
# UI: showExperimentDetail uses textContent only (no innerHTML)
# ---------------------------------------------------------------------------

class UITextContentSafetyTests(unittest.TestCase):
    """Verify that ``static/app.js`` uses only ``textContent`` for failure tail."""

    def _load_js(self):
        import pathlib
        path = pathlib.Path(__file__).parent.parent / "static" / "app.js"
        return path.read_text(encoding="utf-8")

    def test_test_failure_section_uses_textcontent_not_innerhtml(self):
        js = self._load_js()
        # Find the test-failure-section block
        idx = js.find("test-failure-section")
        self.assertGreater(idx, 0, "test-failure-section not found in app.js")
        # Extract a reasonable window around the block
        block = js[idx:idx + 1000]
        self.assertIn("textContent", block)
        self.assertNotIn("innerHTML", block)

    def test_test_failure_pre_class_present(self):
        js = self._load_js()
        self.assertIn("test-failure-output", js)

    def test_test_failure_heading_is_testfehler(self):
        js = self._load_js()
        self.assertIn("Testfehler", js)


if __name__ == "__main__":
    unittest.main()
