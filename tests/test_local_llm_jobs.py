from __future__ import annotations

import logging
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from modules.local_llm_jobs import (
    ERROR_KIND_CANCELLED,
    ERROR_KIND_STUCK,
    ROLE_CONTROLLER,
    ROLE_FAILURE_ANALYSIS,
    LocalLlmJobError,
    LocalLlmJobService,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    acquire_local_llm_job_service,
    llama_server_failure_diagnostics,
    release_local_llm_job_service,
)


class _SlowClient:
    async def complete(
        self,
        *,
        prompt,
        temperature,
        max_tokens,
        response_format=None,
    ):
        del temperature, max_tokens, response_format
        time.sleep(0.05)
        return "reply:" + prompt


class _RecordingClient:
    def __init__(self):
        self.response_formats = []
        self.systems = []

    async def complete(
        self,
        *,
        prompt,
        temperature,
        max_tokens,
        response_format=None,
        system=None,
    ):
        del temperature, max_tokens
        self.response_formats.append(response_format)
        self.systems.append(system)
        return "reply:" + prompt


class _FailingClient:
    """Simulates a transport error such as httpx.RemoteProtocolError."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def complete(
        self,
        *,
        prompt,
        temperature,
        max_tokens,
        response_format=None,
    ):
        del prompt, temperature, max_tokens, response_format
        raise self._exc


class _RemoteProtocolError(Exception):
    """Stand-in for httpx.RemoteProtocolError to avoid a hard dependency."""


class LocalLlmJobServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_is_persisted_and_completed_by_single_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = LocalLlmJobService(
                db_path=str(Path(tmp) / "jobs.db"),
                llama_client=_SlowClient(),
                poll_interval=0.01,
            )
            try:
                job_id = service.submit("hello", session_id="session-1", max_tokens=7)
                queued = service.get(job_id)
                self.assertIsNotNone(queued)
                result = await service.wait(job_id)
                job = service.get(job_id)
                self.assertEqual(result, "reply:hello")
                self.assertEqual(job.status, STATUS_SUCCEEDED)
                self.assertEqual(job.session_id, "session-1")
                self.assertIsNotNone(job.started_at)
                self.assertIsNotNone(job.finished_at)
            finally:
                service.close()

    async def test_structured_response_format_is_persisted_and_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _RecordingClient()
            service = LocalLlmJobService(
                db_path=str(Path(tmp) / "jobs.db"),
                llama_client=client,
                poll_interval=0.01,
            )
            response_format = {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {"type": {"type": "string"}},
                },
            }
            try:
                job_id = service.submit(
                    "return JSON",
                    response_format=response_format,
                )
                result = await service.wait(job_id)
                stored = service._conn.execute(
                    "SELECT response_format FROM local_llm_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()["response_format"]

                self.assertEqual(result, "reply:return JSON")
                self.assertEqual(client.response_formats, [response_format])
                self.assertIn('"json_schema"', stored)
            finally:
                service.close()

    async def test_transport_error_is_recorded_and_diagnostics_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = LocalLlmJobService(
                db_path=str(Path(tmp) / "jobs.db"),
                llama_client=_FailingClient(_RemoteProtocolError("peer closed connection")),
                poll_interval=0.01,
            )
            fake_diag = {
                "service": "llama-server",
                "active_state": "active",
                "sub_state": "running",
                "main_pid": "4242",
                "restart_count": "3",
                "recent_journal": ["line1", "line2"],
            }
            try:
                with mock.patch(
                    "modules.local_llm_jobs.llama_server_failure_diagnostics",
                    return_value=fake_diag,
                ) as diag_mock, self.assertLogs(
                    "modules.local_llm_jobs", level="ERROR"
                ) as log_ctx:
                    job_id = service.submit("hello", session_id="session-2", max_tokens=5)
                    with self.assertRaises(Exception):
                        await service.wait(job_id)

                job = service.get(job_id)
                self.assertEqual(job.status, STATUS_FAILED)
                self.assertIn("_RemoteProtocolError", job.error)
                self.assertIn("peer closed connection", job.error)
                self.assertEqual(job.session_id, "session-2")

                diag_mock.assert_called_once()
                combined_log = "\n".join(log_ctx.output)
                self.assertIn(job_id, combined_log)
                self.assertIn("session-2", combined_log)
                self.assertIn("_RemoteProtocolError", combined_log)
                self.assertIn("peer closed connection", combined_log)
                self.assertIn("active", combined_log)
                self.assertIn("4242", combined_log)
                self.assertIn("3", combined_log)
                self.assertIn("line1", combined_log)
            finally:
                service.close()


class JobRoleAndLivenessTests(unittest.IsolatedAsyncioTestCase):
    """Roles, cancellation and stuck-job handling are the shared component's (#167)."""

    async def test_roles_are_recorded_and_separable(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = LocalLlmJobService(
                db_path=str(Path(tmp) / "jobs.db"),
                llama_client=_RecordingClient(),
                poll_interval=0.01,
            )
            try:
                controller = service.submit("controller work", role=ROLE_CONTROLLER)
                analysis = service.submit(
                    "analysis work", role=ROLE_FAILURE_ANALYSIS, system="be terse"
                )
                await service.wait(controller)
                await service.wait(analysis)

                self.assertEqual(service.get(controller).role, ROLE_CONTROLLER)
                self.assertEqual(service.get(analysis).role, ROLE_FAILURE_ANALYSIS)
                self.assertEqual(
                    [job.id for job in service.recent(role=ROLE_FAILURE_ANALYSIS)], [analysis]
                )
                # The system prompt rides with the job instead of being applied
                # by a caller-owned second request path.
                self.assertEqual(service.llama.systems, [None, "be terse"])
            finally:
                service.close()

    async def test_cancelled_job_stays_cancelled_when_the_late_result_arrives(self):
        release = threading.Event()

        class _BlockingClient:
            async def complete(self, *, prompt, temperature, max_tokens, response_format=None, system=None):
                del prompt, temperature, max_tokens, response_format, system
                release.wait(timeout=10)
                return "late"

        with tempfile.TemporaryDirectory() as tmp:
            service = LocalLlmJobService(
                db_path=str(Path(tmp) / "jobs.db"),
                llama_client=_BlockingClient(),
                poll_interval=0.01,
            )
            try:
                job_id = service.submit("slow work", role=ROLE_FAILURE_ANALYSIS)
                self.assertTrue(service.cancel(job_id, reason="operator stopped it"))
                with self.assertRaises(LocalLlmJobError) as raised:
                    await service.wait(job_id)
                self.assertEqual(raised.exception.kind, ERROR_KIND_CANCELLED)
                self.assertEqual(raised.exception.status, STATUS_CANCELLED)

                release.set()
                time.sleep(0.2)
                self.assertEqual(service.get(job_id).status, STATUS_CANCELLED)
                self.assertFalse(service.cancel(job_id))
            finally:
                release.set()
                service.close()

    async def test_stuck_running_job_is_swept_and_releases_its_waiter(self):
        release = threading.Event()

        class _BlockingClient:
            async def complete(self, *, prompt, temperature, max_tokens, response_format=None, system=None):
                del prompt, temperature, max_tokens, response_format, system
                release.wait(timeout=10)
                return "late"

        with tempfile.TemporaryDirectory() as tmp:
            service = LocalLlmJobService(
                db_path=str(Path(tmp) / "jobs.db"),
                llama_client=_BlockingClient(),
                poll_interval=0.01,
            )
            service._stuck_after_seconds = 0.0
            try:
                with mock.patch("modules.local_llm_jobs._STUCK_SWEEP_INTERVAL_SECONDS", 0.0):
                    job_id = service.submit("wedged work")
                    with self.assertRaises(LocalLlmJobError) as raised:
                        await service.wait(job_id)
                self.assertEqual(raised.exception.kind, ERROR_KIND_STUCK)
                job = service.get(job_id)
                self.assertEqual(job.status, STATUS_FAILED)
                self.assertEqual(job.error_kind, ERROR_KIND_STUCK)
            finally:
                release.set()
                service.close()


class SchemaMigrationTests(unittest.TestCase):
    def test_pre_167_database_gains_role_system_and_error_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "legacy.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE local_llm_jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, prompt TEXT NOT NULL,
                    temperature REAL NOT NULL, max_tokens INTEGER NOT NULL,
                    session_id TEXT, created_at TEXT NOT NULL, started_at TEXT,
                    finished_at TEXT, result TEXT, error TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO local_llm_jobs (id,status,prompt,temperature,max_tokens,session_id,created_at,result) "
                "VALUES ('legacy-1','succeeded','p',0.0,10,'s','2026-01-01T00:00:00+00:00','r')"
            )
            conn.commit()
            conn.close()

            service = LocalLlmJobService(db_path=db_path, llama_client=_RecordingClient())
            try:
                job = service.get("legacy-1")
                # Rows that predate the role column were all controller work.
                self.assertEqual(job.role, ROLE_CONTROLLER)
                self.assertIsNone(job.error_kind)
                self.assertEqual([j.id for j in service.recent(role=ROLE_CONTROLLER)], ["legacy-1"])
            finally:
                service.close()


