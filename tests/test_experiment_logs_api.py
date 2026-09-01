"""Focused tests for GET /experiments/{experiment_id}/logs.

Uses a mocked ExperimentService (same pattern as test_activity_api.py) but
writes real JSONL files via modules.experiment_logger so the read path,
filters, and secret-scrubbing are exercised end to end.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules.experiment_logger import ExperimentLogger
from modules.experiment_service import ExperimentNotFoundError

SECRET = "abcdef0123456789abcdef0123456789"


class ExperimentLogsApiTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.log_dir = Path(self._tmpdir.name)

        self.experiment = SimpleNamespace(id="ab" * 16, status="succeeded")

        self.service = MagicMock()
        self.service.resolve_experiment.side_effect = self._resolve_experiment
        self.service.experiment_log_dir = self.log_dir
        self.service.experiment_log_path.side_effect = (
            lambda experiment_id: self.log_dir / f"{experiment_id}.jsonl"
        )
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
        if experiment_id == self.experiment.id or self.experiment.id.startswith(experiment_id):
            return self.experiment
        raise ExperimentNotFoundError("no such experiment")

    def _write_logs(self):
        elog = ExperimentLogger(self.experiment.id, mailbox_task_id="task-99", log_dir=self.log_dir)
        elog.info("research", "research_started", "starting research")
        elog.debug("research", "file_selected", "chose foo.py")
        elog.warning("selection", "plan_reformat_retry", "retrying plan format")
        elog.log_process(
            "verifying", "tests_completed", "tests finished",
            command=["python", "-m", "pytest"], cwd="/agent/worktree",
            duration_ms=555, exit_code=1, stdout="1 failed", stderr=f"token={SECRET}",
        )
        elog.error("verifying", "tests_failed", "verification tests failed", exit_code=1)

    def test_requires_api_key(self):
        response = self.client.get(f"/experiments/{self.experiment.id}/logs")
        self.assertEqual(response.status_code, 401)

    def test_rejects_wrong_api_key(self):
        response = self.client.get(
            f"/experiments/{self.experiment.id}/logs", headers={"x-api-key": "wrong"}
        )
        self.assertEqual(response.status_code, 401)

    def test_unknown_experiment_returns_404(self):
        response = self.client.get(
            "/experiments/deadbeefdeadbeef/logs", headers={"x-api-key": "test-key"}
        )
        self.assertEqual(response.status_code, 404)

    def test_missing_log_file_returns_empty_entries(self):
        response = self.client.get(
            f"/experiments/{self.experiment.id}/logs", headers={"x-api-key": "test-key"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["entries"], [])
        self.assertEqual(body["experiment_id"], self.experiment.id)

    def test_returns_entries_chronologically(self):
        self._write_logs()
        response = self.client.get(
            f"/experiments/{self.experiment.id}/logs", headers={"x-api-key": "test-key"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 5)
        events = [e["event"] for e in body["entries"]]
        self.assertEqual(
            events,
            ["research_started", "file_selected", "plan_reformat_retry", "tests_completed", "tests_failed"],
        )
        # Correlation fields must be present throughout.
        for entry in body["entries"]:
            self.assertEqual(entry["experiment_id"], self.experiment.id)
            self.assertEqual(entry["mailbox_task_id"], "task-99")

    def test_filters_by_level(self):
        self._write_logs()
        response = self.client.get(
            f"/experiments/{self.experiment.id}/logs?level=ERROR", headers={"x-api-key": "test-key"}
        )
        body = response.json()
        # "tests_completed" auto-escalates to ERROR too since it recorded a
        # non-zero exit code, alongside the explicit "tests_failed" entry.
        self.assertEqual(body["count"], 2)
        self.assertEqual(
            {e["event"] for e in body["entries"]}, {"tests_completed", "tests_failed"}
        )

    def test_filters_by_phase(self):
        self._write_logs()
        response = self.client.get(
            f"/experiments/{self.experiment.id}/logs?phase=verifying", headers={"x-api-key": "test-key"}
        )
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertTrue(all(e["phase"] == "verifying" for e in body["entries"]))

    def test_rejects_invalid_level_filter(self):
        response = self.client.get(
            f"/experiments/{self.experiment.id}/logs?level=NOTALEVEL", headers={"x-api-key": "test-key"}
        )
        self.assertEqual(response.status_code, 400)

    def test_id_prefix_resolution(self):
        self._write_logs()
        prefix = self.experiment.id[:8]
        response = self.client.get(f"/experiments/{prefix}/logs", headers={"x-api-key": "test-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["experiment_id"], self.experiment.id)

    def test_response_never_leaks_secrets(self):
        self._write_logs()
        response = self.client.get(
            f"/experiments/{self.experiment.id}/logs", headers={"x-api-key": "test-key"}
        )
        serialized = json.dumps(response.json())
        self.assertNotIn(SECRET, serialized)

    def test_response_uses_explicit_model_fields_only(self):
        """The response must never be a generic dump of the internal log
        dict - only the documented, allowlisted fields."""
        self._write_logs()
        response = self.client.get(
            f"/experiments/{self.experiment.id}/logs", headers={"x-api-key": "test-key"}
        )
        entry = response.json()["entries"][0]
        expected_fields = {
            "timestamp", "level", "logger", "message", "experiment_id",
            "mailbox_task_id", "phase", "event", "duration_ms", "command",
            "cwd", "exit_code", "stdout", "stderr", "exception_type",
            "exception_message", "stacktrace", "truncated",
        }
        self.assertEqual(set(entry.keys()), expected_fields)

    def test_merges_correlated_mailbox_task_log(self):
        # Gap 1: selecting one experiment must show the same complete
        # task path as the unified feed, including events recorded before
        # the experiment id existed.
        from modules.experiment_logger import get_task_logger

        self.service.mailbox_task_id_for_experiment.return_value = "task-99"
        get_task_logger("task-99", log_dir=self.log_dir).info(
            "mailbox", "task_received", "mailbox task received"
        )
        self._write_logs()
        response = self.client.get(
            f"/experiments/{self.experiment.id}/logs", headers={"x-api-key": "test-key"}
        )
        body = response.json()
        events = [e["event"] for e in body["entries"]]
        self.assertEqual(events[0], "task_received")
        self.assertIsNone(body["entries"][0]["experiment_id"])

    def test_cursor_pagination_returns_older_entries_without_duplicates(self):
        self._write_logs()
        first = self.client.get(
            f"/experiments/{self.experiment.id}/logs?limit=2", headers={"x-api-key": "test-key"}
        ).json()
        self.assertEqual(len(first["entries"]), 2)
        self.assertIsNotNone(first["next_cursor"])

        second = self.client.get(
            f"/experiments/{self.experiment.id}/logs?limit=2&cursor={first['next_cursor']}",
            headers={"x-api-key": "test-key"},
        ).json()
        first_events = [e["event"] for e in first["entries"]]
        second_events = [e["event"] for e in second["entries"]]
        self.assertEqual(len(set(first_events) & set(second_events)), 0)

    def test_repeated_pagination_reproduces_every_entry_exactly_once(self):
        # Several of these entries share an identical timestamp (no sleep
        # between writes in _write_logs): the page boundary must still be
        # gap-free and duplicate-free when walked end to end.
        self._write_logs()
        full = self.client.get(
            f"/experiments/{self.experiment.id}/logs?limit=500", headers={"x-api-key": "test-key"}
        ).json()
        expected_events = [e["event"] for e in full["entries"]]
        self.assertGreaterEqual(len(expected_events), 5)

        collected: list[str] = []
        cursor = None
        for _ in range(20):
            url = f"/experiments/{self.experiment.id}/logs?limit=2" + (
                f"&cursor={cursor}" if cursor else ""
            )
            page = self.client.get(url, headers={"x-api-key": "test-key"}).json()
            collected = [e["event"] for e in page["entries"]] + collected
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(collected, expected_events)

    def test_malformed_entry_does_not_fail_the_whole_response(self):
        self._write_logs()
        with open(
            self.log_dir / f"{self.experiment.id}.jsonl", "a", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps({
                "timestamp": "2026-01-01T00:00:10Z", "level": "INFO",
                "logger": "nexus.experiment", "experiment_id": self.experiment.id,
                "event": "missing_phase_field",
            }) + "\n")

        response = self.client.get(
            f"/experiments/{self.experiment.id}/logs", headers={"x-api-key": "test-key"}
        )
        self.assertEqual(response.status_code, 200)
        events = [e["event"] for e in response.json()["entries"]]
        self.assertNotIn("missing_phase_field", events)


if __name__ == "__main__":
    unittest.main()
