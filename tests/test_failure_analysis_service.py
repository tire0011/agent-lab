"""Tests for the reusable failure-analysis service (#145)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from modules.failure_analysis_service import (
    CATEGORY_UNAVAILABLE,
    FailureAnalysisService,
    evidence_from_log_entries,
    render_evidence_line,
)
from modules.central_logging import ExperimentLogger, read_experiment_logs


SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


class _FakeClient:
    """Scripted local-model stand-in; records the prompt it was given."""

    model = "test-local-model"

    def __init__(self, response: str | Exception):
        self._response = response
        self.prompts: list[str] = []

    async def complete(self, *, prompt, system, temperature, max_tokens):
        self.prompts.append(prompt)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _service(tmp_path: Path, client) -> FailureAnalysisService:
    return FailureAnalysisService(db_path=str(tmp_path / "agent.db"), llama_client=client)


def _diagnosis_json(**overrides) -> str:
    payload = {
        "summary": "no summary",
        "category": "unknown",
        "confidence": 0.9,
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


class AnalyzeBasicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_analysis_is_parsed_and_persisted(self):
        client = _FakeClient(_diagnosis_json(
            summary="the checkout is stale",
            category="lifecycle_logic",
            confidence=0.8,
            likely_root_causes=[{"cause": "stale ref", "confidence": 0.7, "evidence": ["E1"]}],
            recommended_checks=["re-fetch origin/main"],
            can_self_recover=True,
        ))
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="experiment",
            subject_id="exp-1",
            failure={"status": "failed", "reason": "stale checkout"},
            evidence=[{"event": "git_fetch_failed", "level": "ERROR", "phase": "adoption"}],
        )

        self.assertEqual(analysis.category, "lifecycle_logic")
        self.assertEqual(analysis.confidence, 0.8)
        self.assertTrue(analysis.can_self_recover)
        self.assertEqual(len(analysis.likely_root_causes), 1)
        self.assertEqual(analysis.likely_root_causes[0].evidence, ("E1",))

        stored = service.get(analysis.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["summary"], "the checkout is stale")

        recent = service.list_recent(subject_type="experiment", subject_id="exp-1")
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["id"], analysis.id)


    def test_missing_subject_falls_back_without_raising(self):
        client = _FakeClient(_diagnosis_json())
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(subject_type="", subject_id="", failure="boom")
        self.assertEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertTrue(analysis.needs_human)


class MalformedOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_non_json_output_degrades_gracefully(self):
        client = _FakeClient("I cannot help with that, sorry.")
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(subject_type="tool", subject_id="t-1", failure="tool crashed")

        self.assertEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertTrue(analysis.needs_human)
        self.assertIsNotNone(service.get(analysis.id))

    def test_non_object_json_degrades_gracefully(self):
        client = _FakeClient("[1, 2, 3]")
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(subject_type="tool", subject_id="t-2", failure="tool crashed")
        self.assertEqual(analysis.category, CATEGORY_UNAVAILABLE)

    def test_client_exception_does_not_propagate(self):
        client = _FakeClient(RuntimeError("connection refused"))
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(subject_type="tool", subject_id="t-3", failure="boom")
        self.assertEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertIn("connection refused", analysis.escalation_reason)

    def test_malformed_output_does_not_lose_original_failure_state(self):
        # The analyzer is best-effort scaffolding: whatever a caller was
        # about to do with the *original* failure (e.g. mark an approval
        # needs_review) must be entirely unaffected by the analyzer itself
        # blowing up. analyze() must simply return without raising.
        client = _FakeClient(RuntimeError("boom"))
        service = _service(self.tmp, client)
        self.addCleanup(service.close)
        try:
            service.analyze(subject_type="x", subject_id="y", failure="z")
        except Exception as exc:  # pragma: no cover - defensive
            self.fail(f"analyze() must never raise, got {exc!r}")


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_low_confidence_forces_human_escalation(self):
        client = _FakeClient(_diagnosis_json(confidence=0.1))
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(subject_type="t", subject_id="s", failure="x")
        self.assertTrue(analysis.needs_human)
        self.assertIn("confidence", analysis.escalation_reason)

    def test_explicit_escalation_flags_are_respected(self):
        client = _FakeClient(_diagnosis_json(
            confidence=0.9, needs_external_model=True,
            escalation_reason="conflicting CI and git state",
        ))
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(subject_type="t", subject_id="s", failure="x")
        self.assertTrue(analysis.needs_external_model)
        self.assertFalse(analysis.needs_human)
        self.assertEqual(analysis.escalation_reason, "conflicting CI and git state")


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_secrets_never_reach_the_prompt_or_the_stored_result(self):
        client = _FakeClient(_diagnosis_json(summary=f"leaked api_key={SECRET} /agent/private.py"))
        service = _service(self.tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="t",
            subject_id="s",
            failure={"error": f"token={SECRET} at /agent/private/x.py"},
            evidence=[{"event": "e", "level": "ERROR", "secret": f"AGENT_API_KEY={SECRET}"}],
            goal="do the thing, token=" + SECRET,
        )

        serialized = json.dumps(analysis.to_dict())
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("/agent", serialized)

        prompt = client.prompts[0]
        self.assertNotIn(SECRET, prompt)
        self.assertNotIn("/agent", prompt)

        stored = json.dumps(service.get(analysis.id))
        self.assertNotIn(SECRET, stored)


class EvidenceFromLogEntriesTests(unittest.TestCase):
    def test_prefers_warning_error_and_decision_shaped_events(self):
        entries = [
            {"timestamp": "1", "level": "INFO", "event": "adoption_started", "phase": "adoption"},
            {"timestamp": "2", "level": "INFO", "event": "adoption_diff_captured", "phase": "adoption"},
            {"timestamp": "3", "level": "WARNING", "event": "ci_evidence_rejected", "rejection_reason": "target_base_moved"},
            {"timestamp": "4", "level": "INFO", "event": "adoption_resumed_from_commit", "phase": "adoption"},
        ]
        bundle = evidence_from_log_entries(entries)
        events = [e["event"] for e in bundle]
        self.assertEqual(events, ["ci_evidence_rejected"])

    def test_falls_back_to_recent_entries_when_nothing_is_relevant(self):
        entries = [{"timestamp": str(i), "level": "INFO", "event": "progress"} for i in range(5)]
        bundle = evidence_from_log_entries(entries, max_items=3)
        self.assertEqual(len(bundle), 3)
        self.assertEqual([e["timestamp"] for e in bundle], ["2", "3", "4"])

    def test_bounds_item_and_field_counts(self):
        entries = [
            {
                "timestamp": str(i), "level": "ERROR", "event": f"e{i}",
                **{f"field{n}": "x" * 500 for n in range(20)},
            }
            for i in range(100)
        ]
        bundle = evidence_from_log_entries(entries, max_items=10)
        self.assertEqual(len(bundle), 10)
        for entry in bundle:
            # timestamp/level/phase/event + at most _MAX_EVIDENCE_EXTRA_FIELDS extras
            self.assertLessEqual(len(entry), 4 + 8)
            for value in entry.values():
                if isinstance(value, str):
                    self.assertLessEqual(len(value), 300)

    def test_render_evidence_line_matches_structured_log_vocabulary(self):
        line = render_evidence_line(2, {"event": "ci_evidence_rejected", "rejection_reason": "target_base_moved"})
        self.assertEqual(line, "[E2] ci_evidence_rejected rejection_reason=target_base_moved")


class AnalyzeFromLogsTests(unittest.TestCase):
    """analyze_from_logs bridges real ExperimentLogger JSONL output."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log_dir = self.tmp / "logs"

    def test_reads_and_merges_experiment_and_task_logs(self):
        elog = ExperimentLogger("exp1234567890abcdef1234567890ab", mailbox_task_id="task1234567890abcdef1234567890a", log_dir=self.log_dir)
        elog.info("adoption", "adoption_started", "starting")
        elog.warning("adoption", "ci_evidence_rejected", "not authoritative", rejection_reason="target_base_moved")

        client = _FakeClient(_diagnosis_json(summary="stale base", category="lifecycle_logic"))
        service = FailureAnalysisService(db_path=str(self.tmp / "agent.db"), llama_client=client, log_dir=self.log_dir)
        self.addCleanup(service.close)

        analysis = service.analyze_from_logs(
            subject_type="task_finalization",
            subject_id="exp1234567890abcdef1234567890ab",
            experiment_id="exp1234567890abcdef1234567890ab",
            mailbox_task_id="task1234567890abcdef1234567890a",
            failure={"status": "needs_review"},
        )
        self.assertEqual(analysis.category, "lifecycle_logic")
        prompt = client.prompts[0]
        self.assertIn("ci_evidence_rejected", prompt)
        self.assertIn("target_base_moved", prompt)

    def test_missing_log_directory_yields_empty_evidence_not_a_crash(self):
        client = _FakeClient(_diagnosis_json())
        service = FailureAnalysisService(
            db_path=str(self.tmp / "agent.db"), llama_client=client,
            log_dir=self.tmp / "does-not-exist",
        )
        self.addCleanup(service.close)
        analysis = service.analyze_from_logs(
            subject_type="mailbox_task", subject_id="a1", experiment_id="exp-1", failure="x",
        )
        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertEqual(analysis.evidence_refs, ())


