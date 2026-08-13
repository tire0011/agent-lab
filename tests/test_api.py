import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import data_sources
import api


class ApiTestsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api.app)
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

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
