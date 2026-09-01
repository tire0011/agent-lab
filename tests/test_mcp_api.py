import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from api import (
    get_codex_service,
    get_experiment_adoption_service,
    get_experiment_service,
    get_mailbox_task_service,
    get_maintenance_service,
    get_operation_chain_service,
    get_operations_service,
)


class _NoopPublisher:
    def __init__(self, *args, **kwargs):
        pass

    def publish_pending(self):
        return None

    def close(self):
        return None


class _NoopWorker:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return None

    def stop(self):
        return None


class McpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = TemporaryDirectory()

        cls.codex = MagicMock()
        cls.codex.mark_interrupted_tasks_failed.return_value = None
        cls.codex.list_tasks.return_value = []

        cls.mailbox = MagicMock()
        cls.mailbox.recover_after_restart.return_value = None
        cls.mailbox.list_recent.return_value = []
        cls.mailbox.list_pending.return_value = []

        cls.adoption = MagicMock()
        cls.adoption.recover_after_restart.return_value = []
        cls.adoption.get.return_value = MagicMock(
            to_dict=lambda: {
                "id": None,
                "experiment_id": "exp-1",
                "status": "not_started",
                "target_branch": "main",
                "expected_files": [],
                "actual_files": [],
                "test_output_lines": [],
                "local_sync_pending": False,
            }
        )

        cls.operations = MagicMock()
        cls.operations.catalog.return_value = []

        cls.chains = MagicMock()
        cls.chains.recover_after_restart.return_value = None
        cls.chains.list_chains.return_value = []

        cls.maintenance = MagicMock()
        cls.maintenance.is_enabled.return_value = False
        cls.maintenance.audit_log = []

        cls.experiment = MagicMock()
        cls.experiment.list_activity.return_value = []
        cls.experiment.list_experiments.return_value = []

        api.app.dependency_overrides[get_codex_service] = lambda: cls.codex
        api.app.dependency_overrides[get_mailbox_task_service] = lambda: cls.mailbox
        api.app.dependency_overrides[get_experiment_adoption_service] = lambda: cls.adoption
        api.app.dependency_overrides[get_operations_service] = lambda: cls.operations
        api.app.dependency_overrides[get_operation_chain_service] = lambda: cls.chains
        api.app.dependency_overrides[get_maintenance_service] = lambda: cls.maintenance
        api.app.dependency_overrides[get_experiment_service] = lambda: cls.experiment

        cls.publisher_patch = patch.object(api, "OperationChainOutboxPublisher", _NoopPublisher)
        cls.publisher_patch.start()
        cls.task_publisher_patch = patch.object(api, "TaskApprovalOutboxPublisher", _NoopPublisher)
        cls.task_publisher_patch.start()
        cls.worker_patch = patch.object(api, "MailboxTaskWorker", _NoopWorker)
        cls.worker_patch.start()

        cls.client_cm = TestClient(api.app, raise_server_exceptions=True)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)
        cls.worker_patch.stop()
        cls.task_publisher_patch.stop()
        cls.publisher_patch.stop()
        api.app.dependency_overrides.clear()
        cls.temp_dir.cleanup()

    def setUp(self):
        self.codex.reset_mock()
        self.codex.mark_interrupted_tasks_failed.return_value = None
        self.codex.list_tasks.return_value = []

        self.mailbox.reset_mock()
        self.mailbox.recover_after_restart.return_value = None
        self.mailbox.list_recent.return_value = []
        self.mailbox.list_pending.return_value = []

        self.adoption.reset_mock()
        self.adoption.recover_after_restart.return_value = []
        self.adoption.get.return_value = MagicMock(
            to_dict=lambda: {
                "id": None,
                "experiment_id": "exp-1",
                "status": "not_started",
                "target_branch": "main",
                "expected_files": [],
                "actual_files": [],
                "test_output_lines": [],
                "local_sync_pending": False,
            }
        )

        self.operations.reset_mock()
        self.operations.catalog.return_value = []

        self.chains.reset_mock()
        self.chains.recover_after_restart.return_value = None
        self.chains.list_chains.return_value = []

        self.maintenance.reset_mock()
        self.maintenance.is_enabled.return_value = False
        self.maintenance.audit_log = []

        self.experiment.reset_mock()
        self.experiment.list_activity.return_value = []
        self.experiment.list_experiments.return_value = []

    def _mcp_post(self, body: dict, *, api_key: str | None = "test-key"):
        headers = {"accept": "application/json, text/event-stream"}
        if api_key is not None:
            headers["x-api-key"] = api_key
        if body.get("method") != "initialize":
            headers["mcp-protocol-version"] = "2025-06-18"
        return self.client.post("/mcp/", headers=headers, json=body)

    def _initialize_mcp(self):
        self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            }
        )

    def test_mcp_requires_api_key(self):
        response = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            api_key=None,
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Unauthorized"})

    def test_experiment_logs_tool_rejects_limit_zero(self):
        self._initialize_mcp()
        with patch.object(api.experiment_logger_module, "read_log_entries") as read_logs:
            response = self._mcp_post(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "experiment_logs",
                        "arguments": {
                            "experiment_id": "exp-123",
                            "limit": 0,
                        },
                    },
                }
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()["result"]
        self.assertTrue(body["isError"])
        self.assertIn("invalid limit", body["content"][0]["text"])
        self.experiment.resolve_experiment.assert_not_called()
        read_logs.assert_not_called()

    def test_experiment_logs_tool_rejects_limit_above_maximum(self):
        self._initialize_mcp()
        with patch.object(api.experiment_logger_module, "read_log_entries") as read_logs:
            response = self._mcp_post(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "experiment_logs",
                        "arguments": {
                            "experiment_id": "exp-123",
                            "limit": api.EXPERIMENT_LOGS_MAX_LIMIT + 1,
                        },
                    },
                }
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()["result"]
        self.assertTrue(body["isError"])
        self.assertIn("invalid limit", body["content"][0]["text"])
        self.experiment.resolve_experiment.assert_not_called()
        read_logs.assert_not_called()

    def test_activity_list_tool_rejects_limit_zero(self):
        self._initialize_mcp()
        response = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "activity_list",
                    "arguments": {"limit": 0},
                },
            }
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()["result"]
        self.assertTrue(body["isError"])
        self.assertIn("invalid limit", body["content"][0]["text"])
        self.experiment.list_activity.assert_not_called()

    def test_activity_list_tool_rejects_limit_above_maximum(self):
        self._initialize_mcp()
        response = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "activity_list",
                    "arguments": {"limit": api.ACTIVITY_MAX_LIMIT + 1},
                },
            }
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()["result"]
        self.assertTrue(body["isError"])
        self.assertIn("invalid limit", body["content"][0]["text"])
        self.experiment.list_activity.assert_not_called()

    def test_operation_chains_list_tool_rejects_limit_zero(self):
        self._initialize_mcp()
        response = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "operation_chains_list",
                    "arguments": {"limit": 0},
                },
            }
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()["result"]
        self.assertTrue(body["isError"])
        self.assertIn("invalid limit", body["content"][0]["text"])
        self.chains.list_chains.assert_not_called()

    def test_operation_chains_list_tool_rejects_limit_above_maximum(self):
        self._initialize_mcp()
        response = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "operation_chains_list",
                    "arguments": {"limit": api.OPERATION_CHAINS_MAX_LIMIT + 1},
                },
            }
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()["result"]
        self.assertTrue(body["isError"])
        self.assertIn("invalid limit", body["content"][0]["text"])
        self.chains.list_chains.assert_not_called()

    def test_mcp_lists_only_read_only_tools(self):
        self._initialize_mcp()
        response = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        self.assertEqual(response.status_code, 200)
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        self.assertTrue(
            {
                "health",
                "status",
                "chat",
                "codex_tasks_list",
                "mailbox_messages_list",
                "experiments_list",
                "experiment_logs",
                "activity_list",
                "diagnostics_public_get",
            }.issubset(names)
        )
        self.assertFalse(
            {
                "create_idea",
                "run_codex_task",
                "approve_mailbox_task",
                "reject_mailbox_task",
                "adopt_experiment",
                "execute_operation",
                "maintenance_execute",
            }
            & names
        )

    def test_status_tool_returns_structured_content(self):
        self._initialize_mcp()
        response = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {}},
            }
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()["result"]
        self.assertFalse(body["isError"])
        self.assertEqual(
            body["structuredContent"],
            {
                "agent_status": "idle",
                "api_version": "0.2.0",
                "components": ["health", "status", "problems", "ideas", "chat"],
            },
        )

    def test_experiment_logs_tool_reuses_existing_route_logic(self):
        experiment = SimpleNamespace(id="exp-123", status="failed")
        self.experiment.resolve_experiment.return_value = experiment
        self.experiment.experiment_log_dir = self.temp_dir.name
        self.experiment.mailbox_task_id_for_experiment.return_value = None

        entries = [
            {
                "timestamp": "2026-08-19T20:00:00Z",
                "level": "ERROR",
                "logger": "experiment",
                "message": "tests failed",
                "experiment_id": "exp-123",
                "mailbox_task_id": "task-1",
                "phase": "verifying",
                "event": "tests_failed",
                "duration_ms": 12,
                "command": "python -m unittest",
                "cwd": None,
                "exit_code": 1,
                "stdout": "failed",
                "stderr": None,
                "exception_type": None,
                "exception_message": None,
                "stacktrace": None,
                "truncated": False,
            }
        ]

        self._initialize_mcp()
        with patch.object(
            api.experiment_logger_module, "read_experiment_logs", return_value=entries
        ) as read_logs:
            response = self._mcp_post(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "experiment_logs",
                        "arguments": {
                            "experiment_id": "exp-123",
                            "limit": 2,
                            "level": "ERROR",
                            "phase": "verifying",
                        },
                    },
                }
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()["result"]["structuredContent"]
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["experiment_id"], "exp-123")
        self.assertEqual(body["entries"][0]["event"], "tests_failed")
        read_logs.assert_called_once_with(
            self.temp_dir.name, "exp-123",
            mailbox_task_id=None, level="ERROR", phase="verifying", limit=3, before=None,
        )
        self.experiment.resolve_experiment.assert_called_once_with("exp-123")


if __name__ == "__main__":
    unittest.main()
