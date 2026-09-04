"""API tests for the authenticated Codex task endpoints in api.py.

A fake CodexTaskService (backed by a temporary SQLite file and a fake worker)
is injected via FastAPI's ``dependency_overrides``. No real Codex process is
started and no Codex quota is consumed.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules.codex_task_service import CodexTaskService
from modules.codex_worker import CodexResult

API_KEY = "test-key"
HEADERS = {"x-api-key": API_KEY}


def _make_result(**overrides) -> CodexResult:
    defaults = dict(
        success=True,
        exit_code=0,
        stdout="",
        stderr="",
        runtime_ms=42,
        timed_out=False,
        stdout_truncated=False,
        stderr_truncated=False,
        thread_id="tid-1",
        final_message="done",
        usage={"input_tokens": 10, "output_tokens": 5},
        task="task",
        working_dir="/agent",
        sandbox="read-only",
        error=None,
    )
    defaults.update(overrides)
    return CodexResult(**defaults)


class CodexApiTestCase(unittest.TestCase):
    """Base test case: fresh temp-db CodexTaskService + fake worker per test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        self.worker = MagicMock()
        self.service = CodexTaskService(worker=self.worker, db_path=self.db_path)
        self.addCleanup(self.service.close)

        api.app.dependency_overrides[api.get_codex_service] = lambda: self.service
        self.addCleanup(api.app.dependency_overrides.clear)

        self.client = TestClient(api.app)


class CreateTaskTests(CodexApiTestCase):
    def test_authenticated_create_task_is_queued(self):
        response = self.client.post(
            "/codex/tasks", json={"prompt": "inspect readme"}, headers=HEADERS
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["prompt"], "inspect readme")
        self.assertTrue(body["id"])
        self.worker.run.assert_not_called()

    def test_server_forces_working_dir(self):
        response = self.client.post(
            "/codex/tasks",
            json={"prompt": "do something", "working_dir": "/tmp"},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["working_dir"], "/agent")

    def test_server_forces_read_only_sandbox(self):
        response = self.client.post(
            "/codex/tasks",
            json={"prompt": "do something", "sandbox": "workspace-write"},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["sandbox"], "read-only")

    def test_default_timeout_is_applied(self):
        response = self.client.post(
            "/codex/tasks", json={"prompt": "do something"}, headers=HEADERS
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["timeout_seconds"], api.CODEX_DEFAULT_TIMEOUT_SECONDS)

    def test_smaller_timeout_is_honored(self):
        response = self.client.post(
            "/codex/tasks",
            json={"prompt": "do something", "timeout_seconds": 5},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["timeout_seconds"], 5)

    def test_timeout_above_server_maximum_is_rejected(self):
        response = self.client.post(
            "/codex/tasks",
            json={"prompt": "do something", "timeout_seconds": 99999},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 422)

    def test_empty_prompt_is_rejected(self):
        response = self.client.post("/codex/tasks", json={"prompt": ""}, headers=HEADERS)
        self.assertEqual(response.status_code, 422)

    def test_whitespace_only_prompt_is_rejected(self):
        response = self.client.post("/codex/tasks", json={"prompt": "   "}, headers=HEADERS)
        self.assertEqual(response.status_code, 400)

    def test_too_long_prompt_is_rejected(self):
        response = self.client.post(
            "/codex/tasks", json={"prompt": "x" * (16 * 1024 + 1)}, headers=HEADERS
        )
        self.assertEqual(response.status_code, 422)


class AuthEnforcementTests(CodexApiTestCase):
    def test_missing_auth_is_rejected(self):
        response = self.client.post("/codex/tasks", json={"prompt": "do something"})
        self.assertEqual(response.status_code, 401)

    def test_wrong_auth_is_rejected(self):
        response = self.client.post(
            "/codex/tasks",
            json={"prompt": "do something"},
            headers={"x-api-key": "wrong-key"},
        )
        self.assertEqual(response.status_code, 401)

    def test_rejected_create_does_not_persist_task(self):
        self.client.post("/codex/tasks", json={"prompt": "do something"})  # no auth
        self.assertEqual(self.service.list_tasks(limit=10), [])

    def test_rejected_run_does_not_invoke_worker(self):
        task = self.service.create_task(
            prompt="do something", working_dir="/agent", sandbox="read-only", timeout_seconds=30
        )
        response = self.client.post(f"/codex/tasks/{task.id}/run")  # no auth
        self.assertEqual(response.status_code, 401)
        self.worker.run.assert_not_called()

    def test_get_task_requires_auth(self):
        task = self.service.create_task(
            prompt="do something", working_dir="/agent", sandbox="read-only", timeout_seconds=30
        )
        response = self.client.get(f"/codex/tasks/{task.id}")
        self.assertEqual(response.status_code, 401)

    def test_list_tasks_requires_auth(self):
        response = self.client.get("/codex/tasks")
        self.assertEqual(response.status_code, 401)


class GetTaskTests(CodexApiTestCase):
    def test_get_existing_task(self):
        created = self.client.post(
            "/codex/tasks", json={"prompt": "inspect readme"}, headers=HEADERS
        ).json()
        response = self.client.get(f"/codex/tasks/{created['id']}", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], created["id"])

    def test_get_unknown_task_returns_404(self):
        response = self.client.get("/codex/tasks/does-not-exist", headers=HEADERS)
        self.assertEqual(response.status_code, 404)


