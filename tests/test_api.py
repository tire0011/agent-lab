import hashlib
import hmac
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import data_sources
import api
from api import (
    get_codex_service,
    get_experiment_adoption_service,
    get_mailbox_task_service,
    get_operations_service,
)
from modules.mailbox.poll_runner import MailboxPollRunResult


def _mock_codex_service():
    svc = MagicMock()
    svc.mark_interrupted_tasks_failed.return_value = None
    svc.list_tasks.return_value = []
    return svc


def _mock_mailbox_task_service():
    svc = MagicMock()
    svc.recover_after_restart.return_value = None
    svc.list_recent.return_value = []
    svc.list_pending.return_value = []
    return svc


def _mock_adoption_service():
    svc = MagicMock()
    svc.recover_after_restart.return_value = []
    svc.get.return_value = MagicMock(to_dict=lambda: {
        "id": None,
        "experiment_id": "exp-1",
        "status": "not_started",
        "target_branch": "main",
        "expected_files": [],
        "actual_files": [],
        "test_output_lines": [],
        "local_sync_pending": False,
    })
    return svc


def _mock_operations_service():
    svc = MagicMock()
    svc.catalog.return_value = []
    return svc


class ApiTestsBase(unittest.TestCase):
    def setUp(self) -> None:
        # Override heavy singletons so tests never touch the production DB
        # or spawn background workers.
        api.app.dependency_overrides[get_codex_service] = _mock_codex_service
        api.app.dependency_overrides[get_mailbox_task_service] = _mock_mailbox_task_service
        api.app.dependency_overrides[get_experiment_adoption_service] = _mock_adoption_service
        api.app.dependency_overrides[get_operations_service] = _mock_operations_service

        self.client = TestClient(api.app)
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(api.app.dependency_overrides.clear)

        self.runs_dir = Path(self.temp_dir.name) / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.ideas_path = Path(self.temp_dir.name) / "IDEAS.md"

        self._orig_runs_dir = data_sources.RUNS_DIR
        self._orig_ideas_path = data_sources.IDEAS_PATH
        data_sources.RUNS_DIR = self.runs_dir
        data_sources.IDEAS_PATH = self.ideas_path
        self.addCleanup(self._restore_paths)

    def _restore_paths(self) -> None:
        data_sources.RUNS_DIR = self._orig_runs_dir
        data_sources.IDEAS_PATH = self._orig_ideas_path

    def _write_run(self, name: str, response: str, timestamp: str = "2026-08-10T00:00:00+00:00") -> None:
        (self.runs_dir / name).write_text(
            json.dumps({"timestamp": timestamp, "response": response}),
            encoding="utf-8",
        )

    def _write_ideas_md(self, content: str) -> None:
        self.ideas_path.write_text(content, encoding="utf-8")


