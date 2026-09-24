from __future__ import annotations

import logging
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from modules import heartbeat
from modules.local_llm_jobs import (
    ERROR_KIND_CANCELLED,
    ERROR_KIND_STUCK,
    ERROR_KIND_INTERRUPTED,
    ROLE_CONTROLLER,
    ROLE_FAILURE_ANALYSIS,
    LocalLlmJobError,
    LocalLlmJobService,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RUNNING,
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


class _ConcurrencyProbe:
    """Records whether two workers were ever inside the model call at once."""

    model = "test-local-model"

    def __init__(self, *, delay: float = 0.0, shared_state: dict | None = None):
        self.delay = delay
        self.calls = 0
        self.state = shared_state if shared_state is not None else {"active": 0, "max_concurrent": 0}
        self._lock = threading.Lock()

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None, system=None):
        del temperature, max_tokens, response_format, system
        with self._lock:
            self.calls += 1
            self.state["active"] += 1
            self.state["max_concurrent"] = max(self.state["max_concurrent"], self.state["active"])
        try:
            if self.delay:
                time.sleep(self.delay)
            return "reply:" + prompt
        finally:
            with self._lock:
                self.state["active"] -= 1


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
                # "Stays cancelled" is a negative, so there is nothing to wait
                # for directly. The worker is a single thread, so a later job
                # reaching a terminal state proves it has already finished
                # handling this one's late result - an ordering guarantee,
                # where the sleep here before was a guess that got weaker the
                # busier the host was.
                await service.wait(service.submit("after", role=ROLE_FAILURE_ANALYSIS))

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


class CrossProcessSerializationTests(unittest.IsolatedAsyncioTestCase):
    """The one-request-at-a-time rule lives in the database, not in a process.

    Nexus runs the controller (`nexus_tick`) and the API as separate
    processes against the same database, so a process-wide singleton cannot
    be the thing that stops them racing llama-server. Each service instance
    here has its own connection and its own worker - the same situation two
    processes are in.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "jobs.db")

    def _service(self, client) -> LocalLlmJobService:
        service = LocalLlmJobService(
            db_path=self.db_path,
            llama_client=client,
            poll_interval=0.01,
            idle_poll_interval=0.02,
        )
        self.addCleanup(service.close)
        return service

    async def test_two_services_on_one_database_never_run_concurrently(self):
        first_client = _ConcurrencyProbe(delay=0.15)
        second_client = _ConcurrencyProbe(delay=0.15, shared_state=first_client.state)
        first = self._service(first_client)
        second = self._service(second_client)

        jobs = [first.submit(f"a{i}", role=ROLE_CONTROLLER) for i in range(3)]
        jobs += [second.submit(f"b{i}", role=ROLE_FAILURE_ANALYSIS) for i in range(3)]

        for job_id in jobs:
            self.assertTrue((await first.wait(job_id)).startswith("reply:"))

        # Both workers were awake and willing to run the whole time; the
        # claim is what kept them from overlapping at llama-server. Which
        # worker won which job is deliberately not asserted - either may
        # drain the queue, and only the overlap matters.
        self.assertEqual(first_client.state["max_concurrent"], 1)
        self.assertEqual(first_client.calls + second_client.calls, len(jobs))

    async def test_a_worker_executes_work_another_process_queued(self):
        producer_client = _ConcurrencyProbe()
        producer = self._service(producer_client)
        job_id = producer.submit("queued by the other process", role=ROLE_FAILURE_ANALYSIS)
        # The producer exits before its own worker gets to the job.
        producer.close()

        consumer_client = _ConcurrencyProbe()
        consumer = self._service(consumer_client)
        self.assertEqual(await consumer.wait(job_id), "reply:queued by the other process")
        self.assertEqual(consumer.get(job_id).worker_id, consumer.worker_id)

    async def test_closing_finishes_a_job_it_claimed_for_another_process(self):
        # A short-lived process (the tick, a mailbox run) drains the shared
        # queue, so its worker can be mid-request on a job another process is
        # waiting for when it closes. Closing used to give up after two
        # seconds and close the database under the worker: the result could
        # not be recorded and the job stayed `running` until its lease
        # lapsed, then failed as interrupted.
        started = threading.Event()
        release = threading.Event()

        class _BlockingClient:
            model = "test-local-model"

            async def complete(self, *, prompt, temperature, max_tokens, response_format=None, system=None):
                del temperature, max_tokens, response_format, system
                started.set()
                release.wait(timeout=10)
                return "reply:" + prompt

        short_lived = self._service(_BlockingClient())
        job_id = short_lived.submit("queued by the long-lived process")
        self.assertTrue(started.wait(timeout=5))

        # Released only after the old two-second close deadline has passed.
        timer = threading.Timer(2.5, release.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.addCleanup(release.set)
        short_lived.close()

        waiter = self._service(_ConcurrencyProbe())
        self.assertEqual(await waiter.wait(job_id), "reply:queued by the long-lived process")
        self.assertEqual(waiter.get(job_id).status, STATUS_SUCCEEDED)

    def test_startup_does_not_kill_another_live_worker_claim(self):
        client = _ConcurrencyProbe()
        live = self._service(client)
        job_id = live.submit("owned by a live worker")
        # A claim a live worker is still refreshing, as another process would
        # see it. Starting a second service must leave it alone.
        with live._lock:
            live._conn.execute(
                "UPDATE local_llm_jobs SET status = 'running', started_at = ?, worker_id = ?, "
                "lease_expires_at = ? WHERE id = ?",
                (
                    heartbeat.now_iso(),
                    "another-host:4242:beef",
                    (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                    job_id,
                ),
            )

        newcomer = self._service(_ConcurrencyProbe())
        self.assertEqual(newcomer.get(job_id).status, STATUS_RUNNING)

    def test_a_dead_workers_claim_is_released_and_the_slot_freed(self):
        client = _ConcurrencyProbe()
        service = self._service(client)
        abandoned = service.submit("owned by a process that died")
        with service._lock:
            service._conn.execute(
                "UPDATE local_llm_jobs SET status = 'running', started_at = ?, worker_id = ?, "
                "lease_expires_at = ? WHERE id = ?",
                (
                    heartbeat.now_iso(),
                    "dead-host:1:dead",
                    (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                    abandoned,
                ),
            )

        follow_up = service.submit("must not wait for a dead worker")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if service.get(follow_up).status == STATUS_SUCCEEDED:
                break
            time.sleep(0.02)

        self.assertEqual(service.get(follow_up).status, STATUS_SUCCEEDED)
        released = service.get(abandoned)
        self.assertEqual(released.status, STATUS_FAILED)
        self.assertEqual(released.error_kind, ERROR_KIND_INTERRUPTED)


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