class ListTasksTests(CodexApiTestCase):
    def test_list_is_limited_and_newest_first(self):
        created_ids = [
            self.service.create_task(
                prompt=f"task {i}", working_dir="/agent", sandbox="read-only", timeout_seconds=30
            ).id
            for i in range(5)
        ]
        response = self.client.get("/codex/tasks?limit=2", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["tasks"]), 2)
        # Newest-first: the last two created ids, most recent first.
        self.assertEqual([t["id"] for t in body["tasks"]], list(reversed(created_ids))[:2])

    def test_list_summary_omits_prompt(self):
        self.service.create_task(
            prompt="secret-ish prompt text", working_dir="/agent", sandbox="read-only",
            timeout_seconds=30,
        )
        response = self.client.get("/codex/tasks", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("prompt", response.json()["tasks"][0])

    def test_list_uses_sensible_default_limit(self):
        for i in range(3):
            self.service.create_task(
                prompt=f"task {i}", working_dir="/agent", sandbox="read-only", timeout_seconds=30
            )
        response = self.client.get("/codex/tasks", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)

    def test_list_rejects_invalid_limit(self):
        response = self.client.get("/codex/tasks?limit=0", headers=HEADERS)
        self.assertEqual(response.status_code, 422)

    def test_list_enforces_server_maximum(self):
        response = self.client.get(
            f"/codex/tasks?limit={api.CODEX_LIST_MAX_LIMIT + 1}", headers=HEADERS
        )
        self.assertEqual(response.status_code, 422)


class RunTaskTests(CodexApiTestCase):
    def test_successful_run_returns_succeeded(self):
        self.worker.run.return_value = _make_result()
        created = self.client.post(
            "/codex/tasks", json={"prompt": "inspect readme"}, headers=HEADERS
        ).json()
        response = self.client.post(f"/codex/tasks/{created['id']}/run", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["thread_id"], "tid-1")
        self.assertEqual(body["usage"], {"input_tokens": 10, "output_tokens": 5})

    def test_worker_error_is_returned_as_stored_status_not_500(self):
        self.worker.run.return_value = _make_result(
            success=False, exit_code=1, error="boom", final_message=None
        )
        created = self.client.post(
            "/codex/tasks", json={"prompt": "inspect readme"}, headers=HEADERS
        ).json()
        response = self.client.post(f"/codex/tasks/{created['id']}/run", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "failed")
        self.assertIn("boom", body["error"])

    def test_timeout_is_returned_as_timed_out(self):
        self.worker.run.return_value = _make_result(
            success=False, exit_code=None, timed_out=True, error="Codex process timed out after 30s"
        )
        created = self.client.post(
            "/codex/tasks", json={"prompt": "inspect readme"}, headers=HEADERS
        ).json()
        response = self.client.post(f"/codex/tasks/{created['id']}/run", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "timed_out")

    def test_second_run_is_rejected_with_409(self):
        self.worker.run.return_value = _make_result()
        created = self.client.post(
            "/codex/tasks", json={"prompt": "inspect readme"}, headers=HEADERS
        ).json()
        first = self.client.post(f"/codex/tasks/{created['id']}/run", headers=HEADERS)
        self.assertEqual(first.status_code, 200)
        second = self.client.post(f"/codex/tasks/{created['id']}/run", headers=HEADERS)
        self.assertEqual(second.status_code, 409)
        self.worker.run.assert_called_once()

    def test_run_unknown_task_returns_404(self):
        response = self.client.post("/codex/tasks/does-not-exist/run", headers=HEADERS)
        self.assertEqual(response.status_code, 404)


class ExistingEndpointsStillWorkTests(CodexApiTestCase):
    def test_health_still_reachable(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "version": "0.2.0"})

    def test_secure_health_still_enforces_auth(self):
        response = self.client.get("/secure-health")
        self.assertEqual(response.status_code, 401)
        response = self.client.get("/secure-health", headers=HEADERS)
        self.assertEqual(response.status_code, 200)


class StartupRecoveryTests(unittest.TestCase):
    """Verifies mark_interrupted_tasks_failed() runs once at API startup."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        self.service = CodexTaskService(worker=MagicMock(), db_path=self.db_path)
        self.addCleanup(self.service.close)
        api.app.dependency_overrides[api.get_codex_service] = lambda: self.service
        self.addCleanup(api.app.dependency_overrides.clear)

    def test_orphaned_running_task_marked_failed_on_startup(self):
        task = self.service.create_task(
            prompt="do something", working_dir="/agent", sandbox="read-only", timeout_seconds=30
        )
        # Simulate a crash while running: force status to 'running' directly.
        with self.service._conn:
            self.service._conn.execute(
                "UPDATE codex_tasks SET status = 'running' WHERE id = ?", (task.id,)
            )

        with TestClient(api.app):
            pass  # __enter__/__exit__ trigger the lifespan startup/shutdown

        recovered = self.service.get_task(task.id)
        self.assertEqual(recovered.status, "failed")
        self.assertIn("interrupted", recovered.error)

    def test_startup_does_not_restart_task(self):
        task = self.service.create_task(
            prompt="do something", working_dir="/agent", sandbox="read-only", timeout_seconds=30
        )
        with self.service._conn:
            self.service._conn.execute(
                "UPDATE codex_tasks SET status = 'running' WHERE id = ?", (task.id,)
            )

        with TestClient(api.app):
            pass

        self.service.worker.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
