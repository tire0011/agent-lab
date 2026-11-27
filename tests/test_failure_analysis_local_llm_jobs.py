"""FailureAnalysis runs on the shared local-LLM job path (#167).

The regression these tests lock down is the #115/#116 rerun quoted in the
issue: finalization reached ``needs_review``, FailureAnalysis started, and the
analyzer's *own* direct HTTP call to llama-server hit a 60s read timeout and
degraded a healthy-but-slow diagnosis to ``analysis_unavailable`` - while
controller inference, running through :class:`LocalLlmJobService`, was happily
taking minutes for the same model.

So the checks here are about *which mechanism* runs the inference:

* analysis inference is queued as a ``failure_analysis`` job and executed by
  the job worker, not by a second direct client call on the caller's thread;
* a long, healthy job is allowed to finish and is persisted/published
  normally - no analysis-side deadline;
* a job that fails, is cancelled, or is classified as stuck produces exactly
  one durable ``analysis_unavailable`` record, with no second analysis;
* controller and failure-analysis jobs share one service, one queue, one
  worker and one observability path, told apart only by their role.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from modules.failure_analysis_service import CATEGORY_UNAVAILABLE, FailureAnalysisService
from modules.local_llm_jobs import (
    ERROR_KIND_STUCK,
    ROLE_CONTROLLER,
    ROLE_FAILURE_ANALYSIS,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    LocalLlmJobService,
    acquire_local_llm_job_service,
    release_local_llm_job_service,
)
from modules.mailbox.config import MailboxConfig


def _diagnosis_json(**overrides) -> str:
    payload = {
        "summary": "target base moved after checkpoint CI",
        "category": "lifecycle_logic",
        "confidence": 0.8,
        "likely_root_causes": [],
        "missing_information": [],
        "recommended_checks": [],
        "recommended_actions": [],
        "can_self_recover": False,
        "needs_external_model": False,
        "needs_human": False,
        "escalation_reason": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


class _RecordingClient:
    """Local-model stand-in that records how and where it was called."""

    model = "test-local-model"

    def __init__(self, response: str | Exception = "", *, delay: float = 0.0):
        self._response = response
        self.delay = delay
        self.calls: list[dict] = []
        self.threads: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    async def complete(self, *, prompt, temperature, max_tokens, system=None, response_format=None):
        del temperature, max_tokens, response_format
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            self.threads.append(threading.current_thread().name)
            self.calls.append({"prompt": prompt, "system": system})
            if self.delay:
                threading.Event().wait(self.delay)
            if isinstance(self._response, Exception):
                raise self._response
            return self._response
        finally:
            with self._lock:
                self.concurrent -= 1


class _BlockingClient:
    """Blocks inside the model call until the test releases it."""

    model = "test-local-model"

    def __init__(self, response: str = ""):
        self._response = response
        self.started = threading.Event()
        self.release = threading.Event()

    async def complete(self, *, prompt, temperature, max_tokens, system=None, response_format=None):
        del prompt, temperature, max_tokens, system, response_format
        self.started.set()
        self.release.wait(timeout=10)
        return self._response


class _FailureAnalysisJobTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = str(self.tmp / "agent.db")
        self.cfg = MailboxConfig.from_paths(self.tmp, self.tmp)

    def _jobs(self, client, **kwargs) -> LocalLlmJobService:
        jobs = LocalLlmJobService(db_path=self.db_path, llama_client=client, poll_interval=0.01, **kwargs)
        self.addCleanup(jobs.close)
        return jobs

    def _service(self, jobs: LocalLlmJobService) -> FailureAnalysisService:
        service = FailureAnalysisService(
            db_path=self.db_path,
            job_service=jobs,
            log_dir=self.tmp / "logs",
            mailbox_config=self.cfg,
        )
        self.addCleanup(service.close)
        return service

    def _job_rows(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT id,role,status,system,error,error_kind,session_id FROM local_llm_jobs "
                "ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()

    def _analysis_rows(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT id,category,subject_id FROM failure_analyses ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()

    def _outbox_files(self) -> list[Path]:
        if not self.cfg.outbox_dir.is_dir():
            return []
        return sorted(self.cfg.outbox_dir.glob("*.md"))


class SubmittedThroughSharedJobServiceTests(_FailureAnalysisJobTestCase):
    """A lifecycle failure's diagnosis is queued, not called directly."""

    def test_analysis_inference_runs_as_a_failure_analysis_job(self) -> None:
        client = _RecordingClient(_diagnosis_json())
        jobs = self._jobs(client)
        service = self._service(jobs)

        analysis = service.analyze(
            subject_type="task_finalization",
            subject_id="exp-167",
            failure={"status": "needs_review", "phase": "finalization"},
            experiment_id="exp-167",
            mailbox_task_id="task-167",
        )

        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)

        rows = self._job_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], ROLE_FAILURE_ANALYSIS)
        self.assertEqual(rows[0]["status"], STATUS_SUCCEEDED)
        self.assertEqual(rows[0]["session_id"], analysis.id)
        # The analyzer's system prompt travels with the job rather than being
        # applied by a second, analyzer-owned HTTP path.
        self.assertIn("Nexus", rows[0]["system"] or "")

        # The inference itself happened on the job worker, not inline on the
        # caller's thread - which is what a direct LlamaCppClient call would do.
        self.assertEqual(len(client.threads), 1)
        self.assertTrue(
            client.threads[0].startswith("local-llm-job-worker"),
            f"analysis inference ran on {client.threads[0]!r}, not the shared job worker",
        )

    def test_no_direct_client_call_bypasses_the_job_service(self) -> None:
        client = _RecordingClient(_diagnosis_json())
        jobs = self._jobs(client)
        service = self._service(jobs)
        submitted: list[dict] = []
        original_submit = jobs.submit

        def _recording_submit(prompt, **kwargs):
            submitted.append(kwargs)
            return original_submit(prompt, **kwargs)

        jobs.submit = _recording_submit  # type: ignore[method-assign]

        service.analyze(subject_type="tool", subject_id="t-1", failure="boom")

        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["role"], ROLE_FAILURE_ANALYSIS)
        self.assertEqual(len(client.calls), len(submitted))

    def test_analyzer_does_not_install_a_short_client_timeout(self) -> None:
        # The #167 regression in one assertion: the analyzer used to build its
        # own LlamaCppClient with a 60s read timeout. Its liveness window is
        # now the shared job service's watchdog, and a legacy caller-supplied
        # analysis timeout cannot shrink it.
        service = FailureAnalysisService(
            db_path=self.db_path,
            log_dir=self.tmp / "logs",
            analysis_timeout_seconds=1.0,
        )
        self.addCleanup(service.close)
        self.assertGreaterEqual(service._jobs.watchdog_seconds, 300.0)
        self.assertGreaterEqual(service._jobs._stuck_after_seconds, 300.0)


