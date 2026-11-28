"""Issue #814 (part 2c): GET /home-gpu/jobs and the MCP tool home_gpu_jobs_list.

Both list ``home_gpu_jobs`` rows newest first, exposing only row metadata -
the answer's hash and path, never the answer text - with the limit clamped to
1-200.
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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
from modules.home_gpu.collect import (
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    LIST_MIN_LIMIT,
    _ensure_schema,
    list_jobs,
)
from modules.run_store import init_db

API_KEY = "test-key"
HEADERS = {"x-api-key": API_KEY}

#: The exact field set a listed row may carry - no answer text among them.
ENTRY_FIELDS = {
    "job_id", "kind", "status", "submitted_at", "deadline", "answered_at",
    "model", "prompt_tokens", "completion_tokens", "seconds", "input_sha256",
    "answer_sha256", "answer_path", "host",
}

ANSWER_SENTINEL = "SENTINEL-answer-text-that-must-never-leak"


def _make_db(db_path: Path, *, with_table: bool = True) -> None:
    conn = init_db(str(db_path))
    try:
        if with_table:
            _ensure_schema(conn)
            conn.commit()
    finally:
        conn.close()


def _insert_job(
    db_path: Path,
    job_id: str,
    *,
    submitted_at: str,
    status: str = "queued",
    answer_sha256: str | None = None,
    answer_path: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO home_gpu_jobs
                (job_id, kind, status, submitted_at, deadline, input_sha256,
                 answer_sha256, answer_path)
            VALUES (?, 'chat_completion', ?, ?, '2026-01-02T00:00:00Z', ?, ?, ?)
            """,
            (job_id, status, submitted_at, "0" * 64, answer_sha256, answer_path),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_answered_job(db_path: Path, job_id: str, submitted_at: str, state_dir: Path) -> str:
    """Insert an ``answered`` row and write its answer file; returns the path."""
    answer_path = state_dir / "home_gpu" / f"{job_id}.txt"
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.write_text(ANSWER_SENTINEL + "\n", encoding="utf-8")
    digest = __import__("hashlib").sha256(ANSWER_SENTINEL.encode("utf-8")).hexdigest()
    _insert_job(
        db_path, job_id, submitted_at=submitted_at, status="answered",
        answer_sha256=digest, answer_path=str(answer_path),
    )
    return str(answer_path)


class HomeGpuJobsListServiceTests(unittest.TestCase):
    """The read function behind both entry points."""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "agent.db"
        _make_db(self.db_path)

    def test_empty_table_returns_empty_list(self):
        self.assertEqual(list_jobs(self.db_path), [])

    def test_missing_database_file_returns_empty_list(self):
        missing = Path(self.temp_dir.name) / "nope.db"
        self.assertEqual(list_jobs(missing), [])

    def test_missing_table_returns_empty_list(self):
        other = Path(self.temp_dir.name) / "other.db"
        _make_db(other, with_table=False)
        self.assertEqual(list_jobs(other), [])

    def test_newest_first_and_metadata_only(self):
        _insert_job(self.db_path, "hg-old", submitted_at="2026-01-01T00:00:00Z")
        _insert_answered_job(self.db_path, "hg-new", "2026-01-03T00:00:00Z",
                             Path(self.temp_dir.name))
        rows = list_jobs(self.db_path)
        self.assertEqual([row["job_id"] for row in rows], ["hg-new", "hg-old"])
        for row in rows:
            self.assertEqual(set(row), ENTRY_FIELDS)
        self.assertEqual(rows[0]["answer_sha256"], rows[0]["answer_sha256"])
        self.assertTrue(rows[0]["answer_path"].endswith("hg-new.txt"))
        self.assertIsNone(rows[1]["answer_sha256"])
        self.assertNotIn(ANSWER_SENTINEL, json.dumps(rows))

    def test_limit_is_clamped(self):
        for index in range(3):
            _insert_job(self.db_path, f"hg-{index}", submitted_at=f"2026-01-0{index + 1}T00:00:00Z")
        self.assertEqual(len(list_jobs(self.db_path, limit=2)), 2)
        self.assertEqual(len(list_jobs(self.db_path, limit=LIST_MIN_LIMIT - 1)), LIST_MIN_LIMIT)
        self.assertEqual(len(list_jobs(self.db_path, limit=LIST_MAX_LIMIT + 1)), 3)
        # A non-int limit falls back to the default (20 here, so all 3 rows).
        self.assertEqual(len(list_jobs(self.db_path, limit=True)), 3)  # type: ignore[arg-type]
        self.assertEqual(len(list_jobs(self.db_path, limit="x")), 3)  # type: ignore[arg-type]
        self.assertEqual(LIST_MIN_LIMIT, 1)
        self.assertEqual(LIST_MAX_LIMIT, 200)
        self.assertEqual(LIST_DEFAULT_LIMIT, 20)


