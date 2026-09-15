"""First-class Nexus diagnostic commands, end to end (#188).

Two things are pinned here:

* the HTTP surface - ``/diagnostics/status/...``, ``/diagnostics/logs/...``,
  ``/diagnostics/logs/service/...`` and ``/diagnostics/logs/last-failure`` -
  returns structured, bounded, read-only answers, and the operations catalog
  exposes the same backend to the mailbox/chat interface;
* the #186/#187 investigation itself, as a regression test. There, Nexus
  created a branch and then produced no implementation commit and no PR, and
  finding out where execution stopped took a chain of hand-built shell
  commands. One command has to make that diagnosable now.
"""

from __future__ import annotations

import os
import unittest
from urllib.parse import quote

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules import central_logging
from modules.nexus_diagnostics import NexusDiagnostics
from modules.operations_registry import OperationsRegistry
from tests.test_nexus_diagnostics import DiagnosticsFixture, _iso

_KEY = {"x-api-key": api.API_KEY}


class DiagnosticApiTests(DiagnosticsFixture):
    def setUp(self) -> None:
        super().setUp()
        self.diagnostics = NexusDiagnostics(db_path=self.db_path, log_dir=self.log_dir)
        self.addCleanup(self.diagnostics.close)
        api.app.dependency_overrides[api.get_nexus_diagnostics] = lambda: self.diagnostics
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)

    def test_status_route_answers_from_any_reference(self) -> None:
        experiment_id = self.add_experiment(status="implementing", branch="nexus/issue-188")
        task_id = self.add_task(status="running", experiment_id=experiment_id)
        self.link_github(experiment_id=experiment_id, source_id=task_id, issue_number=188)

        # "#188" is spelled %23188 in a URL - a bare '#' is a fragment and
        # never reaches the server - but a caller who writes it that way must
        # still land on the same answer as the bare number.
        for reference in (task_id, experiment_id, "188", quote("#188", safe="")):
            with self.subTest(reference=reference):
                response = self.client.get(f"/diagnostics/status/{reference}", headers=_KEY)
                self.assertEqual(200, response.status_code)
                lifecycle = response.json()["lifecycle"]
                self.assertEqual("nexus/issue-188", lifecycle["branch"])
                self.assertEqual(188, lifecycle["issue_number"])
                self.assertIsNotNone(lifecycle["progress"]["state"])

    def test_status_route_returns_no_log_bodies(self) -> None:
        task_id = self.add_task()
        self.write_log(
            f"{central_logging.TASK_LOG_PREFIX}{task_id}.jsonl",
            [self.log_entry(minutes_ago=1, event="queued", message="queued")],
        )
        payload = self.client.get(f"/diagnostics/status/{task_id}", headers=_KEY).json()
        self.assertEqual([], payload["log_entries"])

    def test_logs_route_returns_bounded_correlated_logs(self) -> None:
        experiment_id = self.add_experiment()
        task_id = self.add_task(experiment_id=experiment_id)
        self.write_log(
            f"{central_logging.TASK_LOG_PREFIX}{task_id}.jsonl",
            [
                self.log_entry(minutes_ago=30 - index, event="step", message=f"line {index}")
                for index in range(30)
            ],
        )
        payload = self.client.get(
            f"/diagnostics/logs/{task_id}?lines=5", headers=_KEY
        ).json()
        self.assertEqual(5, len(payload["log_entries"]))
        self.assertTrue(payload["truncated"])
        # Every entry carries a timestamp and the ids it correlates on, so a
        # reader can line it up against any other feed.
        self.assertTrue(all(entry["timestamp"] for entry in payload["log_entries"]))
        self.assertIn("mailbox_task_id", payload["lifecycle"]["correlation_ids"])

    def test_an_unresolvable_reference_is_a_404_not_a_crash(self) -> None:
        response = self.client.get("/diagnostics/logs/no-such-task", headers=_KEY)
        self.assertEqual(404, response.status_code)

    def test_every_diagnostic_route_requires_the_api_key(self) -> None:
        for path in (
            "/diagnostics/status/anything",
            "/diagnostics/logs/anything",
            "/diagnostics/logs/last-failure",
            "/diagnostics/logs/service/api",
        ):
            with self.subTest(path=path):
                self.assertEqual(401, self.client.get(path).status_code)

    def test_service_route_is_not_shadowed_by_the_reference_route(self) -> None:
        """``/diagnostics/logs/service/...`` must win over ``{reference:path}``."""

        class _Reader:
            async def tail_journal_entries(self, service, lines, **kwargs):
                return [
                    {
                        "timestamp": "2026-09-15T10:00:00.000Z",
                        "level": "ERROR",
                        "message": f"from {service}",
                    }
                ]

        api.app.dependency_overrides[api.get_log_reader] = _Reader
        payload = self.client.get("/diagnostics/logs/service/mailbox", headers=_KEY).json()
        self.assertEqual("nexus-mailbox", payload["service"])
        self.assertEqual("from nexus-mailbox", payload["entries"][0]["message"])

    def test_last_failure_route_reports_the_latest_stalled_work(self) -> None:
        stalled = self.add_experiment(status="implementing", created_minutes_ago=300)
        payload = self.client.get("/diagnostics/logs/last-failure", headers=_KEY).json()
        self.assertEqual(stalled, payload["lifecycle"]["subject_id"])
        self.assertEqual("interrupted", payload["lifecycle"]["progress"]["state"])

    def test_last_failure_says_so_when_nothing_failed(self) -> None:
        self.add_experiment(status="succeeded", ended_at=_iso(2), created_minutes_ago=10)
        response = self.client.get("/diagnostics/logs/last-failure", headers=_KEY)
        self.assertEqual(404, response.status_code)