class LongRunningHealthyJobTests(_FailureAnalysisJobTestCase):
    """A slow but healthy analysis must be allowed to finish."""

    def test_slow_job_completes_and_is_persisted_and_published(self) -> None:
        client = _RecordingClient(_diagnosis_json(summary="slow but fine"), delay=0.75)
        jobs = self._jobs(client)
        service = self._service(jobs)

        analysis = service.analyze(
            subject_type="task_finalization",
            subject_id="exp-slow",
            failure={"status": "needs_review"},
            experiment_id="exp-slow",
            mailbox_task_id="task-slow",
        )

        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertEqual(analysis.summary, "slow but fine")
        self.assertEqual(self._job_rows()[0]["status"], STATUS_SUCCEEDED)

        stored = service.get(analysis.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["category"], "lifecycle_logic")

        files = self._outbox_files()
        self.assertEqual(len(files), 1)
        self.assertIn(f"analysis_id: {analysis.id}\n", files[0].read_text(encoding="utf-8"))


class UnhealthyJobDegradesOnceTests(_FailureAnalysisJobTestCase):
    """Failure, cancellation and stuck-job detection: one durable record each."""

    def _assert_single_unavailable(self, analysis, *, subject_id: str) -> None:
        self.assertEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertTrue(analysis.needs_human)
        rows = self._analysis_rows()
        self.assertEqual(len(rows), 1, f"expected exactly one durable record, got {len(rows)}")
        self.assertEqual(rows[0]["id"], analysis.id)
        self.assertEqual(rows[0]["category"], CATEGORY_UNAVAILABLE)
        self.assertEqual(rows[0]["subject_id"], subject_id)
        # No recursion: diagnosing a failed diagnosis would show up as a
        # second failure-analysis job.
        analysis_jobs = [row for row in self._job_rows() if row["role"] == ROLE_FAILURE_ANALYSIS]
        self.assertEqual(len(analysis_jobs), 1)

    def test_failed_job_yields_one_unavailable_record(self) -> None:
        client = _RecordingClient(RuntimeError("llama-server connection refused"))
        jobs = self._jobs(client)
        service = self._service(jobs)

        lifecycle_status = "needs_review"
        analysis = service.analyze(
            subject_type="task_finalization",
            subject_id="exp-fail",
            failure={"status": lifecycle_status},
            experiment_id="exp-fail",
            mailbox_task_id="task-fail",
        )

        self._assert_single_unavailable(analysis, subject_id="exp-fail")
        self.assertIn("local model call failed", analysis.escalation_reason or "")
        self.assertIn("connection refused", analysis.escalation_reason or "")
        self.assertEqual(self._job_rows()[0]["status"], STATUS_FAILED)
        # The original lifecycle outcome is untouched by its own diagnosis.
        self.assertEqual(lifecycle_status, "needs_review")

    def test_cancelled_job_yields_one_unavailable_record(self) -> None:
        client = _BlockingClient(_diagnosis_json())
        self.addCleanup(client.release.set)
        jobs = self._jobs(client)
        original_submit = jobs.submit

        def _submit_then_cancel(prompt, **kwargs):
            job_id = original_submit(prompt, **kwargs)
            jobs.cancel(job_id, reason="operator cancelled the analysis job")
            return job_id

        jobs.submit = _submit_then_cancel  # type: ignore[method-assign]
        service = self._service(jobs)

        analysis = service.analyze(
            subject_type="task_finalization",
            subject_id="exp-cancel",
            failure={"status": "needs_review"},
            experiment_id="exp-cancel",
        )

        self._assert_single_unavailable(analysis, subject_id="exp-cancel")
        self.assertIn("operator cancelled", analysis.escalation_reason or "")

        # A late result from the cancelled job must not resurrect it.
        client.release.set()
        job = jobs.get(self._job_rows()[0]["id"])
        self.assertIsNotNone(job)
        self.assertEqual(job.status, STATUS_CANCELLED)

    def test_stuck_job_yields_one_unavailable_record(self) -> None:
        client = _BlockingClient(_diagnosis_json())
        self.addCleanup(client.release.set)
        jobs = self._jobs(client)
        # Any job still running is past its liveness window: the worker, not
        # the model, is what the sweep is looking for.
        jobs._stuck_after_seconds = 0.0
        service = self._service(jobs)

        with mock.patch("modules.local_llm_jobs._STUCK_SWEEP_INTERVAL_SECONDS", 0.0):
            analysis = service.analyze(
                subject_type="task_finalization",
                subject_id="exp-stuck",
                failure={"status": "needs_review"},
                experiment_id="exp-stuck",
            )

        self._assert_single_unavailable(analysis, subject_id="exp-stuck")
        row = self._job_rows()[0]
        self.assertEqual(row["status"], STATUS_FAILED)
        self.assertEqual(row["error_kind"], ERROR_KIND_STUCK)
        self.assertIn("stuck", analysis.escalation_reason or "")