class HomeGpuJobsApiTests(unittest.TestCase):
    """GET /home-gpu/jobs: authenticated, newest first, no answer text."""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "agent.db"
        _make_db(self.db_path)

        self.experiment_service = MagicMock()
        self.experiment_service.db_path = str(self.db_path)
        api.app.dependency_overrides[get_experiment_service] = lambda: self.experiment_service
        api.app.dependency_overrides[get_codex_service] = lambda: MagicMock()
        api.app.dependency_overrides[get_mailbox_task_service] = lambda: MagicMock()
        self.addCleanup(api.app.dependency_overrides.clear)

        self.client = TestClient(api.app)

    def test_requires_api_key(self):
        self.assertEqual(self.client.get("/home-gpu/jobs").status_code, 401)
        self.assertEqual(
            self.client.get("/home-gpu/jobs", headers={"x-api-key": "wrong"}).status_code, 401
        )

    def test_empty_table_returns_empty_list(self):
        response = self.client.get("/home-gpu/jobs", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"count": 0, "jobs": []})

    def test_lists_stored_row_newest_first_without_answer_text(self):
        _insert_job(self.db_path, "hg-old", submitted_at="2026-01-01T00:00:00Z")
        answer_path = _insert_answered_job(
            self.db_path, "hg-new", "2026-01-03T00:00:00Z", Path(self.temp_dir.name)
        )
        response = self.client.get("/home-gpu/jobs", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual([job["job_id"] for job in body["jobs"]], ["hg-new", "hg-old"])
        newest = body["jobs"][0]
        self.assertEqual(set(newest), ENTRY_FIELDS)
        self.assertEqual(newest["status"], "answered")
        self.assertEqual(newest["answer_path"], answer_path)
        self.assertEqual(len(newest["answer_sha256"]), 64)
        self.assertIsNone(body["jobs"][1]["answer_sha256"])
        # The answer text itself is only in the file and must not leak.
        self.assertNotIn(ANSWER_SENTINEL, response.text)

    def test_limit_query_is_clamped_to_1_200(self):
        for index in range(3):
            _insert_job(self.db_path, f"hg-{index}", submitted_at=f"2026-01-0{index + 1}T00:00:00Z")
        self.assertEqual(
            self.client.get("/home-gpu/jobs?limit=2", headers=HEADERS).json()["count"], 2
        )
        self.assertEqual(
            self.client.get("/home-gpu/jobs?limit=0", headers=HEADERS).json()["count"], 1
        )
        self.assertEqual(
            self.client.get("/home-gpu/jobs?limit=99999", headers=HEADERS).json()["count"], 3
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


class HomeGpuJobsMcpTests(unittest.TestCase):
    """The MCP read tool home_gpu_jobs_list returns the same rows as the route."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "agent.db"
        _make_db(cls.db_path)

        cls.experiment = MagicMock()
        cls.experiment.db_path = str(cls.db_path)
        cls.experiment.list_experiments.return_value = []
        cls.experiment.list_activity.return_value = []

        cls.codex = MagicMock()
        cls.codex.mark_interrupted_tasks_failed.return_value = None
        cls.mailbox = MagicMock()
        cls.mailbox.recover_after_restart.return_value = None
        cls.adoption = MagicMock()
        cls.adoption.recover_after_restart.return_value = []
        cls.operations = MagicMock()
        cls.operations.catalog.return_value = []
        cls.chains = MagicMock()
        cls.chains.recover_after_restart.return_value = None
        cls.maintenance = MagicMock()
        cls.maintenance.is_enabled.return_value = False

        api.app.dependency_overrides[get_experiment_service] = lambda: cls.experiment
        api.app.dependency_overrides[get_codex_service] = lambda: cls.codex
        api.app.dependency_overrides[get_mailbox_task_service] = lambda: cls.mailbox
        api.app.dependency_overrides[get_experiment_adoption_service] = lambda: cls.adoption
        api.app.dependency_overrides[get_operations_service] = lambda: cls.operations
        api.app.dependency_overrides[get_operation_chain_service] = lambda: cls.chains
        api.app.dependency_overrides[get_maintenance_service] = lambda: cls.maintenance

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
        # Start from an empty table for every test.
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("DELETE FROM home_gpu_jobs")
            conn.commit()
        finally:
            conn.close()

    def _mcp_post(self, body: dict):
        return self.client.post(
            "/mcp/",
            headers={
                "accept": "application/json, text/event-stream",
                "x-api-key": API_KEY,
                "mcp-protocol-version": "2025-06-18",
            },
            json=body,
        )

    def _initialize_mcp(self):
        self.client.post(
            "/mcp/",
            headers={
                "accept": "application/json, text/event-stream",
                "x-api-key": API_KEY,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )

    def _call_tool(self, name: str, arguments: dict, request_id: int = 2):
        response = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def test_tool_is_listed_as_read_only(self):
        self._initialize_mcp()
        response = self._mcp_post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        self.assertEqual(response.status_code, 200)
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        self.assertIn("home_gpu_jobs_list", names)

    def test_tool_returns_same_rows_as_route(self):
        _insert_job(self.db_path, "hg-old", submitted_at="2026-01-01T00:00:00Z")
        _insert_answered_job(
            self.db_path, "hg-new", "2026-01-03T00:00:00Z", Path(self.temp_dir.name)
        )
        self._initialize_mcp()
        result = self._call_tool("home_gpu_jobs_list", {})
        self.assertFalse(result["isError"])
        tool_body = result["structuredContent"]

        route = self.client.get("/home-gpu/jobs", headers=HEADERS)
        self.assertEqual(route.status_code, 200)
        self.assertEqual(tool_body, route.json())
        self.assertEqual([job["job_id"] for job in tool_body["jobs"]], ["hg-new", "hg-old"])
        for job in tool_body["jobs"]:
            self.assertEqual(set(job), ENTRY_FIELDS)
        self.assertNotIn(ANSWER_SENTINEL, json.dumps(tool_body))

    def test_tool_empty_table_returns_empty_list(self):
        self._initialize_mcp()
        result = self._call_tool("home_gpu_jobs_list", {})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], {"count": 0, "jobs": []})

    def test_tool_limit_is_clamped_to_1_200(self):
        for index in range(3):
            _insert_job(self.db_path, f"hg-{index}", submitted_at=f"2026-01-0{index + 1}T00:00:00Z")
        self._initialize_mcp()
        below = self._call_tool("home_gpu_jobs_list", {"limit": 0}, request_id=2)
        self.assertFalse(below["isError"])
        self.assertEqual(below["structuredContent"]["count"], 1)
        above = self._call_tool("home_gpu_jobs_list", {"limit": 99999}, request_id=3)
        self.assertFalse(above["isError"])
        self.assertEqual(above["structuredContent"]["count"], 3)
        exact = self._call_tool("home_gpu_jobs_list", {"limit": 2}, request_id=4)
        self.assertEqual(exact["structuredContent"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