class DiagnosticOperationsTests(DiagnosticsFixture):
    """The mailbox/chat surface reaches the same backend, read-only."""

    def registry(self) -> OperationsRegistry:
        class _ExperimentService:
            experiment_log_dir = self.log_dir

        registry = OperationsRegistry(
            experiment_service=_ExperimentService(),
            db_path=self.db_path,
            repo_root=str(self.root),
        )
        self.addCleanup(registry.close)
        return registry

    def test_the_diagnostic_operations_are_catalogued_and_read_only(self) -> None:
        catalog = {spec.name: spec for spec in self.registry().catalog()}
        for name in ("nexus_logs", "nexus_status", "nexus_last_failure"):
            with self.subTest(operation=name):
                self.assertIn(name, catalog)
                self.assertTrue(catalog[name].read_only)
                self.assertIn("188", catalog[name].description)

    def test_nexus_status_renders_parseable_key_value_lines(self) -> None:
        experiment_id = self.add_experiment(status="implementing", branch="nexus/issue-188")
        task_id = self.add_task(status="running", experiment_id=experiment_id)
        result = self.registry().execute("nexus_status", {"reference": task_id})
        self.assertTrue(result.success)
        parsed = dict(
            line.split(": ", 1) for line in result.output_lines if ": " in line
        )
        self.assertEqual("running", parsed["lifecycle"])
        self.assertEqual("nexus/issue-188", parsed["branch"])

    def test_nexus_logs_includes_the_log_body(self) -> None:
        task_id = self.add_task()
        self.write_log(
            f"{central_logging.TASK_LOG_PREFIX}{task_id}.jsonl",
            [self.log_entry(minutes_ago=1, event="task_approved", message="approved")],
        )
        result = self.registry().execute("nexus_logs", {"reference": task_id})
        self.assertTrue(result.success)
        self.assertTrue(any("task_approved" in line for line in result.output_lines))

    def test_an_unknown_reference_is_a_finding_not_an_operation_fault(self) -> None:
        result = self.registry().execute("nexus_status", {"reference": "nothing-here"})
        self.assertFalse(result.success)
        self.assertIn("nothing-here", result.error_summary or "")

    def test_service_logs_accepts_a_friendly_name(self) -> None:
        captured: list[list[str]] = []

        def runner(command, **kwargs):
            captured.append(list(command))

            class _Completed:
                returncode = 0
                stdout = "line one"
                stderr = ""

            return _Completed()

        class _ExperimentService:
            experiment_log_dir = self.log_dir

        registry = OperationsRegistry(
            experiment_service=_ExperimentService(),
            db_path=self.db_path,
            repo_root=str(self.root),
            subprocess_runner=runner,
        )
        self.addCleanup(registry.close)
        result = registry.execute(
            "service_logs", {"service": "mailbox", "limit": 5, "since_minutes": 30}
        )
        self.assertTrue(result.success)
        self.assertIn("nexus-mailbox", captured[0])
        self.assertIn("--since=-30m", captured[0])


