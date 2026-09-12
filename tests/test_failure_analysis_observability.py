"""Regression coverage for structured FailureAnalysisService observability (#151)."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# Importing the mailbox façade installs the instrumentation used by the real
# API/mailbox processes without constructing an analyzer or invoking an LLM.
from modules import mailbox as _mailbox  # noqa: F401
from modules.failure_analysis_service import CATEGORY_UNAVAILABLE, FailureAnalysisService
from modules.mailbox.task_service import MailboxTaskService


class _FakeClient:
    model = "test-local-model"

    def __init__(self, response: str | Exception):
        self.response = response

    async def complete(self, *, prompt, system, temperature, max_tokens):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _diagnosis_json() -> str:
    return json.dumps(
        {
            "summary": "diagnosed",
            "category": "environment",
            "confidence": 0.9,
            "likely_root_causes": [],
            "missing_information": [],
            "recommended_checks": [],
            "recommended_actions": [],
            "can_self_recover": True,
            "needs_external_model": False,
            "needs_human": False,
            "escalation_reason": None,
        }
    )


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def events(self) -> list[str]:
        return [str(getattr(record, "event", "")) for record in self.records]


class FailureAnalysisObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.capture = _Capture()
        self.obs_logger = logging.getLogger("modules.failure_analysis_observability")
        self.old_level = self.obs_logger.level
        self.obs_logger.setLevel(logging.INFO)
        self.obs_logger.addHandler(self.capture)

    def tearDown(self) -> None:
        self.obs_logger.removeHandler(self.capture)
        self.obs_logger.setLevel(self.old_level)

    def _service(self, response: str | Exception) -> FailureAnalysisService:
        service = FailureAnalysisService(
            db_path=str(self.tmp / "agent.db"),
            log_dir=self.tmp / "logs",
            llama_client=_FakeClient(response),
        )
        self.addCleanup(service.close)
        self.capture.records.clear()
        return service

    def test_success_emits_complete_ordered_sequence(self) -> None:
        service = self._service(_diagnosis_json())
        analysis = service.analyze(
            subject_type="manual",
            subject_id="diag-1",
            failure="boom",
            experiment_id="exp-1",
            mailbox_task_id="task-1",
        )

        self.assertEqual(analysis.category, "environment")
        expected = [
            "failure_analysis_requested",
            "failure_analysis_started",
            "failure_analysis_model_call_started",
            "failure_analysis_model_call_completed",
            "failure_analysis_persist_started",
            "failure_analysis_persisted",
            "failure_analysis_completed",
        ]
        positions = [self.capture.events.index(event) for event in expected]
        self.assertEqual(positions, sorted(positions))

        completed = next(
            record for record in self.capture.records
            if getattr(record, "event", None) == "failure_analysis_completed"
        )
        self.assertEqual(completed.experiment_id, "exp-1")
        self.assertEqual(completed.mailbox_task_id, "task-1")
        self.assertEqual(completed.call_site, "manual_api")
        self.assertEqual(completed.persistence_outcome, "persisted")
        self.assertGreaterEqual(completed.duration_ms, 0)

    def test_model_exception_is_distinct_from_persistence_failure(self) -> None:
        service = self._service(RuntimeError("secret model details must not be logged"))
        analysis = service.analyze(subject_type="tool", subject_id="t-1", failure="boom")

        self.assertEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertIn("failure_analysis_model_call_failed", self.capture.events)
        self.assertIn("failure_analysis_unavailable", self.capture.events)
        self.assertNotIn("secret model details", " ".join(record.getMessage() for record in self.capture.records))

    def test_malformed_model_output_has_its_own_event(self) -> None:
        service = self._service("not-json")
        analysis = service.analyze(subject_type="tool", subject_id="t-2", failure="boom")

        self.assertEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertIn("failure_analysis_model_output_invalid", self.capture.events)
        self.assertNotIn("failure_analysis_model_call_failed", self.capture.events)

    def test_persistence_failure_is_visible_and_analysis_still_returns(self) -> None:
        service = self._service(_diagnosis_json())
        service._conn.close()  # noqa: SLF001 - deliberate DB failure injection

        analysis = service.analyze(subject_type="manual", subject_id="diag-2", failure="boom")

        self.assertEqual(analysis.category, "environment")
        self.assertIn("failure_analysis_persist_failed", self.capture.events)
        completed = next(
            record for record in self.capture.records
            if getattr(record, "event", None) == "failure_analysis_completed"
        )
        self.assertEqual(completed.persistence_outcome, "failed")

    def test_initialization_failure_emits_service_and_schema_failure(self) -> None:
        with self.assertRaises(Exception):
            FailureAnalysisService(
                db_path=str(self.tmp),
                llama_client=_FakeClient(_diagnosis_json()),
            )

        self.assertIn("failure_analysis_schema_initialization_failed", self.capture.events)
        self.assertIn("failure_analysis_service_initialization_failed", self.capture.events)

    def test_unavailable_lazy_service_emits_skipped_after_requested(self) -> None:
        # __new__ is intentional: only the lazy analyzer seam is needed.  A
        # missing DB parent makes construction fail before any other mailbox
        # dependencies are touched.
        service = MailboxTaskService.__new__(MailboxTaskService)
        service.failure_analysis_service = None
        service.db_path = str(self.tmp)
        service._task_log_dir = self.tmp / "logs"
        service.experiment_service = None
        service.outbox_dir = self.tmp / "outbox"
        service.mailbox_repo_path = self.tmp

        approval = SimpleNamespace(id="task-skip", task_body="goal")
        exp = SimpleNamespace(id="exp-skip", error="failed", test_exit_code=1)
        service._analyze_task_failure(approval, "failed", exp)

        self.assertIn("failure_analysis_requested", self.capture.events)
        self.assertIn("failure_analysis_service_lookup", self.capture.events)
        self.assertIn("failure_analysis_service_initialization_failed", self.capture.events)
        self.assertIn("failure_analysis_skipped", self.capture.events)

    def test_finalization_source_only_marks_startup_state_as_recovery(self) -> None:
        import modules.failure_analysis_observability as obs

        service = SimpleNamespace(
            _failure_analysis_recovery_experiment_ids=frozenset({"exp-recovered"})
        )
        self.assertEqual(obs._finalization_source(service, "exp-recovered"), "recovery")
        self.assertEqual(obs._finalization_source(service, "exp-new"), "initial")
        self.assertEqual(obs._finalization_source(SimpleNamespace(), "exp-new"), "initial")

    def test_recovery_needs_review_event_records_source_and_request_decision(self) -> None:
        # Recovery is derived from state captured at process startup. This
        # record-level assertion protects the machine-readable log contract.
        import modules.failure_analysis_observability as obs

        obs._event(  # noqa: SLF001 - verify the structured event contract directly
            "finalization_needs_review",
            experiment_id="exp-r",
            mailbox_task_id="task-r",
            approval_id="task-r",
            lifecycle_phase="finalization",
            call_site="finalization_terminal_transition",
            source="recovery",
            failure_analysis_requested=True,
        )
        record = self.capture.records[-1]
        self.assertEqual(record.event, "finalization_needs_review")
        self.assertEqual(record.source, "recovery")
        self.assertTrue(record.failure_analysis_requested)


if __name__ == "__main__":
    unittest.main()
