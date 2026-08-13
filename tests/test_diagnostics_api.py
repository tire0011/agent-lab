import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from tests.test_activity_api import SECRET, experiment


class DiagnosticsPublicApiTests(unittest.TestCase):
    def setUp(self):
        self.failed = experiment(
            id="3ef5b935abcd1234",
            status="failed",
            test_exit_code=1,
        )
        self.rejected = experiment(
            id="9ab1c2d3e4f56789",
            status="rejected",
            error=f"plan mentions disallowed keyword: 'push' token={SECRET} /agent/private.py",
            policy_rule="disallowed_keyword",
            test_exit_code=None,
        )
        self.service = MagicMock()
        self.service.list_experiments.return_value = [self.failed, self.rejected]
        self.service.mailbox_task_id_for_experiment.return_value = "mailbox-7"
        self.service.list_activity.return_value = [{
            "message": f"raw diagnostic output token={SECRET} /agent/private/x.py",
        }]

        mailbox = MagicMock()
        mailbox.recover_after_restart.return_value = None
        mailbox.get.return_value = SimpleNamespace(experiment_id=self.failed.id)
        mailbox.list_pending.return_value = [SimpleNamespace(id="p1")]
        mailbox.list_recent.return_value = [
            SimpleNamespace(
                id="m1",
                status="approved",
                received_at="2026-08-13T11:00:00+00:00",
                experiment_id=self.failed.id,
            )
        ]

        api.app.dependency_overrides[api.get_experiment_service] = lambda: self.service
        api.app.dependency_overrides[api.get_codex_service] = lambda: MagicMock()
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        self.client = TestClient(api.app)
        self.addCleanup(api.app.dependency_overrides.clear)

    def test_public_diagnostics_requires_no_key(self):
        response = self.client.get("/diagnostics/public")
        self.assertEqual(response.status_code, 200)

    def test_public_diagnostics_allowlist_and_redaction(self):
        response = self.client.get("/diagnostics/public")
        body = response.json()
        serialized = json.dumps(body)
        forbidden = (
            SECRET, "/agent", "user input prompt",
            "AGENT_API_KEY", "AssertionError", "API_KEY",
        )
        for token in forbidden:
            self.assertNotIn(token, serialized)
        self.assertIn("service", body)
        self.assertIn("mailbox", body)
        self.assertIn("experiments", body)
        self.assertIn("problems", body)

    def test_public_diagnostics_limit_query_param(self):
        response = self.client.get("/diagnostics/public?limit=1")
        self.assertEqual(response.status_code, 200)
        self.service.list_experiments.assert_called_with(limit=1)
        response = self.client.get("/diagnostics/public?limit=200")
        self.assertEqual(response.status_code, 422)

    def test_failed_experiment_fully_diagnosable_from_public_endpoint(self):
        response = self.client.get("/diagnostics/public")
        body = response.json()
        entry = next(e for e in body["experiments"] if e["id"] == self.failed.id)
        self.assertEqual(entry["status"], "failed")
        self.assertIsNotNone(entry["test_command"])
        self.assertNotIn("/", entry["test_command"])
        self.assertEqual(entry["test_exit_code"], 1)
        self.assertIsNotNone(entry["failure_summary"])
        self.assertEqual(entry["changed_files"], ["worker.py", "token.txt"])
        self.assertTrue(len(entry["lifecycle"]) >= 1)
        for phase in entry["lifecycle"]:
            self.assertIsNotNone(phase["duration_ms"])
        self.assertIsNotNone(entry["usage"])

    def test_rejected_experiment_fully_diagnosable_from_public_endpoint(self):
        response = self.client.get("/diagnostics/public")
        body = response.json()
        entry = next(e for e in body["experiments"] if e["id"] == self.rejected.id)
        self.assertEqual(entry["status"], "rejected")
        self.assertEqual(entry["policy_rule"], "disallowed_keyword")
        self.assertIsNotNone(entry["rejection_reason"])
        self.assertNotIn(SECRET, entry["rejection_reason"])
        self.assertNotIn("/agent", entry["rejection_reason"])

    def test_mailbox_status_uses_allowlisted_fields_only(self):
        response = self.client.get("/diagnostics/public")
        body = response.json()
        self.assertEqual(body["mailbox"]["pending_count"], 1)
        task = body["mailbox"]["latest_tasks"][0]
        self.assertEqual(set(task), {"id", "status", "received_at", "experiment_id"})

    def test_service_status_includes_git_revision_and_deployment_time(self):
        response = self.client.get("/diagnostics/public")
        body = response.json()
        self.assertEqual(body["service"]["status"], "ok")
        self.assertIn("git_revision", body["service"])
        self.assertIn("deployment_time", body["service"])


if __name__ == "__main__":
    unittest.main()