class Issue186RegressionTests(DiagnosticsFixture):
    """The #186/#187 shape: a branch was created and nothing followed.

    Nexus created the branch, then produced no implementation commit and no
    PR. From outside, that looks exactly like work still in progress, which is
    why the original investigation needed hand-built shell commands to tell
    the two apart. One diagnostic call must now answer it.
    """

    def _stalled_after_branch_creation(self) -> tuple[str, str]:
        experiment_id = self.add_experiment(
            status="implementing", branch="nexus/issue-186", created_minutes_ago=300
        )
        task_id = self.add_task(
            status="running", experiment_id=experiment_id, updated_minutes_ago=300
        )
        self.link_github(
            experiment_id=experiment_id,
            source_id=task_id,
            issue_number=186,
            branch="nexus/issue-186",
            pr_number=None,
        )
        # A worker claimed the task, created the branch, then stopped beating.
        self.add_session(source_id=task_id, heartbeat_minutes_ago=280)
        self.write_log(
            f"{central_logging.TASK_LOG_PREFIX}{task_id}.jsonl",
            [
                self.log_entry(minutes_ago=302, event="task_approved", message="approved"),
                self.log_entry(minutes_ago=299, event="experiment_queued", message="queued"),
            ],
        )
        self.write_log(
            f"{experiment_id}.jsonl",
            [
                self.log_entry(
                    minutes_ago=295,
                    event="branch_created",
                    message="created branch nexus/issue-186",
                    experiment_id=experiment_id,
                )
            ],
        )
        return task_id, experiment_id

    def test_one_command_shows_where_execution_stopped(self) -> None:
        task_id, experiment_id = self._stalled_after_branch_creation()

        report = self.diagnostics().diagnose(task_id)
        state = report.lifecycle

        # The branch exists...
        self.assertEqual("nexus/issue-186", state.branch)
        self.assertEqual(186, state.issue_number)
        # ...and nothing came after it.
        self.assertIsNone(state.pr_number)
        self.assertIsNone(state.commit)
        # The row still claims to be running, but nothing has reported
        # progress since - which is the distinction the original
        # investigation had to establish by hand.
        self.assertEqual("running", state.lifecycle)
        self.assertEqual("interrupted", state.progress.state)
        self.assertEqual("vm:1234:abcd", state.worker_id)
        self.assertIn("stale timeout", state.progress.reason)
        # And the last thing Nexus is known to have done is named outright.
        self.assertEqual("branch_created", report.log_entries[-1]["event"])
        self.assertEqual(experiment_id, state.correlation_ids["experiment_id"])

    def test_last_failure_finds_it_without_being_told_where_to_look(self) -> None:
        task_id, _ = self._stalled_after_branch_creation()
        report = self.diagnostics().last_failure()
        # It never wrote a `failed` status - the stall is the only evidence.
        self.assertIn(
            report.lifecycle.subject_id,
            {task_id, report.lifecycle.correlation_ids.get("experiment_id")},
        )
        self.assertEqual("interrupted", report.lifecycle.progress.state)

    def test_the_issue_number_alone_is_a_sufficient_reference(self) -> None:
        self._stalled_after_branch_creation()
        state = self.diagnostics().status("#186")
        self.assertEqual("nexus/issue-186", state.branch)
        self.assertEqual("interrupted", state.progress.state)

    def test_the_failure_analysis_reads_the_same_backend(self) -> None:
        """The analysis routine must not keep a log lookup of its own (#188)."""
        from modules.failure_analysis_service import _with_lifecycle_context

        task_id, experiment_id = self._stalled_after_branch_creation()
        state, entries = self.diagnostics().evidence_bundle(
            experiment_id=experiment_id, mailbox_task_id=task_id
        )
        self.assertEqual("interrupted", state.progress.state)
        # Raw entries, so the analyzer's own relevance filter still works.
        self.assertTrue(all("level" in entry for entry in entries))
        self.assertEqual(
            ["task_approved", "experiment_queued", "branch_created"],
            [entry["event"] for entry in entries],
        )
        failure = _with_lifecycle_context({"reason": "no PR appeared"}, state)
        self.assertEqual("interrupted", failure["nexus_progress"])
        self.assertEqual("nexus/issue-186", failure["nexus_branch"])
        # The caller's own account of the failure is never overwritten.
        self.assertEqual("no PR appeared", failure["reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
