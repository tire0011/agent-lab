"""Tests for the `/api/runtime/status` and `/api/runtime/trace` endpoints
(issue #76). Uses the real ``modules.runtime_observability`` singleton
(reset before/after each test) rather than mocking it, since the whole
point of these endpoints is to reflect that module's state faithfully.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules.runtime_observability import get_runtime_state

SECRET = "test-key"


class RuntimeStatusApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)
        self.state = get_runtime_state()
        # Ensure a clean slate regardless of test execution order / prior
        # state left by other modules importing runtime_observability.
        self.state._reset_locked()
        self.state._last_task = None

    def tearDown(self):
        self.state._reset_locked()
        self.state._last_task = None

    def test_requires_api_key(self):
        response = self.client.get("/api/runtime/status")
        self.assertEqual(response.status_code, 401)

    def test_status_idle_by_default(self):
        response = self.client.get("/api/runtime/status", headers={"x-api-key": SECRET})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "idle"})

    def test_status_reflects_running_task(self):
        self.state.start_task(task_id="task-1", experiment_id="exp-1", phase="implementation")
        response = self.client.get("/api/runtime/status", headers={"x-api-key": SECRET})
        body = response.json()
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["task_id"], "task-1")
        self.assertEqual(body["experiment_id"], "exp-1")
        self.assertTrue(body["healthy"])
        self.state.end_task(result="succeeded")

    def test_trace_requires_api_key(self):
        response = self.client.get("/api/runtime/trace")
        self.assertEqual(response.status_code, 401)

    def test_trace_reports_bounded_operation_history(self):
        self.state.start_task(task_id="task-1")
        with self.state.operation("git.status", phase="verifying"):
            pass
        with self.state.operation("tests.run", phase="verifying"):
            pass
        response = self.client.get("/api/runtime/trace", headers={"x-api-key": SECRET})
        body = response.json()
        self.assertIsNotNone(body["trace_id"])
        names = [op["name"] for op in body["operations"]]
        self.assertEqual(names, ["git.status", "tests.run"])
        for op in body["operations"]:
            self.assertEqual(op["status"], "completed")
        self.state.end_task(result="succeeded")

    def test_status_shows_last_task_after_completion(self):
        self.state.start_task(task_id="task-1")
        self.state.end_task(result="failed", error="tests failed")
        response = self.client.get("/api/runtime/status", headers={"x-api-key": SECRET})
        body = response.json()
        self.assertEqual(body["status"], "idle")
        self.assertEqual(body["last_task"]["result"], "failed")
        self.assertEqual(body["last_task"]["error"], "tests failed")


if __name__ == "__main__":
    unittest.main()