class HealthEndpointTests(ApiTestsBase):
    def test_health_returns_ok(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class MailboxGithubWebhookTests(ApiTestsBase):
    WEBHOOK_PATH = "/mailbox/webhook/github"

    def _signed_headers(self, body: bytes, *, secret: str, event: str = "push") -> dict[str, str]:
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return {
            "x-github-event": event,
            "x-hub-signature-256": f"sha256={digest}",
            "content-type": "application/json",
        }

    def test_push_with_inbox_addition_returns_202_and_triggers_once(self):
        payload = {"commits": [{"added": ["inbox/new-task.md"], "modified": [], "removed": []}]}
        body = json.dumps(payload).encode("utf-8")
        with patch.dict(os.environ, {api.GITHUB_WEBHOOK_SECRET_ENV: "webhook-secret"}, clear=False):
            with patch("api._start_mailbox_webhook_poll") as start_poll:
                response = self.client.post(self.WEBHOOK_PATH, data=body, headers=self._signed_headers(body, secret="webhook-secret"))
        self.assertEqual(response.status_code, 202)
        start_poll.assert_called_once()

    def test_wrong_or_missing_signature_is_rejected_without_trigger(self):
        payload = {"commits": [{"added": ["inbox/new-task.md"]}]}
        body = json.dumps(payload).encode("utf-8")
        with patch.dict(os.environ, {api.GITHUB_WEBHOOK_SECRET_ENV: "webhook-secret"}, clear=False):
            with patch("api._start_mailbox_webhook_poll") as start_poll:
                wrong_sig = {"x-github-event": "push", "x-hub-signature-256": "sha256=deadbeef", "content-type": "application/json"}
                wrong = self.client.post(self.WEBHOOK_PATH, data=body, headers=wrong_sig)
                missing = self.client.post(self.WEBHOOK_PATH, data=body, headers={"x-github-event": "push", "content-type": "application/json"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(missing.status_code, 401)
        start_poll.assert_not_called()

    def test_missing_webhook_secret_is_rejected_without_trigger(self):
        payload = {"commits": [{"added": ["inbox/new-task.md"]}]}
        body = json.dumps(payload).encode("utf-8")
        with patch.dict(os.environ, {api.GITHUB_WEBHOOK_SECRET_ENV: ""}, clear=False):
            with patch("api._start_mailbox_webhook_poll") as start_poll:
                response = self.client.post(self.WEBHOOK_PATH, data=body, headers={"x-github-event": "push"})
        self.assertEqual(response.status_code, 503)
        start_poll.assert_not_called()

    def test_invalid_json_is_rejected_without_trigger(self):
        body = b"{not-json"
        with patch.dict(os.environ, {api.GITHUB_WEBHOOK_SECRET_ENV: "webhook-secret"}, clear=False):
            with patch("api._start_mailbox_webhook_poll") as start_poll:
                response = self.client.post(
                    self.WEBHOOK_PATH,
                    data=body,
                    headers=self._signed_headers(body, secret="webhook-secret"),
                )
        self.assertEqual(response.status_code, 400)
        start_poll.assert_not_called()

    def test_ping_event_is_successful_without_trigger(self):
        body = json.dumps({"zen": "hi"}).encode("utf-8")
        with patch.dict(os.environ, {api.GITHUB_WEBHOOK_SECRET_ENV: "webhook-secret"}, clear=False):
            with patch("api._start_mailbox_webhook_poll") as start_poll:
                response = self.client.post(self.WEBHOOK_PATH, data=body, headers=self._signed_headers(body, secret="webhook-secret", event="ping"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pong")
        start_poll.assert_not_called()

    def test_push_without_inbox_added_or_modified_is_ignored(self):
        outbox_payload = {"commits": [{"added": ["outbox/result.md"], "modified": [], "removed": []}]}
        archive_payload = {"commits": [{"added": [], "modified": ["archive/old.md"], "removed": []}]}
        inbox_delete_payload = {"commits": [{"added": [], "modified": [], "removed": ["inbox/deleted.md"]}]}
        with patch.dict(os.environ, {api.GITHUB_WEBHOOK_SECRET_ENV: "webhook-secret"}, clear=False):
            with patch("api._start_mailbox_webhook_poll") as start_poll:
                for payload in (outbox_payload, archive_payload, inbox_delete_payload):
                    body = json.dumps(payload).encode("utf-8")
                    response = self.client.post(self.WEBHOOK_PATH, data=body, headers=self._signed_headers(body, secret="webhook-secret"))
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["status"], "ignored")
        start_poll.assert_not_called()

    def test_busy_poll_does_not_start_processing_twice(self):
        with patch(
            "api.run_mailbox_poll_once",
            return_value=MailboxPollRunResult(status="busy"),
        ) as shared_runner:
            api._run_mailbox_poll_once_for_webhook()
        shared_runner.assert_called_once()

    def test_rejection_logs_never_include_secret_or_payload_content(self):
        secret = "top-secret"
        payload_text = "inbox payload should not appear in logs"
        payload = {"commits": [{"added": ["inbox/new.md"]}], "body": payload_text}
        body = json.dumps(payload).encode("utf-8")
        bad_signature = "sha256=leaked-signature-value"
        headers = {"x-github-event": "push", "x-hub-signature-256": bad_signature, "content-type": "application/json"}
        with patch.dict(os.environ, {api.GITHUB_WEBHOOK_SECRET_ENV: secret}, clear=False):
            with self.assertLogs("api", level="WARNING") as logs:
                response = self.client.post(self.WEBHOOK_PATH, data=body, headers=headers)
        self.assertEqual(response.status_code, 401)
        serialized = "\n".join(logs.output)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(bad_signature, serialized)
        self.assertNotIn(payload_text, serialized)


class WebClientTests(ApiTestsBase):
    def test_index_returns_html(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn("Agent Lab", response.text)

    def test_stylesheet_is_reachable(self):
        response = self.client.get("/static/styles.css")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/css"))

    def test_javascript_is_reachable(self):
        response = self.client.get("/static/utils.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertIn("textContent", response.text)

    def test_experiment_detail_shows_full_id_and_copy_control(self):
        javascript = self.client.get("/static/tasks.js").text
        detail_block = javascript.split("async function showExperimentDetail(experimentId)", 1)[1].split(
            "taskElements.loadButton.addEventListener", 1
        )[0]
        self.assertIn('["Experiment-ID", fullExperimentId || "–"]', detail_block)
        self.assertIn('copyButton.textContent = "Kopieren"', detail_block)
        self.assertIn("copyTextToClipboard(fullExperimentId)", detail_block)
        self.assertNotIn("fullExperimentId.slice(0, 8)", detail_block)

    def test_public_experiment_detail_fetch_path_is_preserved(self):
        javascript = self.client.get("/static/tasks.js").text
        fetch_block = javascript.split("async function fetchExperimentSummary(experimentId)", 1)[1].split(
            "async function renderTaskApprovals(tasks)", 1
        )[0]
        self.assertIn(
            "const path = state.apiKey ? `/experiments/${experimentId}` : `/experiments/${experimentId}/public`;",
            fetch_block,
        )

    def test_experiment_detail_mobile_layout_rule_is_preserved(self):
        stylesheet = self.client.get("/static/styles.css").text
        mobile_rules = stylesheet.split("@media (max-width: 640px)", 1)[1]
        self.assertIn(".detail-list { grid-template-columns: 1fr; }", mobile_rules)

    def test_logs_status_contract(self):
        html = self.client.get("/").text
        self.assertIn('id="logs-visible-count"', html)
        self.assertIn('id="logs-last-successful-refresh"', html)
        self.assertIn("Sichtbar:", html)
        self.assertIn("Zuletzt erfolgreich aktualisiert:", html)

    def test_logs_render_and_filter_optional_chat_correlations(self):
        html = self.client.get("/").text
        javascript = self.client.get("/static/logs.js").text
        self.assertIn('id="logs-job-input"', html)
        self.assertIn('id="logs-session-input"', html)
        self.assertIn('addDetail("Chat-Job-ID", entry.job_id)', javascript)
        self.assertIn('addDetail("Session-ID", entry.session_id)', javascript)
        self.assertIn('if (jobId) params.set("job_id", jobId)', javascript)
        self.assertIn('if (sessionId) params.set("session_id", sessionId)', javascript)
        self.assertNotIn("setInterval(loadLogs", javascript)

    def test_logs_refresh_uses_rendered_arrays_and_shared_load_function(self):
        javascript = self.client.get("/static/logs.js").text
        self.assertIn("const visibleCount = renderedEntries.length;", javascript)
        self.assertIn("renderExperimentLogEntries(entries);\n      updateLogsSuccessStatus(entries);", javascript)
        self.assertIn('const baseUrl = `/logs?${params.toString()}`;', javascript)
        self.assertIn("await requestJson(baseUrl, { headers: authHeaders() });", javascript)
        self.assertIn("renderLogsFromDiagnostics(experiments);\n    updateLogsSuccessStatus(experiments);", javascript)
        self.assertNotIn("setInterval(loadLogs", javascript)
        self.assertIn('logsElements.refreshButton.addEventListener("click", loadLogs)', javascript)

    def test_logs_default_view_uses_unified_feed_not_activity_summaries(self):
        """The default "Alle Experimente" view must show real structured
        log entries (GET /logs) rather than lifecycle/status summaries
        (GET /activity)."""
        javascript = self.client.get("/static/logs.js").text
        self.assertNotIn("/activity?", javascript)
        self.assertNotIn("renderLogsFromActivity", javascript)

    def test_logs_success_timestamp_is_not_updated_in_error_paths(self):
        javascript = self.client.get("/static/logs.js").text
        load_logs = javascript.split("async function loadLogs()", 1)[1].split(
            'logsElements.refreshButton.addEventListener("click", loadLogs)', 1
        )[0]
        self.assertEqual(load_logs.count("updateLogsSuccessStatus("), 3)
        for error_path in load_logs.split("catch (error)")[1:]:
            catch_body = error_path.split("}", 1)[0]
            self.assertNotIn("updateLogsSuccessStatus", catch_body)

    def test_logs_error_paths_reset_visible_count_but_not_timestamp(self):
        javascript = self.client.get("/static/logs.js").text
        self.assertIn(
            "function updateLogsErrorStatus() {\n"
            "  logsElements.visibleCount.textContent = \"0\";\n"
            "}",
            javascript,
        )
        self.assertNotIn("lastSuccessfulRefresh", javascript.split(
            "function updateLogsErrorStatus() {", 1
        )[1].split("}", 1)[0])

        load_logs = javascript.split("async function loadLogs()", 1)[1].split(
            'logsElements.refreshButton.addEventListener("click", loadLogs)', 1
        )[0]
        self.assertEqual(load_logs.count("updateLogsErrorStatus()"), 3)
        for error_path in load_logs.split("catch (error)")[1:]:
            catch_body = error_path.split("}", 1)[0]
            self.assertIn("updateLogsErrorStatus()", catch_body)
            self.assertNotIn("lastSuccessfulRefresh", catch_body)

    def test_logs_status_wraps_on_small_screens(self):
        stylesheet = self.client.get("/static/styles.css").text
        self.assertIn(".logs-status {", stylesheet)
        self.assertIn("flex-wrap: wrap", stylesheet)
        mobile_rules = stylesheet.split("@media (max-width: 640px)", 1)[1]
        self.assertIn(".logs-status { align-items: flex-start; flex-direction: column;", mobile_rules)


class MailboxTaskDetailEndpointTests(ApiTestsBase):
    def test_mailbox_task_detail_exposes_full_experiment_id(self):
        full_experiment_id = "exp-20260816-abcdef0123456789fedcba9876543210"
        approval = SimpleNamespace(
            to_dict=lambda: {
                "id": "approval-1",
                "mailbox_msg_id": 42,
                "task_body": "Investigate failure",
                "sender": "nexus",
                "file_path": "communication/inbox/task.md",
                "received_at": "2026-08-16T00:00:00Z",
                "status": "failed",
                "approved_at": None,
                "rejected_at": None,
                "rejection_reason": None,
                "experiment_id": full_experiment_id,
                "outbox_file": None,
                "result_summary": None,
                "error": None,
                "created_at": "2026-08-16T00:00:00Z",
                "updated_at": "2026-08-16T00:05:00Z",
            }
        )
        service = MagicMock()
        service.get.return_value = approval
        api.app.dependency_overrides[get_mailbox_task_service] = lambda: service

        response = self.client.get("/mailbox/tasks/approval-1", headers={"x-api-key": "test-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["experiment_id"], full_experiment_id)
        self.assertGreater(len(response.json()["experiment_id"]), 8)


class StatusEndpointTests(ApiTestsBase):
    def test_status_returns_expected_fields(self):
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("agent_status", body)
        self.assertIn("api_version", body)
        self.assertIn("components", body)
        self.assertIsInstance(body["components"], list)
        self.assertIn("problems", body["components"])
        self.assertIn("ideas", body["components"])

    def test_status_does_not_leak_env_or_paths(self):
        response = self.client.get("/status")
        body_text = json.dumps(response.json())
        self.assertNotIn("AGENT_API_KEY", body_text)
        self.assertNotIn("/agent", body_text)
        self.assertNotIn("test-key", body_text)


class ProblemsEndpointTests(ApiTestsBase):
    def test_problems_empty_when_no_runs(self):
        response = self.client.get("/problems")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["problems"], [])

    def test_problems_lists_failed_runs(self):
        self._write_run("run1.json", "ERROR: something broke")
        self._write_run("run2.json", "all good, no error here")
        response = self.client.get("/problems")
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["problems"][0]["summary"], "something broke")

    def test_problems_missing_runs_dir_returns_empty(self):
        data_sources.RUNS_DIR = Path(self.temp_dir.name) / "does-not-exist"
        response = self.client.get("/problems")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_problems_skips_corrupt_json_file(self):
        (self.runs_dir / "bad.json").write_text("{not valid json", encoding="utf-8")
        self._write_run("good.json", "ERROR: valid error entry")
        response = self.client.get("/problems")
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["problems"][0]["summary"], "valid error entry")


class IdeasEndpointTests(ApiTestsBase):
    def test_ideas_empty_when_no_file(self):
        response = self.client.get("/ideas")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["ideas"], [])

    def test_ideas_lists_items_in_ideas_section_only(self):
        self._write_ideas_md(
            "# IDEAS.md\n\n## Intro\n\n- not an idea, just intro prose\n\n"
            "## Ideas\n\n### Section A\n\n- open idea one `exploring`\n"
            "- open idea two\n"
            "\n### Section B\n\n- open idea three `rejected`\n"
            "\n## Open unknowns\n\n- not an idea, an unknown\n"
        )
        response = self.client.get("/ideas")
        body = response.json()
        self.assertEqual(body["count"], 3)
        texts = {i["text"] for i in body["ideas"]}
        self.assertEqual(texts, {"open idea one", "open idea two", "open idea three"})
        sections = {i["section"] for i in body["ideas"]}
        self.assertEqual(sections, {"Section A", "Section B"})

    def test_ideas_default_state_is_new(self):
        self._write_ideas_md("## Ideas\n\n### Section\n\n- idea without state\n")
        response = self.client.get("/ideas")
        body = response.json()
        self.assertEqual(body["ideas"][0]["state"], "new")

    def test_ideas_parses_trailing_state_marker(self):
        self._write_ideas_md(
            "## Ideas\n\n### Section\n\n- idea one `adopted`\n- idea two `tested`\n"
        )
        response = self.client.get("/ideas")
        body = response.json()
        states = {i["text"]: i["state"] for i in body["ideas"]}
        self.assertEqual(states, {"idea one": "adopted", "idea two": "tested"})


class CreateIdeaEndpointTests(ApiTestsBase):
    HEADERS = {"x-api-key": "test-key"}

    def test_create_idea_requires_api_key(self):
        response = self.client.post("/ideas", json={"text": "no auth"})
        self.assertEqual(response.status_code, 401)

    def test_create_idea_rejects_wrong_api_key(self):
        response = self.client.post(
            "/ideas", json={"text": "wrong key"}, headers={"x-api-key": "nope"}
        )
        self.assertEqual(response.status_code, 401)

    def test_create_idea_rejects_empty_text(self):
        response = self.client.post("/ideas", json={"text": ""}, headers=self.HEADERS)
        self.assertEqual(response.status_code, 422)

    def test_create_idea_rejects_whitespace_only_text(self):
        response = self.client.post("/ideas", json={"text": "   "}, headers=self.HEADERS)
        self.assertEqual(response.status_code, 400)

    def test_create_idea_rejects_too_long_text(self):
        response = self.client.post(
            "/ideas", json={"text": "x" * 501}, headers=self.HEADERS
        )
        self.assertEqual(response.status_code, 422)

    def test_create_idea_appends_with_new_state(self):
        self._write_ideas_md("## Ideas\n\n### Section\n\n- existing idea\n")
        response = self.client.post(
            "/ideas", json={"text": "a fresh idea"}, headers=self.HEADERS
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["text"], "a fresh idea")
        self.assertEqual(body["state"], "new")
        self.assertEqual(body["section"], "Inbox")

    def test_create_idea_is_reflected_by_get_ideas(self):
        self._write_ideas_md("## Ideas\n\n### Section\n\n- existing idea\n")
        self.client.post("/ideas", json={"text": "second idea"}, headers=self.HEADERS)
        response = self.client.get("/ideas")
        body = response.json()
        self.assertEqual(body["count"], 2)
        texts = {i["text"] for i in body["ideas"]}
        self.assertIn("second idea", texts)

    def test_create_idea_creates_ideas_section_when_missing(self):
        self._write_ideas_md("# IDEAS.md\n\nSome intro text.\n")
        response = self.client.post(
            "/ideas", json={"text": "first ever idea"}, headers=self.HEADERS
        )
        self.assertEqual(response.status_code, 201)
        get_response = self.client.get("/ideas")
        self.assertEqual(get_response.json()["count"], 1)

    def test_create_idea_strips_backticks_to_prevent_forged_state(self):
        response = self.client.post(
            "/ideas",
            json={"text": "sneaky `adopted` idea"},
            headers=self.HEADERS,
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["state"], "new")
        self.assertNotIn("`", body["text"])

    def test_create_idea_strips_leading_markdown_markers(self):
        response = self.client.post(
            "/ideas", json={"text": "## fake heading injection"}, headers=self.HEADERS
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["text"], "fake heading injection")

    def test_create_idea_collapses_newlines_and_whitespace(self):
        response = self.client.post(
            "/ideas",
            json={"text": "line one\nline two\n\n   line three"},
            headers=self.HEADERS,
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["text"], "line one line two line three")

    def test_create_idea_does_not_leak_api_key(self):
        response = self.client.post(
            "/ideas", json={"text": "no leak"}, headers=self.HEADERS
        )
        self.assertNotIn("test-key", json.dumps(response.json()))


class LegacyChatReplacementTests(ApiTestsBase):
    def test_agentic_chat_requires_authentication(self):
        response = self.client.post("/chat", json={"message": "status?"})
        self.assertEqual(response.status_code, 401)


class AdoptionAndOperationsEndpointTests(ApiTestsBase):
    HEADERS = {"x-api-key": "test-key"}

    def setUp(self) -> None:
        super().setUp()
        from modules.experiment_service import ExperimentNotFoundError

        self.experiment = MagicMock()
        self.experiment.id = "3ef5b935abcd1234"
        self.experiment.to_dict.return_value = {
            "id": self.experiment.id,
            "goal": "Goal",
            "status": "succeeded",
            "created_at": "2026-08-14T12:00:00Z",
            "quality_evaluation": {"outcome": "pass"},
        }
        self.experiment_service = MagicMock()
        self.experiment_service.resolve_experiment.return_value = self.experiment
        api.app.dependency_overrides[api.get_experiment_service] = lambda: self.experiment_service

        self.adoption_state = {
            "id": "row1",
            "experiment_id": self.experiment.id,
            "status": "queued",
            "target_branch": "main",
            "expected_files": ["app.py"],
            "actual_files": ["app.py"],
            "test_output_lines": [],
            "local_sync_pending": False,
        }
        self.adoption_service = MagicMock()
        self.adoption_service.get.return_value = SimpleNamespace(to_dict=lambda: dict(self.adoption_state), status="queued")
        self.adoption_service.start_adoption.return_value = SimpleNamespace(to_dict=lambda: dict(self.adoption_state), status="queued")
        api.app.dependency_overrides[api.get_experiment_adoption_service] = lambda: self.adoption_service

        self.operations_service = MagicMock()
        self.operations_service.catalog.return_value = [
            SimpleNamespace(to_dict=lambda: {
                "operation": "git_log",
                "description": "History",
                "arguments": [{"name": "limit", "type": "integer", "description": "Max", "required": False, "default": 10, "minimum": 1, "maximum": 100}],
            })
        ]
        self.operations_service.execute.return_value = SimpleNamespace(to_dict=lambda: {
            "operation": "git_log",
            "success": True,
            "output_lines": ["abc init"],
            "duration_ms": 5,
            "exit_code": 0,
            "error_summary": None,
        })
        api.app.dependency_overrides[api.get_operations_service] = lambda: self.operations_service

    def test_post_adopt_requires_api_key(self):
        response = self.client.post(f"/experiments/{self.experiment.id}/adopt")
        self.assertEqual(response.status_code, 401)

    def test_post_adopt_resolves_prefix_and_returns_202(self):
        response = self.client.post(f"/experiments/{self.experiment.id[:8]}/adopt", headers=self.HEADERS)
        self.assertEqual(response.status_code, 202)
        self.experiment_service.resolve_experiment.assert_called_with(self.experiment.id[:8])
        self.adoption_service.start_adoption.assert_called_with(self.experiment.id)
        self.assertEqual(response.json()["status"], "queued")

    def test_post_adopt_maps_conflicts_to_409(self):
        from modules.experiment_adoption_service import ExperimentAdoptionError

        self.adoption_service.start_adoption.side_effect = ExperimentAdoptionError("conflict")
        response = self.client.post(f"/experiments/{self.experiment.id}/adopt", headers=self.HEADERS)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "conflict")

    def test_get_adoption_returns_default_or_existing_state(self):
        self.adoption_service.get.return_value = SimpleNamespace(to_dict=lambda: {
            "id": None,
            "experiment_id": self.experiment.id,
            "status": "not_started",
            "target_branch": "main",
            "expected_files": [],
            "actual_files": [],
            "test_output_lines": [],
            "local_sync_pending": False,
        })
        response = self.client.get(f"/experiments/{self.experiment.id[:8]}/adoption", headers=self.HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_started")

    def test_get_experiment_embeds_adoption_when_present(self):
        response = self.client.get(f"/experiments/{self.experiment.id}", headers=self.HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["adoption"]["status"], "queued")

    def test_operations_catalog_and_execute_require_allowlisted_models(self):
        catalog = self.client.get("/operations/catalog", headers=self.HEADERS)
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.json()["count"], 1)
        execute = self.client.post("/operations/execute", headers=self.HEADERS, json={"operation": "git_log", "args": {"limit": 5}})
        self.assertEqual(execute.status_code, 200)
        self.operations_service.execute.assert_called_with("git_log", {"limit": 5})
        self.assertEqual(execute.json()["output_lines"], ["abc init"])

    def test_operations_execute_maps_validation_errors_to_400(self):
        from modules.operations_registry import OperationValidationError

        self.operations_service.execute.side_effect = OperationValidationError("bad op")
        response = self.client.post("/operations/execute", headers=self.HEADERS, json={"operation": "git_log", "args": {}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "bad op")

    def test_web_assets_contain_operations_tab_and_no_innerhtml(self):
        html = self.client.get("/").text
        js = "\n".join([
            self.client.get(f"/static/{name}").text
            for name in ["utils.js", "api-key.js", "tab-nav.js", "overview.js", "chat.js", "tasks.js", "mailbox.js", "operations.js", "chain-editor.js", "logs.js", "app.js"]
        ])
        self.assertIn('data-tab="operations"', html)
        self.assertIn('id="tab-operations"', html)
        self.assertIn("loadOperationsCatalog", js)
        self.assertNotIn("innerHTML", js)


class TestStartupOutboxRecovery(unittest.TestCase):
    """API lifespan must call publish_pending() for both outbox publishers."""

    def _run_lifespan(self, chain_pub_mock, approval_pub_mock):
        """Start and stop the ASGI lifespan while service mocks are active."""
        from unittest.mock import MagicMock, patch

        mts = _mock_mailbox_task_service()
        ocs = MagicMock()
        ocs.recover_after_restart.return_value = None

        with (
            patch.object(api, "OperationChainOutboxPublisher", chain_pub_mock),
            patch.object(api, "TaskApprovalOutboxPublisher", approval_pub_mock),
            patch("api.MailboxConfig") as mc_cls,
            patch("api.MailboxTaskWorker") as worker_cls,
            patch("api._resolve_mailbox_task_service", return_value=mts),
            patch("api._resolve_codex_service", return_value=MagicMock()),
            patch("api._resolve_experiment_adoption_service", return_value=MagicMock()),
            patch("api._resolve_operation_chain_service", return_value=ocs),
        ):
            mc_cls.from_env.return_value = MagicMock()
            worker_cls.return_value.start.return_value = None
            with TestClient(api.app, raise_server_exceptions=True):
                pass  # triggers lifespan start and stop

    def test_both_publishers_publish_pending_called_at_startup(self):
        """Both OperationChainOutboxPublisher and TaskApprovalOutboxPublisher
        must have publish_pending() called during API startup."""
        from unittest.mock import MagicMock, call

        chain_instance = MagicMock()
        approval_instance = MagicMock()

        chain_cls = MagicMock(return_value=chain_instance)
        approval_cls = MagicMock(return_value=approval_instance)

        self._run_lifespan(chain_cls, approval_cls)

        chain_instance.publish_pending.assert_called_once()
        approval_instance.publish_pending.assert_called_once()

    def test_both_publishers_closed_after_startup(self):
        """Publishers must be closed (connection released) after startup recovery."""
        from unittest.mock import MagicMock

        chain_instance = MagicMock()
        approval_instance = MagicMock()

        chain_cls = MagicMock(return_value=chain_instance)
        approval_cls = MagicMock(return_value=approval_instance)

        self._run_lifespan(chain_cls, approval_cls)

        chain_instance.close.assert_called_once()
        approval_instance.close.assert_called_once()

    def test_startup_continues_when_publisher_raises(self):
        """A publish_pending() exception must not abort the startup sequence."""
        from unittest.mock import MagicMock

        chain_instance = MagicMock()
        chain_instance.publish_pending.side_effect = RuntimeError("push failed")
        approval_instance = MagicMock()

        chain_cls = MagicMock(return_value=chain_instance)
        approval_cls = MagicMock(return_value=approval_instance)

        # Must not raise — startup continues best-effort
        self._run_lifespan(chain_cls, approval_cls)

        # Second publisher must still be attempted even if first raised
        approval_instance.publish_pending.assert_called_once()

    def test_startup_continues_when_constructor_raises(self):
        """A constructor exception must not abort the startup sequence or raise."""
        from unittest.mock import MagicMock

        # First publisher constructor raises; second must still be attempted.
        chain_cls = MagicMock(side_effect=RuntimeError("db unavailable"))
        approval_instance = MagicMock()
        approval_cls = MagicMock(return_value=approval_instance)

        self._run_lifespan(chain_cls, approval_cls)

        approval_instance.publish_pending.assert_called_once()

    def test_startup_continues_when_close_raises(self):
        """A close() exception must not abort the startup sequence or raise."""
        from unittest.mock import MagicMock

        chain_instance = MagicMock()
        chain_instance.close.side_effect = RuntimeError("close failed")
        approval_instance = MagicMock()

        chain_cls = MagicMock(return_value=chain_instance)
        approval_cls = MagicMock(return_value=approval_instance)

        self._run_lifespan(chain_cls, approval_cls)

        # Both publishers must still have publish_pending() attempted
        chain_instance.publish_pending.assert_called_once()
        approval_instance.publish_pending.assert_called_once()




if __name__ == "__main__":
    unittest.main()