class Issue115Issue116RegressionFixtureTests(unittest.TestCase):
    """Regression fixture from the real #115/#116 sequence (see issue #145).

    Reproduces the exact structured-log sequence quoted in the issue and
    checks the service correctly threads it through to the model as bounded,
    labelled evidence, and that a diagnosis naming target_base_moved / the
    missing integration fallback is parsed, escalation-free, and persisted
    end to end.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log_dir = self.tmp / "logs"
        self.experiment_id = "e115116abcdef1234567890abcdef12"

    def _write_fixture_logs(self) -> None:
        elog = ExperimentLogger(self.experiment_id, log_dir=self.log_dir)
        elog.info(
            "assessment", "ci_evidence_lookup",
            "Looking up authoritative CI evidence for the verified checkpoint",
            checkpoint_sha="deadbeef", persisted_found=True,
        )
        elog.warning(
            "assessment", "ci_evidence_rejected",
            "Persisted CI evidence exists but is not authoritative for this integration",
            rejection_reason="target_base_moved", checkpoint_sha="deadbeef",
        )
        elog.warning(
            "assessment", "ci_evidence_live_result",
            "Live GitHub CI lookup finished",
            ci_status="unavailable", reason="integration fallback required",
        )
        elog.warning(
            "assessment", "stale_adoption_assessment",
            "Assessed whether the recorded adoption failure still stands",
            retryable=False, category="no_ci_evidence",
        )
        elog.warning(
            "finalization", "finalization_blocked",
            "Finalization stays in needs_review; the recorded adoption failure was not superseded",
            finalization_phase="needs_review",
        )

    def test_diagnosis_identifies_moved_base_and_recommends_integration_fallback(self):
        self._write_fixture_logs()

        scripted_response = _diagnosis_json(
            summary=(
                "The original CI result is valid for the old base but not "
                "current main; stale-adoption recovery stops before the "
                "integration fallback runs."
            ),
            category="lifecycle_logic",
            confidence=0.86,
            likely_root_causes=[{
                "cause": "moved-base integration fallback is not entered from stale-adoption assessment",
                "confidence": 0.9,
                "evidence": ["E2", "E4", "E5"],
            }],
            missing_information=[],
            recommended_checks=[
                "inspect ExperimentAdoptionService.assess_failure handling of target_base_moved",
                "inspect/run the integration fallback path",
            ],
            recommended_actions=[
                {"type": "inspect_code", "target": "ExperimentAdoptionService.assess_failure"},
                {"type": "run_check", "target": "integration fallback"},
            ],
            can_self_recover=True,
            needs_external_model=False,
            needs_human=False,
        )
        client = _FakeClient(scripted_response)
        service = FailureAnalysisService(db_path=str(self.tmp / "agent.db"), llama_client=client, log_dir=self.log_dir)
        self.addCleanup(service.close)

        analysis = service.analyze_from_logs(
            subject_type="task_finalization",
            subject_id=self.experiment_id,
            experiment_id=self.experiment_id,
            failure={
                "status": "needs_review",
                "phase": "finalization",
                "reason": "finalization retries exhausted after adoption status=failed",
            },
        )

        # The evidence bundle handed to the model carries every fixture
        # event (all are WARNING or ci_evidence*/assess*/block*-shaped) in
        # the same compact vocabulary the issue itself quotes.
        prompt = client.prompts[0]
        for expected in (
            "ci_evidence_lookup", "ci_evidence_rejected", "target_base_moved",
            "ci_evidence_live_result", "stale_adoption_assessment", "finalization_blocked",
        ):
            self.assertIn(expected, prompt)

        self.assertEqual(analysis.category, "lifecycle_logic")
        self.assertIn("target_base_moved", " ".join(c.cause for c in analysis.likely_root_causes) + prompt)
        self.assertTrue(
            any("integration fallback" in check for check in analysis.recommended_checks)
        )
        self.assertTrue(analysis.can_self_recover)
        self.assertFalse(analysis.needs_human)
        self.assertFalse(analysis.needs_external_model)
        self.assertGreaterEqual(analysis.confidence, 0.8)

        stored = service.get(analysis.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["category"], "lifecycle_logic")
        self.assertEqual(service.list_recent(limit=1, subject_type="task_finalization", subject_id=self.experiment_id)[0]["id"], analysis.id)


class AsyncEntryPointTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the PR #146 review finding.

    ``FailureAnalysisService`` is documented (#145) as reusable from
    anywhere in Nexus, including a future async orchestrator/tool-call
    path. The sync ``analyze()``/``analyze_from_logs()`` wrap
    ``asyncio.run()``, which raises ``RuntimeError: asyncio.run() cannot
    be called from a running event loop`` when invoked from code that
    already has one - and the outer try/except in the old implementation
    swallowed that into a misleading ``analysis_unavailable`` record.
    ``analyze_async()``/``analyze_from_logs_async()`` are the fix: the core
    logic never calls ``asyncio.run()`` itself, so an async caller awaits
    it directly instead. ``IsolatedAsyncioTestCase`` runs each test body
    inside a running event loop (via its own internal ``asyncio.run``),
    which is exactly the situation that used to break.
    """

    async def test_analyze_async_returns_a_real_diagnosis_from_a_running_loop(self):
        tmp = Path(tempfile.mkdtemp())
        client = _FakeClient(_diagnosis_json(summary="ok from a running loop", category="lifecycle_logic"))
        service = _service(tmp, client)
        self.addCleanup(service.close)

        analysis = await service.analyze_async(
            subject_type="tool", subject_id="t-async-1", failure="boom",
            evidence=[{"event": "e", "level": "ERROR"}],
        )

        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertEqual(analysis.category, "lifecycle_logic")
        self.assertEqual(analysis.summary, "ok from a running loop")
        self.assertIsNotNone(service.get(analysis.id))

    async def test_analyze_from_logs_async_returns_a_real_diagnosis_from_a_running_loop(self):
        tmp = Path(tempfile.mkdtemp())
        log_dir = tmp / "logs"
        elog = ExperimentLogger("expasync1234567890abcdef1234567", log_dir=log_dir)
        elog.warning("adoption", "ci_evidence_rejected", "not authoritative", rejection_reason="target_base_moved")

        client = _FakeClient(_diagnosis_json(category="lifecycle_logic"))
        service = FailureAnalysisService(db_path=str(tmp / "agent.db"), llama_client=client, log_dir=log_dir)
        self.addCleanup(service.close)

        analysis = await service.analyze_from_logs_async(
            subject_type="task_finalization",
            subject_id="expasync1234567890abcdef1234567",
            experiment_id="expasync1234567890abcdef1234567",
            failure={"status": "needs_review"},
        )

        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)
        self.assertIn("ci_evidence_rejected", client.prompts[0])

    def test_sync_analyze_still_works_with_no_event_loop_running(self):
        # The existing sync mailbox/API call sites must keep working.
        tmp = Path(tempfile.mkdtemp())
        client = _FakeClient(_diagnosis_json(category="lifecycle_logic"))
        service = _service(tmp, client)
        self.addCleanup(service.close)

        analysis = service.analyze(subject_type="tool", subject_id="t-sync-1", failure="boom")
        self.assertNotEqual(analysis.category, CATEGORY_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
