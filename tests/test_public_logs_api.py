"""Focused tests for GET /logs/public, the unauthenticated equivalent of
GET /logs used by the Logs tab before an API key has been entered.

Uses the same mocked-service pattern as test_unified_logs_api.py, but
asserts the strict public allowlist (no command/cwd/stdout/stderr/
exception/stacktrace/job_id/session_id) instead of the full one.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules.central_logging import ExperimentLogger, get_task_logger
from modules.experiment_service import ExperimentNotFoundError

SECRET = "abcdef0123456789abcdef0123456789"


class PublicUnifiedLogsApiTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.log_dir = Path(self._tmpdir.name)

        self.experiments = {
            "aa" * 16: SimpleNamespace(id="aa" * 16, status="running"),
            "bb" * 16: SimpleNamespace(id="bb" * 16, status="succeeded"),
        }

        self.service = MagicMock()
        self.service.experiment_log_dir = self.log_dir
        self.service.resolve_experiment.side_effect = self._resolve_experiment
        self.service.mailbox_task_id_for_experiment.return_value = None

        codex = MagicMock()
        mailbox = MagicMock()
        mailbox.recover_after_restart.return_value = None
        api.app.dependency_overrides[api.get_experiment_service] = lambda: self.service
        api.app.dependency_overrides[api.get_codex_service] = lambda: codex
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        self.client = TestClient(api.app)
        self.addCleanup(api.app.dependency_overrides.clear)

    def _resolve_experiment(self, experiment_id: str):
        for exp in self.experiments.values():
            if exp.id == experiment_id or exp.id.startswith(experiment_id):
                return exp
        raise ExperimentNotFoundError("no such experiment")

    def _write_logs(self):
        elog_a = ExperimentLogger("aa" * 16, mailbox_task_id="task-aa", log_dir=self.log_dir)
        elog_b = ExperimentLogger("bb" * 16, mailbox_task_id="task-bb", log_dir=self.log_dir)
        elog_a.info("queued", "task_bound", "task bound to new experiment")
        time.sleep(0.005)
        elog_a.info("implementation", "implementation_started", "starting implementation")
        time.sleep(0.005)
        elog_b.info("verifying", "verification_started", "running tests")
        time.sleep(0.005)
        elog_b.log_process(
            "verifying", "tests_completed", "tests finished",
            command=["python", "-m", "pytest"], exit_code=1, stderr=f"token={SECRET}",
        )
        time.sleep(0.005)
        elog_a.error("implementation", "codex_failed", "implementation raised")

    def test_does_not_require_api_key(self):
        response = self.client.get("/logs/public")
        self.assertEqual(response.status_code, 200)

    def test_no_log_files_returns_empty_entries(self):
        response = self.client.get("/logs/public")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["entries"], [])

    def test_aggregates_entries_across_experiments_chronologically(self):
        self._write_logs()
        response = self.client.get("/logs/public")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 5)
        events = [e["event"] for e in body["entries"]]
        self.assertEqual(
            events,
            [
                "task_bound",
                "implementation_started",
                "verification_started",
                "tests_completed",
                "codex_failed",
            ],
        )

    def test_pre_experiment_task_log_entries_appear_in_public_feed(self):
        get_task_logger("task-zz", log_dir=self.log_dir).info(
            "mailbox", "task_received", "mailbox task received"
        )
        response = self.client.get("/logs/public")
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertIsNone(body["entries"][0]["experiment_id"])
        self.assertEqual(body["entries"][0]["mailbox_task_id"], "task-zz")

    def test_filters_by_experiment_id(self):
        self._write_logs()
        response = self.client.get(f"/logs/public?experiment_id={'aa' * 16}")
        body = response.json()
        self.assertEqual(body["count"], 3)
        self.assertTrue(all(e["experiment_id"] == "aa" * 16 for e in body["entries"]))

    def test_unknown_experiment_id_returns_empty_entries_not_error(self):
        self._write_logs()
        response = self.client.get("/logs/public?experiment_id=deadbeefdeadbeef")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_filters_by_mailbox_task_id(self):
        self._write_logs()
        response = self.client.get("/logs/public?mailbox_task_id=task-bb")
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertTrue(all(e["mailbox_task_id"] == "task-bb" for e in body["entries"]))

    def test_filters_by_level(self):
        self._write_logs()
        response = self.client.get("/logs/public?level=ERROR")
        body = response.json()
        self.assertEqual(
            {e["event"] for e in body["entries"]}, {"tests_completed", "codex_failed"}
        )

    def test_filters_by_phase(self):
        self._write_logs()
        response = self.client.get("/logs/public?phase=implementation")
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertTrue(all(e["phase"] == "implementation" for e in body["entries"]))

    def test_rejects_invalid_level_filter(self):
        response = self.client.get("/logs/public?level=NOTALEVEL")
        self.assertEqual(response.status_code, 400)

    def test_response_never_leaks_secrets(self):
        self._write_logs()
        response = self.client.get("/logs/public")
        serialized = json.dumps(response.json())
        self.assertNotIn(SECRET, serialized)

    def test_response_excludes_internal_fields(self):
        # The public feed must never expose command/cwd/stdout/stderr/
        # exception/stacktrace/job_id/session_id, unlike the authenticated
        # /logs feed - only the coarse allowlisted fields.
        self._write_logs()
        response = self.client.get("/logs/public")
        entry = response.json()["entries"][0]
        expected_fields = {
            "timestamp", "level", "phase", "event", "message",
            "experiment_id", "mailbox_task_id", "truncated",
        }
        self.assertEqual(set(entry.keys()), expected_fields)
        serialized = json.dumps(response.json())
        for forbidden in ("stdout", "stderr", "stacktrace", "exception_type", "job_id", "session_id"):
            self.assertNotIn(forbidden, serialized)

    def test_missing_log_directory_is_distinct_from_empty_feed(self):
        self.service.experiment_log_dir = self.log_dir / "missing"
        response = self.client.get("/logs/public")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "log storage is unreadable")

    def test_cursor_pagination_returns_older_entries_without_duplicates(self):
        self._write_logs()
        first = self.client.get("/logs/public?limit=3").json()
        self.assertEqual(len(first["entries"]), 3)
        self.assertIsNotNone(first["next_cursor"])

        second = self.client.get(
            f"/logs/public?limit=3&cursor={first['next_cursor']}"
        ).json()
        first_events = [e["event"] for e in first["entries"]]
        second_events = [e["event"] for e in second["entries"]]
        self.assertEqual(len(set(first_events) & set(second_events)), 0)


if __name__ == "__main__":
    unittest.main()
