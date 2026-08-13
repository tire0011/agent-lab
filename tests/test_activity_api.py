import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api


SECRET = "super-secret-value"


def experiment(**changes):
    values = {
        "id": "3ef5b935",
        "goal": f"user input prompt {SECRET}",
        "status": "failed",
        "created_at": "2026-08-13T12:00:00+00:00",
        "ended_at": "2026-08-13T12:01:00+00:00",
        "test_command": ["/agent/.venv/bin/python", "-m", "unittest", "discover", "-s", "tests"],
        "test_exit_code": 1,
        "test_output": f"API_KEY={SECRET}\n/agent/private/file.py failed\nAssertionError: bad",
        "git_status": " M modules/private/worker.py\n?? /tmp/raw/token.txt\n",
        "error": f"exception at /agent/private.py token={SECRET}",
        "policy_rule": None,
        "research_started_at": "2026-08-13T11:58:00+00:00",
        "research_ended_at": "2026-08-13T11:58:30+00:00",
        "selection_started_at": "2026-08-13T11:58:30+00:00",
        "selection_ended_at": "2026-08-13T11:59:00+00:00",
        "implementation_started_at": "2026-08-13T11:59:00+00:00",
        "implementation_ended_at": "2026-08-13T12:00:00+00:00",
        "research_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "selection_usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        "reformat_retry_usage": None,
        "policy_replan_usage": None,
        "implementation_usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    }
    values.update(changes)
    obj = SimpleNamespace(**values)
    obj.to_dict = lambda: dict(values)
    return obj


class ActivityApiTests(unittest.TestCase):
    def setUp(self):
        self.exp = experiment()
        self.service = MagicMock()
        self.service.get_experiment.return_value = self.exp
        self.service.resolve_public_experiment.return_value = self.exp
        self.service.resolve_experiment.return_value = self.exp
        self.service.mailbox_task_id_for_experiment.return_value = "mailbox-7"
        self.service.list_experiments.return_value = [self.exp]
        self.service.list_activity.return_value = [{
            "id": 9,
            "timestamp": "2026-08-13T12:01:00+00:00",
            "entity_type": "experiment",
            "entity_id": self.exp.id,
            "event": "status_forced",
            "from_status": None,
            "to_status": "failed",
            "message": f"raw command output {SECRET} /agent/private.py",
        }]
        codex = MagicMock()
        mailbox = MagicMock()
        mailbox.recover_after_restart.return_value = None
        mailbox.get.return_value = SimpleNamespace(experiment_id=self.exp.id)
        api.app.dependency_overrides[api.get_experiment_service] = lambda: self.service
        api.app.dependency_overrides[api.get_codex_service] = lambda: codex
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        self.client = TestClient(api.app)
        self.addCleanup(api.app.dependency_overrides.clear)

    def test_public_activity_requires_no_key_and_uses_strict_allowlist(self):
        response = self.client.get("/activity/public")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.service.list_activity.assert_called_with(limit=100)
        self.assertEqual(set(body["activity"][0]), {
            "timestamp", "phase", "status", "message", "mailbox_task_id",
            "experiment_id", "failure_reason", "test_exit_code", "changed_files",
            "test_summary",
        })
        self.assertEqual(body["activity"][0]["changed_files"], ["worker.py", "token.txt"])
        serialized = json.dumps(body)
        for forbidden in (SECRET, "raw command", "/agent", "user input", "AssertionError", "API_KEY"):
            self.assertNotIn(forbidden, serialized)

    def test_public_experiment_requires_no_key_and_never_exposes_raw_fields(self):
        response = self.client.get(f"/experiments/{self.exp.id}/public")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("goal", body)
        self.assertNotIn("test_output", body)
        self.assertNotIn("error", body)
        self.assertNotIn(SECRET, json.dumps(body))
        self.service.resolve_public_experiment.assert_called_once_with("3ef5b935")

    def test_authenticated_activity_has_filters_and_bounded_sanitized_output(self):
        self.exp.test_output = "\n".join([f"line {i}" for i in range(105)] + [f"token={SECRET} /agent/x.py"])
        response = self.client.get(
            f"/activity?experiment_id={self.exp.id}&mailbox_task_id=mailbox-7",
            headers={"x-api-key": "test-key"},
        )
        self.assertEqual(response.status_code, 200)
        entry = response.json()["activity"][0]
        self.assertLessEqual(len(entry["test_output_lines"]), 100)
        entry_without_test_command = {k: v for k, v in entry.items() if k != "test_command"}
        serialized = json.dumps(entry_without_test_command)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("/agent", serialized)
        self.assertIsNone(entry["message"])

    def test_authenticated_endpoints_still_require_key(self):
        self.assertEqual(self.client.get("/activity").status_code, 401)
        self.assertEqual(self.client.get(f"/experiments/{self.exp.id}").status_code, 401)

    def test_authenticated_experiment_resolves_full_id_prefix(self):
        response = self.client.get(
            f"/experiments/{self.exp.id}",
            headers={"x-api-key": "test-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.service.resolve_experiment.assert_called_once_with(self.exp.id)
        self.assertEqual(response.json()["id"], self.exp.id)

    def test_authenticated_experiment_ambiguous_prefix_returns_404(self):
        from modules.experiment_service import ExperimentNotFoundError

        self.service.resolve_experiment.side_effect = ExperimentNotFoundError("ambiguous")
        response = self.client.get(
            "/experiments/3ef5b935",
            headers={"x-api-key": "test-key"},
        )
        self.assertEqual(response.status_code, 404)

    def test_authenticated_activity_includes_rejection_diagnostics(self):
        rejected = experiment(
            status="rejected",
            error=f"plan mentions disallowed keyword: 'push' token={SECRET} /agent/private.py",
            policy_rule="disallowed_keyword",
        )
        self.service.get_experiment.return_value = rejected
        self.service.list_activity.return_value = [{
            "id": 10,
            "timestamp": "2026-08-13T12:01:00+00:00",
            "entity_type": "experiment",
            "entity_id": rejected.id,
            "event": "status_changed",
            "from_status": "selecting",
            "to_status": "rejected",
            "message": f"raw command output {SECRET} /agent/private.py",
        }]
        response = self.client.get(
            f"/activity?experiment_id={rejected.id}",
            headers={"x-api-key": "test-key"},
        )
        self.assertEqual(response.status_code, 200)
        entry = response.json()["activity"][0]
        self.assertEqual(entry["phase"], "needs_review")
        self.assertEqual(entry["policy_rule"], "disallowed_keyword")
        self.assertIsNotNone(entry["rejection_reason"])
        self.assertEqual(entry["test_command"], rejected.test_command)
        self.assertEqual(entry["changed_files"], ["worker.py", "token.txt"])
        entry_without_test_command = {k: v for k, v in entry.items() if k != "test_command"}
        serialized = json.dumps(entry_without_test_command)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("/agent", serialized)

    def test_website_loads_public_activity_every_ten_seconds(self):
        html = self.client.get("/").text
        js = self.client.get("/static/app.js").text
        self.assertIn("public-activity-content", html)
        self.assertIn('requestJson("/activity/public")', js)
        self.assertIn("setInterval(loadPublicActivity, 10000)", js)

    def test_website_loads_diagnostics_every_ten_seconds(self):
        html = self.client.get("/").text
        js = self.client.get("/static/app.js").text
        self.assertIn("diagnostics-content", html)
        self.assertIn('requestJson("/diagnostics/public")', js)
        self.assertIn("setInterval(loadDiagnostics, 10000)", js)


if __name__ == "__main__":
    unittest.main()
