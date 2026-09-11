"""Tests for publishing failure analyses to the mailbox outbox (#148).

Covers the acceptance criteria from the issue that live at the
``FailureAnalysisService`` level (content, gating, unavailable-analysis
notifications, and publish failures never touching the underlying
diagnosis). The shared git commit/push lifecycle itself (write -> commit ->
push, restart idempotency, retry-after-failure) is covered by
``TestFailureAnalysisPublishLifecycle`` in ``tests/test_outbox_publisher.py``,
reusing the same ``FailureAnalysisOutboxPublisher`` the other two producers
already share.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from modules.failure_analysis_service import (
    CATEGORY_UNAVAILABLE,
    FailureAnalysisService,
    render_outbox_content,
)
from modules.mailbox.config import MailboxConfig
from modules.outbox_publisher import PUBLISH_WRITTEN


SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


class _FakeClient:
    model = "test-local-model"

    def __init__(self, response: str | Exception):
        self._response = response
        self.prompts: list[str] = []

    async def complete(self, *, prompt, system, temperature, max_tokens):
        self.prompts.append(prompt)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


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


def _outbox_files(outbox_dir: Path) -> list[Path]:
    if not outbox_dir.is_dir():
        return []
    return sorted(outbox_dir.glob("*.md"))


class OutboxPublishGatingTests(unittest.TestCase):
    """Only analyses correlated to a mailbox task / experiment are published."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = MailboxConfig.legacy(self.tmp)

    def _service(self, client) -> FailureAnalysisService:
        return FailureAnalysisService(
            db_path=str(self.tmp / "agent.db"), llama_client=client, mailbox_config=self.cfg,
        )

    def test_outbox_message_published_for_mailbox_task_analysis(self):
        client = _FakeClient(_diagnosis_json(
            summary="Target branch moved after checkpoint CI verification.",
            category="lifecycle_logic",
            confidence=0.86,
            likely_root_causes=[{
                "cause": "moved-base fallback is not entered from stale-adoption assessment",
                "confidence": 0.9, "evidence": ["E1"],
            }],
            recommended_checks=["inspect assess_failure handling of target_base_moved"],
            can_self_recover=True,
        ))
        service = self._service(client)
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="task_finalization",
            subject_id="exp-1",
            failure={"status": "needs_review"},
            experiment_id="exp-1",
            mailbox_task_id="task-1",
        )

        files = _outbox_files(self.cfg.outbox_dir)
        self.assertEqual(len(files), 1)
        content = files[0].read_text(encoding="utf-8")

        self.assertIn("type: failure_analysis\n", content)
        self.assertIn(f"analysis_id: {analysis.id}\n", content)
        self.assertIn("experiment_id: exp-1\n", content)
        self.assertIn("mailbox_task_id: task-1\n", content)
        self.assertIn("Summary: Target branch moved after checkpoint CI verification.", content)
        self.assertIn("Category: lifecycle_logic", content)
        self.assertIn("Confidence: 0.86", content)
        self.assertIn("Likely cause: moved-base fallback is not entered from stale-adoption assessment", content)
        self.assertIn("Next checks:\n- inspect assess_failure handling of target_base_moved", content)
        self.assertIn("Self recoverable: yes", content)
        self.assertIn("External model recommended: no", content)
        self.assertIn("Human required: no", content)
        self.assertIn(f"Analysis id: {analysis.id}", content)
        self.assertIn("Experiment: exp-1", content)
        self.assertIn("Task: task-1", content)

        stored = service.get(analysis.id)
        self.assertEqual(stored["experiment_id"], "exp-1")
        self.assertEqual(stored["mailbox_task_id"], "task-1")

    def test_outbox_message_published_when_only_experiment_id_present(self):
        client = _FakeClient(_diagnosis_json(category="transient"))
        service = self._service(client)
        self.addCleanup(service.close)

        service.analyze(
            subject_type="mailbox_task", subject_id="exp-2", failure="x",
            experiment_id="exp-2",
        )

        self.assertEqual(len(_outbox_files(self.cfg.outbox_dir)), 1)

    def test_no_outbox_message_without_mailbox_or_experiment_correlation(self):
        client = _FakeClient(_diagnosis_json())
        service = self._service(client)
        self.addCleanup(service.close)

        analysis = service.analyze(subject_type="tool", subject_id="t-1", failure="boom")

        self.assertEqual(_outbox_files(self.cfg.outbox_dir), [])
        stored = service.get(analysis.id)
        self.assertIsNone(stored.get("experiment_id"))
        self.assertIsNone(stored.get("mailbox_task_id"))

    def test_no_outbox_message_when_service_has_no_outbox_configuration(self):
        # Manual/API construction site: no mailbox_config, no outbox_dir/repo_root.
        client = _FakeClient(_diagnosis_json())
        service = FailureAnalysisService(db_path=str(self.tmp / "agent2.db"), llama_client=client)
        self.addCleanup(service.close)

        service.analyze(
            subject_type="mailbox_task", subject_id="exp-3", failure="x",
            experiment_id="exp-3", mailbox_task_id="task-3",
        )

        # Nothing should have been written anywhere, and no exception raised.
        self.assertEqual(_outbox_files(self.cfg.outbox_dir), [])


