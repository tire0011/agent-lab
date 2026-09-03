"""Focused tests for GET /logs, the unified "Alle Experimente" log feed.

Uses a mocked ExperimentService (same pattern as test_experiment_logs_api.py)
but writes real JSONL files via modules.central_logging so aggregation,
correlation, filtering, and secret-scrubbing are exercised end to end.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules.central_logging import ExperimentLogger, get_chat_logger
from modules.experiment_service import ExperimentNotFoundError

SECRET = "abcdef0123456789abcdef0123456789"


class UnifiedLogsApiTests(unittest.TestCase):
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
        # A real LogReader would shell out to journalctl; tests instead
        # control exactly what the merged journal feed returns, so
        # structured-log assertions stay deterministic and independent of
        # whatever systemd units happen to exist on the test host.
        self.log_reader = MagicMock()
        self.log_reader.read_all_journal_entries = AsyncMock(return_value=([], {}))
        api.app.dependency_overrides[api.get_experiment_service] = lambda: self.service
        api.app.dependency_overrides[api.get_codex_service] = lambda: codex
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        api.app.dependency_overrides[api.get_log_reader] = lambda: self.log_reader
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

    def test_requires_api_key(self):
        response = self.client.get("/logs")
        self.assertEqual(response.status_code, 401)

    def test_rejects_wrong_api_key(self):
        response = self.client.get("/logs", headers={"x-api-key": "wrong"})
        self.assertEqual(response.status_code, 401)

    def test_no_log_files_returns_empty_entries(self):
        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["entries"], [])

    def test_aggregates_entries_across_experiments_chronologically(self):
        self._write_logs()
        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
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

    def test_a_running_non_terminal_experiment_is_included(self):
        # "aa"*16 is status="running" in this test's fixture - no terminal
        # state or diagnostics-list membership is required for its entries
        # to show up in the unified feed.
        self._write_logs()
        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
        body = response.json()
        experiment_ids = {e["experiment_id"] for e in body["entries"]}
        self.assertIn("aa" * 16, experiment_ids)

    def test_filters_by_experiment_id(self):
        self._write_logs()
        response = self.client.get(
            f"/logs?experiment_id={'aa' * 16}", headers={"x-api-key": "test-key"}
        )
        body = response.json()
        self.assertEqual(body["count"], 3)
        self.assertTrue(all(e["experiment_id"] == "aa" * 16 for e in body["entries"]))

    def test_filters_by_experiment_id_prefix(self):
        self._write_logs()
        prefix = ("aa" * 16)[:8]
        response = self.client.get(
            f"/logs?experiment_id={prefix}", headers={"x-api-key": "test-key"}
        )
        body = response.json()
        self.assertEqual(body["count"], 3)
        self.assertTrue(all(e["experiment_id"] == "aa" * 16 for e in body["entries"]))

    def test_unknown_experiment_id_returns_empty_entries_not_error(self):
        self._write_logs()
        response = self.client.get(
            "/logs?experiment_id=deadbeefdeadbeef", headers={"x-api-key": "test-key"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_filters_by_mailbox_task_id(self):
        self._write_logs()
        response = self.client.get(
            "/logs?mailbox_task_id=task-bb", headers={"x-api-key": "test-key"}
        )
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertTrue(all(e["mailbox_task_id"] == "task-bb" for e in body["entries"]))

    def test_filters_by_chat_job_and_session_id(self):
        get_chat_logger("job-a", session_id="session-a", log_dir=self.log_dir).error(
            "chat", "job_failed", "chat failed"
        )
        get_chat_logger("job-b", session_id="session-b", log_dir=self.log_dir).info(
            "chat", "job_succeeded", "chat completed"
        )

        by_job = self.client.get(
            "/logs?job_id=job-a", headers={"x-api-key": "test-key"}
        ).json()
        by_session = self.client.get(
            "/logs?session_id=session-b", headers={"x-api-key": "test-key"}
        ).json()
        self.assertEqual([entry["event"] for entry in by_job["entries"]], ["job_failed"])
        self.assertEqual([entry["event"] for entry in by_session["entries"]], ["job_succeeded"])

    def test_filters_by_level(self):
        self._write_logs()
        response = self.client.get("/logs?level=ERROR", headers={"x-api-key": "test-key"})
        body = response.json()
        # "tests_completed" auto-escalates to ERROR too since it recorded a
        # non-zero exit code, alongside the explicit "codex_failed" entry.
        self.assertEqual(
            {e["event"] for e in body["entries"]}, {"tests_completed", "codex_failed"}
        )

    def test_filters_by_phase(self):
        self._write_logs()
        response = self.client.get("/logs?phase=implementation", headers={"x-api-key": "test-key"})
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertTrue(all(e["phase"] == "implementation" for e in body["entries"]))

    def test_rejects_invalid_level_filter(self):
        response = self.client.get("/logs?level=NOTALEVEL", headers={"x-api-key": "test-key"})
        self.assertEqual(response.status_code, 400)

    def test_repeated_refresh_is_idempotent_and_has_no_duplicates(self):
        self._write_logs()
        first = self.client.get("/logs", headers={"x-api-key": "test-key"}).json()
        second = self.client.get("/logs", headers={"x-api-key": "test-key"}).json()
        self.assertEqual(first, second)

    def test_response_never_leaks_secrets(self):
        self._write_logs()
        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
        serialized = json.dumps(response.json())
        self.assertNotIn(SECRET, serialized)

    def test_response_uses_explicit_model_fields_only(self):
        self._write_logs()
        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
        entry = response.json()["entries"][0]
        expected_fields = {
            "timestamp", "level", "logger", "message", "experiment_id",
            "mailbox_task_id", "job_id", "session_id", "phase", "event", "duration_ms", "command",
            "cwd", "exit_code", "stdout", "stderr", "exception_type",
            "exception_message", "stacktrace", "truncated", "source",
        }
        self.assertEqual(set(entry.keys()), expected_fields)

    def test_missing_log_directory_is_distinct_from_empty_feed(self):
        self.service.experiment_log_dir = self.log_dir / "missing"
        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "log storage is unreadable")

    def test_pre_experiment_task_log_entries_appear_in_unified_feed(self):
        # Gap 1: mailbox intake/queueing events recorded before an
        # experiment id exists must still show up, with experiment_id null
        # and correlatable via mailbox_task_id.
        from modules.central_logging import get_task_logger

        get_task_logger("task-zz", log_dir=self.log_dir).info(
            "mailbox", "task_received", "mailbox task received"
        )
        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertIsNone(body["entries"][0]["experiment_id"])
        self.assertEqual(body["entries"][0]["mailbox_task_id"], "task-zz")

    def test_malformed_entry_does_not_fail_the_whole_response(self):
        # Gap 3: a syntactically-valid JSON object missing a required
        # field must be skipped, not turned into a 500 for the feed.
        self._write_logs()
        bad_path = self.log_dir / ("aa" * 16 + ".jsonl")
        with open(bad_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "timestamp": "2026-01-01T00:00:01Z", "level": "INFO",
                "logger": "nexus.experiment", "experiment_id": "aa" * 16,
                "event": "missing_phase_field",
            }) + "\n")

        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
        self.assertEqual(response.status_code, 200)
        events = [e["event"] for e in response.json()["entries"]]
        self.assertNotIn("missing_phase_field", events)

    def test_cursor_pagination_returns_older_entries_without_duplicates(self):
        self._write_logs()
        first = self.client.get("/logs?limit=3", headers={"x-api-key": "test-key"}).json()
        self.assertEqual(len(first["entries"]), 3)
        self.assertIsNotNone(first["next_cursor"])

        second = self.client.get(
            f"/logs?limit=3&cursor={first['next_cursor']}", headers={"x-api-key": "test-key"}
        ).json()
        first_events = [e["event"] for e in first["entries"]]
        second_events = [e["event"] for e in second["entries"]]
        self.assertEqual(len(set(first_events) & set(second_events)), 0)

    def test_repeated_pagination_reproduces_every_entry_exactly_once(self):
        # Walking `next_cursor` until it is exhausted must reproduce the
        # complete, chronologically-ordered feed with no gaps at page
        # boundaries and no duplicates.
        self._write_logs()
        full = self.client.get("/logs?limit=500", headers={"x-api-key": "test-key"}).json()
        expected_events = [e["event"] for e in full["entries"]]
        self.assertGreaterEqual(len(expected_events), 5)

        collected: list[str] = []
        cursor = None
        for _ in range(20):
            url = "/logs?limit=2" + (f"&cursor={cursor}" if cursor else "")
            page = self.client.get(url, headers={"x-api-key": "test-key"}).json()
            collected = [e["event"] for e in page["entries"]] + collected
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(collected, expected_events)

    def test_derives_source_grouping_from_phase(self):
        self._write_logs()
        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
        by_event = {e["event"]: e["source"] for e in response.json()["entries"]}
        self.assertEqual(by_event["task_bound"], "mailbox")
        self.assertEqual(by_event["implementation_started"], "experiment")
        self.assertEqual(by_event["verification_started"], "experiment")

    def test_merges_journal_entries_into_default_view(self):
        self._write_logs()
        self.log_reader.read_all_journal_entries = AsyncMock(return_value=([
            {
                "timestamp": "2026-01-01T00:00:00+0000", "level": "INFO",
                "logger": "journalctl", "phase": "system", "event": "journal:agent-api",
                "message": "agent-api started", "source": "agent-api",
            },
        ], {}))
        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
        body = response.json()
        sources = {e["source"] for e in body["entries"]}
        self.assertIn("agent-api", sources)
        events = [e["event"] for e in body["entries"]]
        self.assertIn("journal:agent-api", events)

    def test_journal_not_merged_when_filtering_by_experiment_id(self):
        self._write_logs()
        self.log_reader.read_all_journal_entries = AsyncMock(return_value=([
            {
                "timestamp": "2026-01-01T00:00:00+0000", "level": "INFO",
                "logger": "journalctl", "phase": "system", "event": "journal:agent-api",
                "message": "agent-api started", "source": "agent-api",
            },
        ], {}))
        response = self.client.get(
            f"/logs?experiment_id={'aa' * 16}", headers={"x-api-key": "test-key"}
        )
        sources = {e["source"] for e in response.json()["entries"]}
        self.assertNotIn("agent-api", sources)
        self.log_reader.read_all_journal_entries.assert_not_awaited()

    def test_filters_by_source(self):
        self._write_logs()
        response = self.client.get("/logs?source=mailbox", headers={"x-api-key": "test-key"})
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["entries"][0]["event"], "task_bound")

    def test_filters_by_free_text_search(self):
        self._write_logs()
        response = self.client.get("/logs?search=codex_failed", headers={"x-api-key": "test-key"})
        body = response.json()
        self.assertEqual([e["event"] for e in body["entries"]], ["codex_failed"])

    def test_reports_journal_source_errors_without_emptying_feed(self):
        self._write_logs()
        self.log_reader.read_all_journal_entries = AsyncMock(
            return_value=([], {"llama-server": "journalctl failed for llama-server (exit code 1)"})
        )
        response = self.client.get("/logs", headers={"x-api-key": "test-key"})
        body = response.json()
        self.assertGreater(body["count"], 0)
        self.assertEqual(
            body["source_errors"], {"llama-server": "journalctl failed for llama-server (exit code 1)"}
        )

    def test_include_journal_false_skips_journal_merge(self):
        self._write_logs()
        response = self.client.get(
            "/logs?include_journal=false", headers={"x-api-key": "test-key"}
        )
        self.assertEqual(response.status_code, 200)
        self.log_reader.read_all_journal_entries.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
