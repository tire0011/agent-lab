"""Unit/integration tests for the /operations/* self-healing endpoints."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from api import get_operations_executor
from modules.operations_executor import OperationResult


_HEADERS = {"x-api-key": "test-key"}
_BAD_HEADERS = {"x-api-key": "wrong"}


def _make_executor(operation: str, data: dict, success: bool = True, error: str | None = None):
    """Return a mock OperationsExecutor whose execute() returns a canned OperationResult."""
    from datetime import datetime, timezone

    result = OperationResult(
        success=success,
        operation=operation,
        timestamp=datetime.now(timezone.utc).isoformat(),
        data=data,
        error=error,
        duration_ms=5,
        status="ok" if success else "error",
    )
    mock = MagicMock()
    mock.execute = AsyncMock(return_value=result)
    return mock


class TestGitOperationsEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, raise_server_exceptions=True)

    def _override(self, executor):
        api.app.dependency_overrides[get_operations_executor] = lambda: executor

    def tearDown(self):
        api.app.dependency_overrides.pop(get_operations_executor, None)

    # --- auth checks ---
    def test_git_pull_requires_auth(self):
        resp = self.client.post("/operations/git/pull")
        self.assertEqual(resp.status_code, 401)

    def test_git_status_requires_auth(self):
        resp = self.client.get("/operations/git/status")
        self.assertEqual(resp.status_code, 401)

    def test_git_commit_push_requires_auth(self):
        resp = self.client.post("/operations/git/commit-and-push",
                                json={"message": "x", "files": ["f"]})
        self.assertEqual(resp.status_code, 401)

    # --- happy-path ---
    def test_git_pull_success(self):
        mock = _make_executor("git/pull", {"success": True, "commits_pulled": 2, "message": "ok"})
        self._override(mock)
        resp = self.client.post("/operations/git/pull", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["operation"], "git/pull")
        self.assertIn("commits_pulled", body["data"])
        mock.execute.assert_awaited_once_with("git/pull", {})

    def test_git_status_success(self):
        mock = _make_executor("git/status", {"status": "clean", "branch": "main", "uncommitted": []})
        self._override(mock)
        resp = self.client.get("/operations/git/status", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["status"], "clean")

    def test_git_commit_push_success(self):
        mock = _make_executor("git/commit-and-push", {"success": True, "commit_sha": "abc123"})
        self._override(mock)
        resp = self.client.post("/operations/git/commit-and-push", headers=_HEADERS,
                                json={"message": "Auto-commit", "files": ["README.md"]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    def test_git_commit_push_bad_api_key(self):
        resp = self.client.post("/operations/git/commit-and-push", headers=_BAD_HEADERS,
                                json={"message": "Auto-commit", "files": ["README.md"]})
        self.assertEqual(resp.status_code, 401)

    def test_git_commit_push_invalid_body(self):
        mock = _make_executor("git/commit-and-push", {})
        self._override(mock)
        # missing 'files'
        resp = self.client.post("/operations/git/commit-and-push", headers=_HEADERS,
                                json={"message": "test"})
        self.assertEqual(resp.status_code, 422)


class TestServiceOperationsEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, raise_server_exceptions=True)

    def _override(self, executor):
        api.app.dependency_overrides[get_operations_executor] = lambda: executor

    def tearDown(self):
        api.app.dependency_overrides.pop(get_operations_executor, None)

    def test_restart_requires_auth(self):
        resp = self.client.post("/operations/services/restart", json={"service": "nexus-api"})
        self.assertEqual(resp.status_code, 401)

    def test_restart_success(self):
        mock = _make_executor("services/restart", {
            "status": "restarting", "service": "nexus-api", "results": {}, "message": "ok"
        })
        self._override(mock)
        resp = self.client.post("/operations/services/restart", headers=_HEADERS,
                                json={"service": "nexus-api"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["status"], "restarting")

    def test_services_status_success(self):
        mock = _make_executor("services/status", {"nexus-api": "running", "nexus-mailbox": "running"})
        self._override(mock)
        resp = self.client.get("/operations/services/status", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    def test_llama_reload_success(self):
        mock = _make_executor("services/llama/reload-model",
                              {"status": "reloading", "model": "llama", "eta_seconds": 30})
        self._override(mock)
        resp = self.client.post("/operations/services/llama/reload-model", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["status"], "reloading")


class TestLogsEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, raise_server_exceptions=True)

    def _override(self, executor):
        api.app.dependency_overrides[get_operations_executor] = lambda: executor

    def tearDown(self):
        api.app.dependency_overrides.pop(get_operations_executor, None)

    def test_tail_requires_auth(self):
        resp = self.client.get("/operations/logs/tail?service=nexus-api")
        self.assertEqual(resp.status_code, 401)

    def test_tail_success(self):
        mock = _make_executor("logs/tail", {"service": "nexus-api", "lines": ["line1"], "total_available": 1})
        self._override(mock)
        resp = self.client.get("/operations/logs/tail?service=nexus-api&lines=10", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("lines", resp.json()["data"])

    def test_tail_requires_service_param(self):
        mock = _make_executor("logs/tail", {})
        self._override(mock)
        resp = self.client.get("/operations/logs/tail", headers=_HEADERS)
        self.assertEqual(resp.status_code, 422)

    def test_errors_success(self):
        mock = _make_executor("logs/errors",
                              {"service": "nexus-api", "error_count": 0, "errors": []})
        self._override(mock)
        resp = self.client.get("/operations/logs/errors?service=nexus-api", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["error_count"], 0)


class TestHealingEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, raise_server_exceptions=True)

    def _override(self, executor):
        api.app.dependency_overrides[get_operations_executor] = lambda: executor

    def tearDown(self):
        api.app.dependency_overrides.pop(get_operations_executor, None)

    def test_fix_locks_requires_auth(self):
        resp = self.client.post("/operations/healing/fix-locks")
        self.assertEqual(resp.status_code, 401)

    def test_fix_locks_success(self):
        mock = _make_executor("healing/fix-locks", {"success": True, "locks_removed": 1, "details": ["x.lock"]})
        self._override(mock)
        resp = self.client.post("/operations/healing/fix-locks", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["locks_removed"], 1)

    def test_cleanup_temps_success(self):
        mock = _make_executor("healing/cleanup-temps",
                              {"success": True, "cleaned_bytes": 1024, "files_removed": 3})
        self._override(mock)
        resp = self.client.post("/operations/healing/cleanup-temps", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["files_removed"], 3)

    def test_diagnose_success(self):
        mock = _make_executor("healing/diagnose", {"problems": []})
        self._override(mock)
        resp = self.client.get("/operations/healing/diagnose", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("problems", resp.json()["data"])


class TestModelManagementEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, raise_server_exceptions=True)

    def _override(self, executor):
        api.app.dependency_overrides[get_operations_executor] = lambda: executor

    def tearDown(self):
        api.app.dependency_overrides.pop(get_operations_executor, None)

    def test_models_status_requires_auth(self):
        resp = self.client.get("/operations/models/status")
        self.assertEqual(resp.status_code, 401)

    def test_models_status_success(self):
        mock = _make_executor("models/status", {"azure_gpt4o": "available", "llama": "unknown"})
        self._override(mock)
        resp = self.client.get("/operations/models/status", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("azure_gpt4o", resp.json()["data"])

    def test_models_test_success(self):
        mock = _make_executor("models/test",
                              {"success": True, "model": "azure_gpt4o", "latency_ms": 100, "response": "hi"})
        self._override(mock)
        resp = self.client.post("/operations/models/test", headers=_HEADERS,
                                json={"model": "azure_gpt4o", "prompt": "ping"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    def test_models_test_invalid_body(self):
        mock = _make_executor("models/test", {})
        self._override(mock)
        resp = self.client.post("/operations/models/test", headers=_HEADERS,
                                json={})
        self.assertEqual(resp.status_code, 422)


class TestContextEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, raise_server_exceptions=True)

    def _override(self, executor):
        api.app.dependency_overrides[get_operations_executor] = lambda: executor

    def tearDown(self):
        api.app.dependency_overrides.pop(get_operations_executor, None)

    def test_env_requires_auth(self):
        resp = self.client.get("/operations/context/env")
        self.assertEqual(resp.status_code, 401)

    def test_env_success(self):
        mock = _make_executor("context/env", {"AGENT_FS_ROOT": "/agent"})
        self._override(mock)
        resp = self.client.get("/operations/context/env", headers=_HEADERS)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("AGENT_FS_ROOT", resp.json()["data"])

    def test_set_config_success(self):
        mock = _make_executor("context/set-config",
                              {"success": True, "key": "FEATURE_FLAG_ROUTER"})
        self._override(mock)
        resp = self.client.post("/operations/context/set-config", headers=_HEADERS,
                                json={"key": "FEATURE_FLAG_ROUTER", "value": "true"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["key"], "FEATURE_FLAG_ROUTER")

    def test_set_config_requires_auth(self):
        resp = self.client.post("/operations/context/set-config",
                                json={"key": "FEATURE_FLAG_ROUTER", "value": "true"})
        self.assertEqual(resp.status_code, 401)

    def test_set_config_invalid_key_format(self):
        mock = _make_executor("context/set-config", {})
        self._override(mock)
        # key must match ^[A-Z_][A-Z0-9_]*$
        resp = self.client.post("/operations/context/set-config", headers=_HEADERS,
                                json={"key": "bad-key!", "value": "v"})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
