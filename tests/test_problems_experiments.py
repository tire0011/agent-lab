import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import data_sources
import nexus_mcp
from fastapi import FastAPI


class ExperimentProblemsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.db = self.root / "agent.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """CREATE TABLE experiments (
                    id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, ended_at TEXT, error TEXT,
                    failure_phase TEXT, failure_type TEXT
                )"""
            )
        self.old_runs = data_sources.RUNS_DIR
        data_sources.RUNS_DIR = self.runs
        self.addCleanup(setattr, data_sources, "RUNS_DIR", self.old_runs)

    def _experiment(self, id, issue, status, timestamp, error=None, **failure):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """INSERT INTO experiments
                   (id, goal, status, created_at, ended_at, error, failure_phase, failure_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    id, f"GitHub Issue #{issue}: test", status, timestamp, timestamp, error,
                    failure.get("failure_phase"), failure.get("failure_type"),
                ),
            )

    def _run(self, id, timestamp, error):
        (self.runs / f"{id}.json").write_text(
            json.dumps({"timestamp": timestamp, "response": f"ERROR: {error}"}),
            encoding="utf-8",
        )

    def test_newer_needs_review_experiment_precedes_old_run(self):
        self._run("old", "2026-08-10T00:00:00+00:00", "old run failure")
        self._experiment(
            "new-exp", 403, "needs_review", "2026-10-24T00:00:00+00:00",
            "review this change", failure_phase="verification", failure_type="test_failure",
        )

        problems = data_sources.list_problems(experiment_db_path=self.db)

        self.assertEqual([problem.source for problem in problems], ["experiment", "run_log"])
        self.assertEqual(problems[0].summary, "review this change")
        self.assertEqual(problems[0].issue_number, 403)
        self.assertEqual(problems[0].failure_phase, "verification")
        self.assertEqual(problems[0].failure_type, "test_failure")

    def test_later_success_suppresses_failure_for_same_issue(self):
        self._experiment("failed", 7, "failed", "2026-10-20T00:00:00+00:00", "broke")
        self._experiment("success", 7, "succeeded", "2026-10-21T00:00:00+00:00")

        self.assertEqual(data_sources.list_problems(experiment_db_path=self.db), [])

    def test_limit_and_since_apply_to_combined_results(self):
        self._run("old", "2026-08-10T00:00:00+00:00", "old")
        self._experiment("new", 8, "failed", "2026-10-21T00:00:00+00:00", "new")

        limited = data_sources.list_problems(limit=1, experiment_db_path=self.db)
        recent = data_sources.list_problems(
            since="2026-10-01T00:00:00Z", experiment_db_path=self.db
        )

        self.assertEqual([problem.id for problem in limited], ["new"])
        self.assertEqual([problem.id for problem in recent], ["new"])


class ProblemsMcpTests(unittest.TestCase):
    def test_tool_passes_limit_and_since_to_api(self):
        class FakeMcp:
            def __init__(self):
                self.tools = {}
                self.session_manager = object()

            def tool(self, **_kwargs):
                def decorate(function):
                    self.tools[function.__name__] = function
                    return function
                return decorate

            def streamable_http_app(self, **_kwargs):
                return FastAPI()

        fake_mcp = FakeMcp()
        fake_api = SimpleNamespace(problems=MagicMock())
        with (
            patch.object(nexus_mcp, "MCPServer", return_value=fake_mcp),
            patch.object(nexus_mcp, "_api", return_value=fake_api),
            patch.object(nexus_mcp, "_call_api", return_value={}) as call_api,
        ):
            nexus_mcp._build_mount_app()
            fake_mcp.tools["problems"](limit=1, since="2026-10-01T00:00:00Z")

        call_api.assert_called_once_with(
            fake_api.problems,
            limit=1,
            since=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
