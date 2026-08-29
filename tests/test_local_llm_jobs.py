from __future__ import annotations

import logging
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from modules.local_llm_jobs import (
    LocalLlmJobService,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    llama_server_failure_diagnostics,
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

    async def complete(
        self,
        *,
        prompt,
        temperature,
        max_tokens,
        response_format=None,
    ):
        del temperature, max_tokens
        self.response_formats.append(response_format)
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
