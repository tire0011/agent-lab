import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import data_sources
import api
from api import get_codex_service, get_mailbox_task_service


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


class ApiTestsBase(unittest.TestCase):
    def setUp(self) -> None:
        # Override heavy singletons so tests never touch the production DB
        # or spawn background workers.
        api.app.dependency_overrides[get_codex_service] = _mock_codex_service
        api.app.dependency_overrides[get_mailbox_task_service] = _mock_mailbox_task_service

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
        response = self.client.get("/static/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertIn("textContent", response.text)

    def test_logs_status_contract(self):
        html = self.client.get("/").text
        self.assertIn('id="logs-visible-count"', html)
        self.assertIn('id="logs-last-successful-refresh"', html)
        self.assertIn("Sichtbar:", html)
        self.assertIn("Zuletzt erfolgreich aktualisiert:", html)

    def test_logs_refresh_uses_rendered_arrays_and_shared_load_function(self):
        javascript = self.client.get("/static/app.js").text
        self.assertIn("const visibleCount = renderedEntries.length;", javascript)
        self.assertIn("renderExperimentLogEntries(entries);\n      updateLogsSuccessStatus(entries);", javascript)
        self.assertIn("renderLogsFromActivity(activity);\n      updateLogsSuccessStatus(activity);", javascript)
        self.assertIn("renderLogsFromDiagnostics(experiments);\n    updateLogsSuccessStatus(experiments);", javascript)
        self.assertIn('setInterval(loadLogs, 10000)', javascript)
        self.assertIn('logsElements.refreshButton.addEventListener("click", loadLogs)', javascript)

    def test_logs_success_timestamp_is_not_updated_in_error_paths(self):
        javascript = self.client.get("/static/app.js").text
        load_logs = javascript.split("async function loadLogs()", 1)[1].split(
            'logsElements.refreshButton.addEventListener("click", loadLogs)', 1
        )[0]
        self.assertEqual(load_logs.count("updateLogsSuccessStatus("), 3)
        for error_path in load_logs.split("catch (error)")[1:]:
            catch_body = error_path.split("}", 1)[0]
            self.assertNotIn("updateLogsSuccessStatus", catch_body)

    def test_logs_error_paths_reset_visible_count_but_not_timestamp(self):
        javascript = self.client.get("/static/app.js").text
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


class ChatEndpointTests(ApiTestsBase):
    def test_chat_rejects_empty_message(self):
        response = self.client.post("/chat", json={"message": ""})
        self.assertEqual(response.status_code, 422)

    def test_chat_rejects_whitespace_only_message(self):
        response = self.client.post("/chat", json={"message": "   "})
        self.assertEqual(response.status_code, 400)

    def test_chat_rejects_missing_message_field(self):
        response = self.client.post("/chat", json={})
        self.assertEqual(response.status_code, 422)

    def test_chat_answers_problem_question(self):
        self._write_run("run1.json", "ERROR: disk full")
        response = self.client.post(
            "/chat", json={"message": "Welche Probleme gibt es?"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["intent"], "problems")
        self.assertIn("disk full", body["reply"])

    def test_chat_answers_idea_question(self):
        self._write_ideas_md("## Ideas\n\n### Section\n\n- do the thing\n")
        response = self.client.post(
            "/chat", json={"message": "Was gibt es an Ideen?"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["intent"], "ideas")
        self.assertIn("do the thing", body["reply"])

    def test_chat_answers_status_question(self):
        response = self.client.post("/chat", json={"message": "status?"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["intent"], "status")

    def test_chat_unhandled_message_is_clearly_marked(self):
        response = self.client.post("/chat", json={"message": "hallo wie geht es dir"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["intent"], "unhandled")
        self.assertIn("regelbasierter", body["reply"])


class ConsistencyTests(ApiTestsBase):
    def test_chat_problems_match_problems_endpoint(self):
        self._write_run("run1.json", "ERROR: consistent error")
        problems_response = self.client.get("/problems").json()
        chat_response = self.client.post(
            "/chat", json={"message": "Probleme?"}
        ).json()

        self.assertEqual(problems_response["count"], 1)
        self.assertIn(
            problems_response["problems"][0]["summary"], chat_response["reply"]
        )

    def test_chat_ideas_match_ideas_endpoint(self):
        self._write_ideas_md("## Ideas\n\n### Section\n\n- shared idea item\n")
        ideas_response = self.client.get("/ideas").json()
        chat_response = self.client.post(
            "/chat", json={"message": "next idea?"}
        ).json()

        self.assertEqual(ideas_response["count"], 1)
        self.assertIn(ideas_response["ideas"][0]["text"], chat_response["reply"])


if __name__ == "__main__":
    unittest.main()