class SharedJobServiceTests(unittest.TestCase):
    """One worker per database, so features cannot race llama-server (#167)."""

    def test_acquire_shares_one_service_and_releases_it_once_unused(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "shared.db")
            first = acquire_local_llm_job_service(db_path=db_path)
            second = acquire_local_llm_job_service(db_path=db_path)
            try:
                self.assertIs(first, second)
                release_local_llm_job_service(second)
                # Still held by the first acquirer.
                self.assertFalse(first._closed.is_set())
                self.assertIs(acquire_local_llm_job_service(db_path=db_path), first)
                release_local_llm_job_service(first)
            finally:
                release_local_llm_job_service(first)
            self.assertTrue(first._closed.is_set())
            # A closed service is never handed out again.
            replacement = acquire_local_llm_job_service(db_path=db_path)
            self.addCleanup(release_local_llm_job_service, replacement)
            self.assertIsNot(replacement, first)

    def test_an_injected_client_is_never_replaced_by_a_shared_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "shared.db")
            shared = acquire_local_llm_job_service(db_path=db_path)
            self.addCleanup(release_local_llm_job_service, shared)
            scripted = _RecordingClient()
            private = acquire_local_llm_job_service(db_path=db_path, llama_client=scripted)
            self.addCleanup(release_local_llm_job_service, private)

            self.assertIsNot(private, shared)
            self.assertIs(private.llama, scripted)


