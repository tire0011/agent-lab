from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules.operations_registry import OperationsRegistry
from modules.web_research import WebResearchClient, WebResearchConfig


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int) -> bytes:
        return self.body[:amount]


def config(**overrides) -> WebResearchConfig:
    values = {
        "endpoint": "https://search.test/search",
        "max_pages": 2,
        "max_results": 5,
        "max_requests": 8,
        "max_response_bytes": 1000,
        "timeout_seconds": 0.1,
        "max_concurrency": 1,
    }
    values.update(overrides)
    return WebResearchConfig(**values)


class WebResearchTests(unittest.TestCase):
    def test_page_limit_and_response_size_limit_are_enforced(self):
        search = json.dumps({"results": [
            {"title": "one", "url": "https://one.test/a", "content": "first"},
            {"title": "two", "url": "https://two.test/b", "content": "second"},
            {"title": "three", "url": "https://three.test/c", "content": "third"},
        ]}).encode()
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            if request.full_url.startswith("https://search.test"):
                return Response(search)
            if request.full_url.endswith("robots.txt"):
                return Response(b"User-agent: *\nAllow: /\n")
            if request.full_url == "https://two.test/b":
                return Response(b"x" * 1001)
            return Response(b"<html><body>Useful page text</body></html>")

        result = WebResearchClient(config(), opener=opener).research("question", max_pages=10)

        self.assertEqual(result.metrics["search_count"], 1)
        self.assertEqual(result.metrics["fetched_pages"], 1)
        self.assertEqual(result.metrics["http_requests"], 5)  # search + 2 robots + 2 pages
        self.assertTrue(any("size_limit" in failure for failure in result.metrics["failures"]))
        self.assertFalse(any("three.test" in url for url, _ in calls))

    def test_robots_refusal_prevents_page_fetch(self):
        search = json.dumps({"results": [{"title": "blocked", "url": "https://site.test/private", "content": "snippet"}]}).encode()
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if request.full_url.startswith("https://search.test"):
                return Response(search)
            if request.full_url.endswith("robots.txt"):
                return Response(b"User-agent: *\nDisallow: /private\n")
            raise AssertionError("blocked page was fetched")

        result = WebResearchClient(config(), opener=opener).research("question")

        self.assertEqual(result.metrics["fetched_pages"], 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("robots_refused" in failure for failure in result.metrics["failures"]))

    def test_search_timeout_is_bounded_and_recorded(self):
        timeouts = []

        def opener(_request, timeout):
            timeouts.append(timeout)
            raise socket.timeout("late")

        result = WebResearchClient(config(timeout_seconds=0.25), opener=opener).research("question")

        self.assertFalse(result.success)
        self.assertEqual(timeouts, [0.25])
        self.assertEqual(result.metrics["search_count"], 1)
        self.assertEqual(result.metrics["http_requests"], 1)
        self.assertIn("search: timeout", result.metrics["failures"])

    def test_request_limit_fails_closed_for_robots(self):
        search = json.dumps({"results": [{"url": "https://site.test/a", "content": "snippet"}]}).encode()

        def opener(request, timeout):
            if request.full_url.startswith("https://search.test"):
                return Response(search)
            raise AssertionError("request limit should stop robots I/O")

        result = WebResearchClient(config(max_requests=1), opener=opener).research("question")
        self.assertEqual(result.metrics["http_requests"], 1)
        self.assertEqual(result.metrics["fetched_pages"], 0)
        self.assertTrue(any("request_limit" in failure for failure in result.metrics["failures"]))

    def test_fetch_concurrency_never_exceeds_configured_limit(self):
        search = json.dumps({"results": [
            {"url": f"https://site.test/{number}", "content": str(number)}
            for number in range(3)
        ]}).encode()
        active = 0
        peak = 0
        lock = threading.Lock()

        def opener(request, timeout):
            nonlocal active, peak
            if request.full_url.startswith("https://search.test"):
                return Response(search)
            if request.full_url.endswith("robots.txt"):
                return Response(b"User-agent: *\nAllow: /\n")
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return Response(b"page")

        result = WebResearchClient(
            config(max_pages=3, max_concurrency=2), opener=opener
        ).research("question", max_pages=3)

        self.assertEqual(result.metrics["fetched_pages"], 3)
        self.assertLessEqual(peak, 2)


class WebResearchRegistryTests(unittest.TestCase):
    def test_metrics_are_persisted_in_operation_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state.db")
            registry = OperationsRegistry(
                experiment_service=SimpleNamespace(), db_path=db_path, repo_root=tmp,
                mailbox_lock_path=str(Path(tmp) / "mailbox.lock"),
            )
            self.addCleanup(registry.close)
            search = json.dumps({"results": [{"title": "answer", "url": "https://site.test/a", "content": "short fact"}]}).encode()

            def opener(request, timeout):
                if request.full_url.startswith("https://search.test"):
                    return Response(search)
                if request.full_url.endswith("robots.txt"):
                    return Response(b"User-agent: *\nAllow: /\n")
                return Response(b"<p>short fact with context</p>")

            env = {
                "SEARXNG_JSON_URL": "https://search.test/search",
                "NEXUS_WEB_RESEARCH_MAX_PAGES": "1",
            }
            with patch.dict("os.environ", env), patch(
                "modules.web_research.urllib.request.urlopen", side_effect=opener
            ):
                result = registry.execute("web_research", {"query": "question", "max_pages": 1})

            row = registry._conn.execute(
                "SELECT result FROM operation_log WHERE operation = 'web_research'"
            ).fetchone()
            logged = json.loads(row["result"])
            self.assertTrue(result.success)
            self.assertEqual(logged["metrics"]["search_count"], 1)
            self.assertEqual(logged["metrics"]["fetched_pages"], 1)
            self.assertGreater(logged["metrics"]["tokens"], 0)
            self.assertIn("runtime_ms", logged["metrics"])
            self.assertIn("estimated_cost_usd", logged["metrics"])
            self.assertIn("failures", logged["metrics"])


if __name__ == "__main__":
    unittest.main()
