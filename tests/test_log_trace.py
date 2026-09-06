"""Tests for modules/log_trace.py and GET /logs/trace/{reference}.

The point of a trace is that an order can be followed from *any* single
reference to it, even though it changes identity as it moves through the
system (inbox filename -> mailbox task id -> experiment id -> session /
job id).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from modules import log_trace
from modules.mailbox.processing import transient_mailbox_task_id

os.environ.setdefault("AGENT_API_KEY", "test-key")


def _entry(**fields) -> str:
    base = {
        "timestamp": "2026-01-01T00:00:00.000Z",
        "level": "INFO",
        "logger": "nexus.experiment",
        "phase": "mailbox",
        "event": "event",
    }
    base.update(fields)
    return json.dumps(base)


class ResolveTraceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def _write(self, name: str, lines: list[str]) -> None:
        (self.log_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_full_chain(self) -> str:
        """A realistic order: inbox intake, then the experiment it became."""
        source = "inbox/20260906-1142-refresh-nexus-claude.md"
        task_id = transient_mailbox_task_id(source)
        self._write(f"task-{task_id}.jsonl", [
            _entry(timestamp="2026-01-01T00:00:01.000Z", mailbox_task_id=task_id,
                   phase="mailbox", event="message_received", message="mailbox message discovered"),
            _entry(timestamp="2026-01-01T00:00:02.000Z", mailbox_task_id=task_id,
                   experiment_id="exp-42", phase="queue", event="experiment_created",
                   message="queued as exp-42"),
        ])
        self._write("exp-42.jsonl", [
            _entry(timestamp="2026-01-01T00:00:03.000Z", experiment_id="exp-42",
                   session_id="sess-9", phase="implementation", event="codex_started",
                   message="implementation started"),
            _entry(timestamp="2026-01-01T00:00:04.000Z", experiment_id="exp-42",
                   session_id="sess-9", phase="verifying", event="tests_passed",
                   message="verification succeeded"),
        ])
        return source

    def test_inbox_filename_resolves_the_whole_chain(self):
        source = self._write_full_chain()

        trace = log_trace.resolve_trace(self.log_dir, source)

        self.assertTrue(trace.found)
        self.assertEqual(
            [e["event"] for e in trace.entries],
            ["message_received", "experiment_created", "codex_started", "tests_passed"],
        )
        self.assertIn("exp-42", trace.correlation_ids["experiment_id"])

    def test_bare_filename_without_the_inbox_prefix_also_resolves(self):
        self._write_full_chain()

        trace = log_trace.resolve_trace(self.log_dir, "20260906-1142-refresh-nexus-claude.md")

        self.assertTrue(trace.found)
        self.assertEqual(len(trace.entries), 4)

    def test_experiment_id_reaches_back_to_the_mailbox_intake(self):
        """Expansion must work in both directions: starting from the
        experiment must still surface the mailbox events that preceded it."""
        self._write_full_chain()

        trace = log_trace.resolve_trace(self.log_dir, "exp-42")

        self.assertEqual(
            [e["event"] for e in trace.entries],
            ["message_received", "experiment_created", "codex_started", "tests_passed"],
        )

    def test_session_id_resolves_the_chain(self):
        self._write_full_chain()

        trace = log_trace.resolve_trace(self.log_dir, "sess-9")

        self.assertTrue(trace.found)
        self.assertIn("message_received", [e["event"] for e in trace.entries])

    def test_free_text_fragment_falls_back_to_search_and_then_expands(self):
        self._write_full_chain()

        trace = log_trace.resolve_trace(self.log_dir, "verification succeeded")

        self.assertTrue(trace.found)
        # The fragment only appears in one entry, but the trace returns
        # the whole correlated order around it.
        self.assertEqual(len(trace.entries), 4)

    def test_unknown_reference_is_reported_as_not_found_not_an_error(self):
        self._write_full_chain()

        trace = log_trace.resolve_trace(self.log_dir, "nothing-matches-this")

        self.assertFalse(trace.found)
        self.assertEqual(trace.entries, [])

    def test_unrelated_orders_are_not_mixed_in(self):
        self._write_full_chain()
        self._write("exp-99.jsonl", [
            _entry(timestamp="2026-01-01T00:00:05.000Z", experiment_id="exp-99",
                   phase="implementation", event="other_order", message="unrelated"),
        ])

        trace = log_trace.resolve_trace(self.log_dir, "exp-42")

        self.assertNotIn("other_order", [e["event"] for e in trace.entries])

    def test_summary_is_derived_from_the_entries(self):
        self._write_full_chain()
        trace = log_trace.resolve_trace(self.log_dir, "exp-42")

        summary = log_trace.summarize(trace.entries)

        self.assertEqual(summary["entry_count"], 4)
        self.assertEqual(summary["started_at"], "2026-01-01T00:00:01.000Z")
        self.assertEqual(summary["ended_at"], "2026-01-01T00:00:04.000Z")
        self.assertEqual(summary["last_event"], "tests_passed")
        self.assertEqual(summary["error_count"], 0)

    def test_summary_counts_errors(self):
        self._write("exp-err.jsonl", [
            _entry(experiment_id="exp-err", level="ERROR", event="boom", message="failed"),
        ])
        trace = log_trace.resolve_trace(self.log_dir, "exp-err")

        self.assertEqual(log_trace.summarize(trace.entries)["error_count"], 1)

    def test_empty_summary_for_no_entries(self):
        summary = log_trace.summarize([])
        self.assertEqual(summary["entry_count"], 0)
        self.assertIsNone(summary["started_at"])


class LogTraceApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import api

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.log_dir = Path(self._tmpdir.name)

        source = "inbox/20260906-1142-refresh-nexus-claude.md"
        self.task_id = transient_mailbox_task_id(source)
        (self.log_dir / f"task-{self.task_id}.jsonl").write_text(
            _entry(timestamp="2026-01-01T00:00:01.000Z", mailbox_task_id=self.task_id,
                   experiment_id="exp-42", phase="mailbox", event="message_received",
                   message="mailbox message discovered") + "\n",
            encoding="utf-8",
        )
        (self.log_dir / "exp-42.jsonl").write_text(
            _entry(timestamp="2026-01-01T00:00:02.000Z", experiment_id="exp-42",
                   phase="verifying", event="tests_passed",
                   message="verification succeeded") + "\n",
            encoding="utf-8",
        )

        class _StubService:
            experiment_log_dir = self.log_dir

        api.app.dependency_overrides[api.get_experiment_service] = lambda: _StubService()
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)

    def test_trace_endpoint_needs_no_api_key(self):
        response = self.client.get("/logs/trace/exp-42")
        self.assertEqual(response.status_code, 200)

    def test_trace_endpoint_returns_the_chain_and_summary(self):
        body = self.client.get("/logs/trace/exp-42").json()

        self.assertTrue(body["found"])
        self.assertEqual(
            [e["event"] for e in body["entries"]], ["message_received", "tests_passed"]
        )
        self.assertEqual(body["summary"]["entry_count"], 2)
        self.assertIn("exp-42", body["correlation_ids"]["experiment_id"])

    def test_trace_endpoint_accepts_an_inbox_path_reference(self):
        body = self.client.get(
            "/logs/trace/inbox/20260906-1142-refresh-nexus-claude.md"
        ).json()

        self.assertTrue(body["found"])
        self.assertEqual(body["count"], 2)

    def test_unknown_reference_returns_found_false_not_404(self):
        response = self.client.get("/logs/trace/does-not-exist")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["found"])


if __name__ == "__main__":
    unittest.main()