class UnavailableAnalysisOutboxTests(unittest.TestCase):
    """AC: analysis_unavailable also emits a concise notification, without
    hiding the original error."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = MailboxConfig.legacy(self.tmp)

    def test_unavailable_analysis_publishes_a_notification_naming_the_reason(self):
        client = _FakeClient(RuntimeError("local model unreachable: connection refused"))
        service = FailureAnalysisService(
            db_path=str(self.tmp / "agent.db"), llama_client=client, mailbox_config=self.cfg,
        )
        self.addCleanup(service.close)

        analysis = service.analyze(
            subject_type="mailbox_task", subject_id="exp-4", failure="original failure detail",
            experiment_id="exp-4", mailbox_task_id="task-4",
        )
        self.assertEqual(analysis.category, CATEGORY_UNAVAILABLE)

        files = _outbox_files(self.cfg.outbox_dir)
        self.assertEqual(len(files), 1)
        content = files[0].read_text(encoding="utf-8")

        self.assertIn("category: analysis_unavailable\n", content)
        self.assertIn("Failure Analysis Unavailable", content)
        self.assertIn("did not complete", content)
        self.assertIn("original failure/result is unaffected", content)
        self.assertIn("local model unreachable: connection refused", content)
        self.assertIn(f"Analysis id: {analysis.id}", content)
        self.assertIn("Experiment: exp-4", content)
        self.assertIn("Task: task-4", content)


class RedactionInOutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = MailboxConfig.legacy(self.tmp)

    def test_outbox_content_never_contains_secrets(self):
        client = _FakeClient(_diagnosis_json(
            summary=f"leaked api_key={SECRET} /agent/private.py",
            recommended_checks=[f"check token={SECRET}"],
        ))
        service = FailureAnalysisService(
            db_path=str(self.tmp / "agent.db"), llama_client=client, mailbox_config=self.cfg,
        )
        self.addCleanup(service.close)

        service.analyze(
            subject_type="mailbox_task", subject_id="exp-5", failure="x",
            experiment_id="exp-5", mailbox_task_id="task-5",
        )

        files = _outbox_files(self.cfg.outbox_dir)
        self.assertEqual(len(files), 1)
        content = files[0].read_text(encoding="utf-8")
        self.assertNotIn(SECRET, content)
        self.assertNotIn("/agent", content)


class PublishFailureDoesNotAffectAnalysisTests(unittest.TestCase):
    """AC: publishing failure must never alter or break the underlying
    lifecycle/failure state, and must not raise."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = MailboxConfig.legacy(self.tmp)

    def test_write_failure_is_swallowed_and_analysis_is_unaffected(self):
        client = _FakeClient(_diagnosis_json(category="lifecycle_logic", confidence=0.77))
        service = FailureAnalysisService(
            db_path=str(self.tmp / "agent.db"), llama_client=client, mailbox_config=self.cfg,
        )
        self.addCleanup(service.close)

        def _boom(self, analysis):
            raise OSError("disk full")

        service._write_outbox_file = _boom.__get__(service, FailureAnalysisService)

        try:
            analysis = service.analyze(
                subject_type="mailbox_task", subject_id="exp-6", failure="x",
                experiment_id="exp-6", mailbox_task_id="task-6",
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.fail(f"analyze() must never raise from a publish failure, got {exc!r}")

        self.assertEqual(analysis.category, "lifecycle_logic")
        self.assertEqual(analysis.confidence, 0.77)
        stored = service.get(analysis.id)
        self.assertEqual(stored["category"], "lifecycle_logic")
        self.assertEqual(stored["confidence"], 0.77)
        # No outbox row was recorded since the write itself never happened.
        row = service._conn.execute(
            "SELECT outbox_file, publish_status FROM failure_analyses WHERE id=?",
            (analysis.id,),
        ).fetchone()
        self.assertIsNone(row["outbox_file"])
        self.assertIsNone(row["publish_status"])


class RenderOutboxContentUnitTests(unittest.TestCase):
    """Direct unit coverage of the content renderer independent of the LLM."""

    def test_falls_back_to_unknown_cause_and_placeholder_checks(self):
        from modules.failure_analysis_service import FailureAnalysis

        analysis = FailureAnalysis(
            id="abcdef1234567890",
            subject_type="tool",
            subject_id="t-1",
            created_at="2026-01-01T00:00:00.000Z",
            provider="local",
            model="m",
            summary="something broke",
            category="unknown",
            confidence=0.5,
            experiment_id=None,
            mailbox_task_id="task-x",
        )
        content = render_outbox_content(analysis)
        self.assertIn("Likely cause: unknown", content)
        self.assertIn("- (none identified)", content)
        self.assertIn("Experiment: none", content)
        self.assertIn("Task: task-x", content)


if __name__ == "__main__":
    unittest.main()