class SharedWithControllerTests(_FailureAnalysisJobTestCase):
    """One local-model mechanism: same service, same queue, same worker."""

    def test_production_wiring_shares_one_job_service_per_database(self) -> None:
        service = FailureAnalysisService(db_path=self.db_path, log_dir=self.tmp / "logs")
        self.addCleanup(service.close)

        controller_jobs = acquire_local_llm_job_service(db_path=self.db_path)
        self.addCleanup(release_local_llm_job_service, controller_jobs)

        self.assertIs(controller_jobs, service._jobs)

    def test_orchestrator_and_analysis_queue_on_the_same_worker(self) -> None:
        client = _RecordingClient(_diagnosis_json(), delay=0.2)
        jobs = self._jobs(client)
        service = self._service(jobs)

        from modules.task_orchestrator import NexusTaskOrchestrator

        orchestrator = NexusTaskOrchestrator(
            operations_registry=SimpleNamespace(db_path=self.db_path, repo_root=str(self.tmp)),
            experiment_service=SimpleNamespace(),
            llama_client=client,
            llm_job_service=jobs,
        )
        self.addCleanup(orchestrator.close)
        self.assertIs(orchestrator.llm_jobs, service._jobs)

        analysis_result: list = []

        def _run_analysis() -> None:
            analysis_result.append(
                service.analyze(subject_type="task_finalization", subject_id="exp-shared", failure="boom")
            )

        worker = threading.Thread(target=_run_analysis)
        worker.start()
        controller_job = orchestrator.llm_jobs.submit(
            "controller prompt", session_id="session-1", role=ROLE_CONTROLLER
        )
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive())
        asyncio.run(jobs.wait(controller_job))

        # Both roles are in one queue, on one worker: nothing overlapped, and
        # both jobs carry the same observability fields under different roles.
        self.assertEqual(client.max_concurrent, 1)
        roles = {row["role"] for row in self._job_rows()}
        self.assertEqual(roles, {ROLE_CONTROLLER, ROLE_FAILURE_ANALYSIS})
        for row in self._job_rows():
            job = jobs.get(row["id"])
            self.assertIsNotNone(job)
            self.assertEqual(job.status, STATUS_SUCCEEDED)
            self.assertTrue(job.created_at and job.started_at and job.finished_at)

        self.assertEqual(len(analysis_result), 1)
        self.assertNotEqual(analysis_result[0].category, CATEGORY_UNAVAILABLE)
        self.assertEqual(jobs.get(controller_job).status, STATUS_SUCCEEDED)

        by_role = {job.role for job in jobs.recent(role=ROLE_FAILURE_ANALYSIS)}
        self.assertEqual(by_role, {ROLE_FAILURE_ANALYSIS})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
