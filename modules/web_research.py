"""Bounded, robots-aware web research backed by SearXNG's JSON API."""

from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable


class WebResearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebResearchConfig:
    endpoint: str
    max_pages: int = 3
    max_results: int = 8
    max_requests: int = 12
    max_response_bytes: int = 512_000
    timeout_seconds: float = 5.0
    max_concurrency: int = 2
    user_agent: str = "NexusWebResearch/1.0"

    @classmethod
    def from_env(cls) -> "WebResearchConfig":
        endpoint = os.getenv("SEARXNG_JSON_URL", "").strip()
        return cls(
            endpoint=endpoint,
            max_pages=_env_int("NEXUS_WEB_RESEARCH_MAX_PAGES", 3, 1, 10),
            max_results=_env_int("NEXUS_WEB_RESEARCH_MAX_RESULTS", 8, 1, 20),
            max_requests=_env_int("NEXUS_WEB_RESEARCH_MAX_REQUESTS", 12, 2, 50),
            max_response_bytes=_env_int("NEXUS_WEB_RESEARCH_MAX_BYTES", 512_000, 1024, 2_000_000),
            timeout_seconds=_env_float("NEXUS_WEB_RESEARCH_TIMEOUT_SECONDS", 5.0, 0.1, 30.0),
            max_concurrency=_env_int("NEXUS_WEB_RESEARCH_CONCURRENCY", 2, 1, 4),
        )


@dataclass
class WebResearchResult:
    success: bool
    lines: list[str]
    runtime_ms: int
    metrics: dict[str, object] = field(default_factory=dict)
    error: str | None = None


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)


class WebResearchClient:
    """Perform one strictly bounded search/fetch operation.

    ``opener`` is injectable so all network behavior can be tested without
    making external calls.
    """

    def __init__(
        self,
        config: WebResearchConfig,
        *,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self.config = config
        self._opener = opener or urllib.request.urlopen
        self._request_lock = threading.Lock()

    def research(self, query: str, *, max_pages: int | None = None) -> WebResearchResult:
        started = time.perf_counter()
        failures: list[str] = []
        metrics: dict[str, object] = {
            "search_count": 0,
            "fetched_pages": 0,
            "tokens": 0,
            "runtime_ms": 0,
            "failures": failures,
            "estimated_cost_usd": 0.0,
            "http_requests": 0,
        }
        if not self.config.endpoint:
            return self._finish(started, metrics, [], "SEARXNG_JSON_URL is not configured")
        if not query.strip():
            return self._finish(started, metrics, [], "query is required")

        page_limit = min(max_pages or self.config.max_pages, self.config.max_pages)
        try:
            search_url = self._search_url(query)
            metrics["search_count"] = 1
            body = self._request(search_url, metrics, accept="application/json")
            payload = json.loads(body.decode("utf-8"))
            raw_results = payload.get("results", [])
            if not isinstance(raw_results, list):
                raise WebResearchError("invalid SearXNG results")
        except (WebResearchError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"search: {self._failure_name(exc)}")
            return self._finish(started, metrics, [], "web search failed")

        results: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for item in raw_results[: self.config.max_results]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or url in seen:
                continue
            seen.add(url)
            results.append((str(item.get("title", "")).strip(), url, str(item.get("content", "")).strip()))
            if len(results) >= page_limit:
                break

        allowed: list[tuple[str, str, str]] = []
        robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        for item in results:
            origin = self._origin(item[1])
            parser = robots_cache.get(origin)
            if origin not in robots_cache:
                parser = self._robots(origin, metrics, failures)
                robots_cache[origin] = parser
            if parser is None or not parser.can_fetch(self.config.user_agent, item[1]):
                failures.append(f"robots_refused: {item[1]}")
                continue
            allowed.append(item)

        pages: list[tuple[str, str, str, str]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.max_concurrency) as pool:
            future_items = {pool.submit(self._fetch_page, item, metrics): item for item in allowed[:page_limit]}
            for future, item in future_items.items():
                try:
                    pages.append(future.result(timeout=self.config.timeout_seconds + 0.5))
                except Exception as exc:  # individual pages do not fail the whole research call
                    failures.append(f"fetch: {item[1]}: {self._failure_name(exc)}")

        metrics["fetched_pages"] = len(pages)
        lines = self._answer(pages, results)
        metrics["tokens"] = max(1, sum(len(line) for line in lines) // 4) if lines else 0
        error = None if lines else "no accessible research results"
        return self._finish(started, metrics, lines, error)

    def _request(self, url: str, metrics: dict[str, object], *, accept: str) -> bytes:
        # The counter is deliberately updated before I/O: timed-out attempts
        # are requests and must be visible in the operation log.
        with self._request_lock:
            count = int(metrics["http_requests"])
            if count >= self.config.max_requests:
                raise WebResearchError("request limit reached")
            metrics["http_requests"] = count + 1
        request = urllib.request.Request(url, headers={"User-Agent": self.config.user_agent, "Accept": accept})
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                data = response.read(self.config.max_response_bytes + 1)
        except (TimeoutError, socket.timeout) as exc:
            raise WebResearchError("timeout") from exc
        except urllib.error.URLError as exc:
            reason = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "request failed"
            raise WebResearchError(reason) from exc
        if len(data) > self.config.max_response_bytes:
            raise WebResearchError("response size limit exceeded")
        return data

    def _robots(self, origin: str, metrics: dict[str, object], failures: list[str]):
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            body = self._request(parser.url, metrics, accept="text/plain")
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
            return parser
        except WebResearchError as exc:
            # Fail closed: inability to establish permission means no fetch.
            failures.append(f"robots: {origin}: {self._failure_name(exc)}")
            return None

    def _fetch_page(self, item, metrics):
        title, url, snippet = item
        body = self._request(url, metrics, accept="text/html,text/plain")
        extractor = _TextExtractor()
        extractor.feed(body.decode("utf-8", errors="replace"))
        text = " ".join(extractor.parts)[:1200]
        return title, url, snippet, text

    def _search_url(self, query: str) -> str:
        parsed = urllib.parse.urlsplit(self.config.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise WebResearchError("invalid SearXNG endpoint")
        params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        params.extend((("q", query), ("format", "json")))
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(params), ""))

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @staticmethod
    def _answer(pages, search_results) -> list[str]:
        lines: list[str] = []
        for title, url, snippet, text in pages[:3]:
            summary = snippet or text
            summary = " ".join(summary.split())[:320]
            lines.append(f"{title or 'Source'}: {summary}" if summary else (title or "Source"))
            lines.append(f"Source: {url}")
        # Search snippets remain useful sources when all page bodies are empty.
        if not lines:
            for title, url, snippet in search_results[:3]:
                if snippet:
                    lines.extend((f"{title or 'Result'}: {' '.join(snippet.split())[:320]}", f"Source: {url}"))
        return lines

    @staticmethod
    def _failure_name(exc: BaseException) -> str:
        text = str(exc).lower()
        if "timeout" in text:
            return "timeout"
        if "size limit" in text:
            return "size_limit"
        if "request limit" in text:
            return "request_limit"
        return "request_failed"

    @staticmethod
    def _finish(started, metrics, lines, error):
        runtime_ms = int((time.perf_counter() - started) * 1000)
        metrics["runtime_ms"] = runtime_ms
        return WebResearchResult(not bool(error), lines, runtime_ms, metrics, error)