class LlamaServerFailureDiagnosticsTests(unittest.TestCase):
    def test_parses_systemctl_and_journalctl_output(self):
        systemctl_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="ActiveState=active\nSubState=running\nMainPID=1234\nNRestarts=2\n",
            stderr="",
        )
        journal_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="2026-08-24T12:00:00+0000 host llama-server[1234]: ready\n"
                   "2026-08-24T12:00:01+0000 host llama-server[1234]: listening\n",
            stderr="",
        )
        with mock.patch(
            "modules.local_llm_jobs.subprocess.run",
            side_effect=[systemctl_result, journal_result],
        ):
            diag = llama_server_failure_diagnostics()

        self.assertEqual(diag["service"], "llama-server")
        self.assertEqual(diag["active_state"], "active")
        self.assertEqual(diag["sub_state"], "running")
        self.assertEqual(diag["main_pid"], "1234")
        self.assertEqual(diag["restart_count"], "2")
        self.assertEqual(len(diag["recent_journal"]), 2)
        self.assertIn("ready", diag["recent_journal"][0])

    def test_is_best_effort_and_never_raises_on_missing_tools(self):
        with mock.patch(
            "modules.local_llm_jobs.subprocess.run",
            side_effect=FileNotFoundError("systemctl not found"),
        ):
            diag = llama_server_failure_diagnostics()

        self.assertEqual(diag["service"], "llama-server")
        self.assertIn("systemctl_error", diag)
        self.assertIn("journalctl_error", diag)

    def test_handles_timeout(self):
        with mock.patch(
            "modules.local_llm_jobs.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=3),
        ):
            diag = llama_server_failure_diagnostics()

        self.assertIn("systemctl_error", diag)
        self.assertIn("TimeoutExpired", diag["systemctl_error"])


if __name__ == "__main__":
    unittest.main()
