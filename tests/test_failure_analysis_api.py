"""Tests for the manual/debug failure-analysis endpoints (#145)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api


def _entry(**overrides):
    payload = {
        "id": "a1",
        "subject_type": "task_finalization",
        "subject_id": "exp-1",
        "created_at": "2026-09-10T00:00:00Z",
        "provider": "local",
        "model": "qwen2.5-coder-7b-instruct",
        "summary": "target branch moved after checkpoint CI verification",
        "category": "lifecycle_logic",
        "confidence": 0.86,
        "likely_root_causes": [
            {"cause": "moved-base fallback not entered", "confidence": 0.9, "evidence": ["E2"]}
        ],
        "missing_information": [],
        "recommended_checks": ["inspect integration fallback entry point"],
        "recommended_actions": [{"type": "inspect_code", "target": "ExperimentAdoptionService"}],
        "can_self_recover": True,
        "needs_external_model": False,
        "needs_human": False,
        "escalation_reason": None,
        "evidence_refs": ["E1", "E2"],
    }
    payload.update(overrides)
    return payload


class FailureAnalysisApiTests(unittest.TestCase):
    def setUp(self):
        self.service = MagicMock()
        api.app.dependency_overrides[api.get_failure_analysis_service] = lambda: self.service
        self.client = TestClient(api.app)
        self.addCleanup(api.app.dependency_overrides.clear)

    def test_list_requires_api_key(self):
        response = self.client.get("/diagnostics/failure-analyses")
        self.assertEqual(response.status_code, 401)

    def test_list_returns_stored_analyses(self):
        self.service.list_recent.return_value = [_entry()]
        response = self.client.get(
            "/diagnostics/failure-analyses", headers={"x-api-key": "test-key"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["analyses"][0]["category"], "lifecycle_logic")

    def test_list_forwards_filters(self):
        self.service.list_recent.return_value = []
        self.client.get(
            "/diagnostics/failure-analyses?subject_type=mailbox_task&subject_id=x&limit=5",
            headers={"x-api-key": "test-key"},
        )
        self.service.list_recent.assert_called_with(limit=5, subject_type="mailbox_task", subject_id="x")

    def test_get_by_id_404_when_missing(self):
        self.service.get.return_value = None
        response = self.client.get(
            "/diagnostics/failure-analyses/does-not-exist", headers={"x-api-key": "test-key"}
        )
        self.assertEqual(response.status_code, 404)

    def test_get_by_id_returns_entry(self):
        self.service.get.return_value = _entry(id="a2")
        response = self.client.get(
            "/diagnostics/failure-analyses/a2", headers={"x-api-key": "test-key"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "a2")

    def test_trigger_requires_experiment_or_mailbox_task_id(self):
        response = self.client.post(
            "/diagnostics/failure-analyses/analyze",
            json={"subject_type": "manual"},
            headers={"x-api-key": "test-key"},
        )
        self.assertEqual(response.status_code, 400)
        self.service.analyze_from_logs.assert_not_called()

    def test_trigger_calls_analyze_from_logs(self):
        from modules.failure_analysis_service import FailureAnalysis

        self.service.analyze_from_logs.return_value = FailureAnalysis(
            id="a3", subject_type="manual", subject_id="exp-9", created_at="t",
            provider="local", model="m", summary="s", category="c", confidence=0.5,
        )
        response = self.client.post(
            "/diagnostics/failure-analyses/analyze",
            json={"subject_type": "manual", "experiment_id": "exp-9", "reason": "debugging"},
            headers={"x-api-key": "test-key"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["id"], "a3")
        kwargs = self.service.analyze_from_logs.call_args.kwargs
        self.assertEqual(kwargs["subject_type"], "manual")
        self.assertEqual(kwargs["experiment_id"], "exp-9")
        self.assertEqual(kwargs["failure"]["reason"], "debugging")


if __name__ == "__main__":
    unittest.main()
