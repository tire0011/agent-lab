"""#667: streamed llama-server calls, early stops, measured partial rates, admission.

A fake llama-server runs behind ``httpx.MockTransport``; nothing is contacted.
Times are real but small (a 1-2 s client timeout), so each test is quick.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import httpx

from modules import local_shadow_replay as lsr
from modules.llama_cpp_client import (
    REASON_GENERATION_TOO_SLOW,
    REASON_PROMPT_TOO_SLOW,
    LlamaCppClient,
    LlamaCppClientError,
)
from modules.local_coding_worker import (
    LOCAL_TOO_SLOW,
    MIN_OUTPUT_TOKENS,
    LocalCodingWorker,
    local_not_called_reason,
)
from modules.local_model_rates import LocalModelRates
from modules.run_store import init_db
from tests.test_issue_637_local_shadow_replay import ReplayFixture, _result

_CONTENT_PIECES = ["Hel", "lo", ", ", "wor", "ld"]
_SERVER_TIMINGS = {"prompt_n": 120, "prompt_ms": 400.0, "predicted_n": 5, "predicted_ms": 100.0,
                   "cache_n": 30}


def _sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


class FakeLlamaServer:
    """Serves ``/chat/completions`` (SSE or JSON), ``/slots``, ``/props``, ``/tokenize``."""

    def __init__(self, *, first_delay=0.0, token_delay=0.0, tokens=None, per_token_timings=False,
                 final_timings=True, busy=1, n_ctx=8192):
        self.first_delay = first_delay
        self.token_delay = token_delay
        self.tokens = list(tokens if tokens is not None else _CONTENT_PIECES)
        self.per_token_timings = per_token_timings
        self.final_timings = final_timings
        self.busy = busy
        self.n_ctx = n_ctx
        self.payloads: list[dict] = []
        self.sent = 0
        self.finished = False
        self.transport = httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/slots"):
            return httpx.Response(200, json=[{"id": i, "is_processing": i < self.busy} for i in range(2)])
        if path.endswith("/props"):
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": self.n_ctx}})
        if path.endswith("/tokenize"):
            content = json.loads(request.content)["content"]
            return httpx.Response(200, json={"tokens": list(range(max(1, len(content) // 4)))})
        payload = json.loads(request.content)
        self.payloads.append(payload)
        if not payload.get("stream"):
            return httpx.Response(200, json={
                "id": "cmpl-1", "object": "chat.completion", "created": 1, "model": "m",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "".join(self.tokens)}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": len(self.tokens)},
                "timings": _SERVER_TIMINGS,
            })
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=self.events())

    async def events(self):
        await asyncio.sleep(self.first_delay)
        yield _sse({"id": "cmpl-1", "created": 1, "model": "m",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]})
        for index, piece in enumerate(self.tokens):
            if index:
                await asyncio.sleep(self.token_delay)
            chunk = {"id": "cmpl-1", "choices": [{"index": 0, "delta": {"content": piece},
                                                  "finish_reason": None}]}
            if self.per_token_timings:
                chunk["timings"] = {"prompt_n": 120, "prompt_ms": 400.0,
                                    "predicted_n": index + 1, "predicted_ms": 20.0 * (index + 1)}
            self.sent += 1
            yield _sse(chunk)
        final = {"id": "cmpl-1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        if self.final_timings:
            final["timings"] = _SERVER_TIMINGS
        yield _sse(final)
        yield _sse({"id": "cmpl-1", "choices": [],
                    "usage": {"prompt_tokens": 120, "completion_tokens": len(self.tokens)}})
        self.finished = True
        yield b"data: [DONE]\n\n"


def _client(server: FakeLlamaServer, *, stream: bool, timeout: float = 2.0) -> LlamaCppClient:
    return LlamaCppClient(base_url="http://llama.test/v1", timeout=timeout, model="m",
                          stream=stream, transport=server.transport)


def _complete(client: LlamaCppClient, max_tokens: int = 64) -> str:
    return asyncio.run(client.complete("say hello", system="sys", max_tokens=max_tokens))


class StreamingClientTests(unittest.TestCase):
    def test_stream_is_on_by_default_and_env_switches_it_off(self):
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("LOCAL_MODEL_STREAM", None)
            self.assertTrue(LlamaCppClient(base_url="http://x/v1").stream)
            env["LOCAL_MODEL_STREAM"] = "0"
            self.assertFalse(LlamaCppClient(base_url="http://x/v1").stream)
            env["LOCAL_MODEL_STREAM"] = "1"
            self.assertTrue(LlamaCppClient(base_url="http://x/v1").stream)

    def test_streamed_chunks_assemble_the_same_content_and_timings_as_non_stream(self):
        plain_server, stream_server = FakeLlamaServer(), FakeLlamaServer()
        plain, streamed = _client(plain_server, stream=False), _client(stream_server, stream=True)

        self.assertEqual(_complete(plain), "Hello, world")
        self.assertEqual(_complete(streamed), "Hello, world")
        self.assertEqual(streamed.last_timings, plain.last_timings)
        self.assertEqual(plain.last_timings, {"prompt_n": 120, "prompt_ms": 400.0, "predicted_n": 5,
                                              "predicted_ms": 100.0, "tokens_cached": 30})
        # Today's behaviour without stream: a plain POST, no stream fields.
        self.assertNotIn("stream", plain_server.payloads[0])
        self.assertIsNone(plain.last_stream)
        self.assertTrue(stream_server.payloads[0]["stream"])
        self.assertEqual(stream_server.payloads[0]["messages"], plain_server.payloads[0]["messages"])
        self.assertEqual(streamed.last_stream["tokens_streamed"], 5)
        self.assertIsNotNone(streamed.last_stream["ttft_seconds"])

    def test_stream_response_dict_has_the_non_stream_shape(self):
        data = asyncio.run(_client(FakeLlamaServer(), stream=True).chat_completion(
            [{"role": "user", "content": "x"}], max_tokens=64))
        self.assertEqual(data["choices"][0]["message"], {"role": "assistant", "content": "Hello, world"})
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        self.assertEqual(data["usage"]["prompt_tokens"], 120)
        self.assertEqual(data["timings"], _SERVER_TIMINGS)

    def test_no_first_token_before_ttft_limit_stops_with_prompt_too_slow(self):
        server = FakeLlamaServer(first_delay=10.0, busy=2)
        client = _client(server, stream=True, timeout=1.0)  # TTFT limit 0.6 s
        started = time.monotonic()
        with self.assertRaises(LlamaCppClientError) as caught:
            _complete(client)
        elapsed = time.monotonic() - started
        error = caught.exception
        self.assertEqual(error.reason, REASON_PROMPT_TOO_SLOW)
        self.assertLess(elapsed, 0.95)
        self.assertGreaterEqual(error.rates["elapsed_seconds"], 0.55)
        self.assertIsNone(error.rates["ttft_seconds"])
        self.assertEqual(error.rates["tokens_streamed"], 0)
        self.assertIsNone(error.timings)  # not one token: nothing measured
        self.assertEqual(error.busy_slots, 2)
        self.assertIn("prompt_too_slow", str(error))
        self.assertIn("busy slots 2", str(error))
        self.assertEqual(server.sent, 0)

    def test_slow_generation_stops_with_generation_too_slow_before_the_timeout(self):
        server = FakeLlamaServer(token_delay=0.05, tokens=[f"t{i} " for i in range(200)],
                                 per_token_timings=True)
        client = _client(server, stream=True, timeout=2.0)
        client.stream_min_tokens = 4
        started = time.monotonic()
        with self.assertRaises(LlamaCppClientError) as caught:
            _complete(client, max_tokens=1000)  # ~20 tok/s -> ~50 s projected
        elapsed = time.monotonic() - started
        error = caught.exception
        self.assertEqual(error.reason, REASON_GENERATION_TOO_SLOW)
        self.assertLess(elapsed, 1.0)
        self.assertLess(server.sent, 20)
        self.assertFalse(server.finished)  # the request was cancelled
        # The measured partial rates: server per-token timings.
        self.assertEqual(error.timings["prompt_n"], 120)
        self.assertAlmostEqual(error.rates["prompt_tokens_per_second"], 300.0)
        self.assertAlmostEqual(error.rates["generation_tokens_per_second"], 50.0)
        self.assertEqual(error.busy_slots, 1)
        self.assertEqual(client.last_timings, error.timings)
        self.assertIn("generation_too_slow", str(error))

    def test_generation_rate_from_the_client_clock_without_server_timings(self):
        server = FakeLlamaServer(token_delay=0.05, tokens=[f"t{i} " for i in range(200)],
                                 final_timings=False)
        client = _client(server, stream=True, timeout=2.0)
        client.stream_min_tokens = 6
        with self.assertRaises(LlamaCppClientError) as caught:
            _complete(client, max_tokens=1000)
        error = caught.exception
        self.assertEqual(error.reason, REASON_GENERATION_TOO_SLOW)
        self.assertEqual(error.timings["predicted_n"], 5)
        rate = error.rates["generation_tokens_per_second"]
        self.assertTrue(10 < rate < 25, rate)  # ~20 tok/s on the client clock

    def test_a_call_that_can_finish_in_time_is_not_stopped(self):
        server = FakeLlamaServer(token_delay=0.01, tokens=[f"t{i} " for i in range(30)])
        client = _client(server, stream=True, timeout=2.0)
        client.stream_min_tokens = 4
        self.assertEqual(_complete(client, max_tokens=40), "".join(f"t{i} " for i in range(30)))
        self.assertTrue(server.finished)


class _Log:
    def __init__(self):
        self.events = []

    def info(self, _phase, event, _message, **fields):
        self.events.append((event, fields))

    warning = info

    def all(self, name):
        return [fields for event, fields in self.events if event == name]


class _CountingServer:
    """A fake client for the admission tests; counts model calls."""

    model = "m"

    def __init__(self, timeout=300.0):
        self.timeout = timeout
        self.calls = 0
        self.last_timings = None

    async def context_size(self):
        return 20_000

    async def count_tokens(self, text):
        return max(1, len(text) // 4)

    async def complete(self, *_args, **_kwargs):
        self.calls += 1
        return "not a diff"


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "value.txt").write_text("old\n", encoding="utf-8")
        self.rates_path = Path(self.temp.name) / "rates.json"

    def run_worker(self, client, max_tokens=4096):
        log = _Log()
        worker = LocalCodingWorker(client=client, max_tokens=max_tokens, rates_path=self.rates_path)
        result = worker.run(task="Edit value.txt", working_dir=str(self.repo),
                            sandbox="workspace-write", experiment_logger=log)
        return worker, result, log

    def test_early_stop_stores_measured_partial_rates_not_guesses(self):
        server = FakeLlamaServer(token_delay=0.05, tokens=[f"t{i} " for i in range(200)],
                                 per_token_timings=True)
        client = _client(server, stream=True, timeout=2.0)
        client.stream_min_tokens = 4
        _, result, log = self.run_worker(client)

        self.assertFalse(result.success)
        self.assertIn("generation_too_slow", result.error)
        prompt_rate, generation_rate = LocalModelRates(self.rates_path, model="m").medians()
        self.assertAlmostEqual(prompt_rate, 300.0)
        self.assertAlmostEqual(generation_rate, 50.0)
        # What record_timeout would have guessed instead.
        self.assertNotAlmostEqual(generation_rate, MIN_OUTPUT_TOKENS / (2.0 * 0.8 * 0.5))
        call = log.all("local_model_call")[-1]
        self.assertEqual(call["outcome"], "early_stop")
        self.assertEqual(call["stop_reason"], "generation_too_slow")
        self.assertEqual(call["busy_slots"], 1)
        self.assertAlmostEqual(call["stop_rates"]["generation_tokens_per_second"], 50.0)
        self.assertEqual(log.all("local_patch_round")[-1]["outcome"], "model_timeout")
        self.assertEqual(log.all("local_patch_round")[-1]["stop_reason"], "generation_too_slow")

    def test_prompt_too_slow_stores_the_measured_prompt_bound_not_guesses(self):
        server = FakeLlamaServer(first_delay=10.0)
        client = _client(server, stream=True, timeout=1.0)
        _, result, log = self.run_worker(client)

        self.assertIn("prompt_too_slow", result.error)
        call = log.all("local_model_call")[-1]
        self.assertEqual(call["stop_reason"], "prompt_too_slow")
        rates = LocalModelRates(self.rates_path, model="m")
        prompt_rate, generation_rate = rates.medians()
        # At most prompt_tokens / time waited (~0.6 s), not prompt_tokens / timeout.
        expected = call["prompt_tokens"] / call["stop_rates"]["elapsed_seconds"]
        self.assertAlmostEqual(prompt_rate, expected, places=3)
        # The measured prompt bound wins over record_timeout's prompt guess,
        # while generation gets the conservative fallback needed for admission.
        self.assertAlmostEqual(generation_rate, MIN_OUTPUT_TOKENS / (1.0 * 0.8 * 0.5))
        self.assertFalse(rates.generation_measured())

        # A repeat run is refused instead of burning another TTFT interval.
        second_client = _CountingServer(timeout=1.0)
        _, second, second_log = self.run_worker(second_client)
        self.assertEqual(second_client.calls, 0)
        self.assertTrue(second.error.startswith(LOCAL_TOO_SLOW), second.error)
        self.assertEqual(len(second_log.all("local_too_slow")), 1)

    def test_record_timeout_stays_the_fallback_without_any_measurement(self):
        rates = LocalModelRates(self.rates_path, model="m")
        self.assertTrue(rates.record_timeout(prompt_tokens=3000, timeout_seconds=300.0,
                                             minimum_output_tokens=MIN_OUTPUT_TOKENS))
        prompt_rate, generation_rate = rates.medians()
        self.assertAlmostEqual(prompt_rate, 10.0)
        self.assertAlmostEqual(generation_rate, MIN_OUTPUT_TOKENS / 120.0)
        self.assertFalse(rates.generation_measured())
        # A measurement replaces the guess for that rate at once.
        rates.record({"predicted_n": 50, "predicted_ms": 1000})
        self.assertEqual(rates.medians(), (10.0, 50.0))
        self.assertTrue(rates.generation_measured())

    def test_prompt_stop_persists_both_rates_atomically_and_is_repeatable(self):
        rates = LocalModelRates(self.rates_path, model="m")
        timings = {"prompt_n": 120, "prompt_ms": 600}
        with patch.object(rates, "_append", wraps=rates._append) as append:
            self.assertTrue(rates.record_prompt_stop(
                timings, prompt_tokens=120, timeout_seconds=1.0,
                minimum_output_tokens=MIN_OUTPUT_TOKENS))
            self.assertEqual(append.call_count, 1)
            written = append.call_args.args[0]
            self.assertIn("prompt_tokens_per_second", written)
            self.assertIn("estimated_generation_tokens_per_second", written)
        self.assertTrue(rates.record_prompt_stop(
            timings, prompt_tokens=120, timeout_seconds=1.0,
            minimum_output_tokens=MIN_OUTPUT_TOKENS))
        self.assertEqual(rates.medians(), (200.0, 640.0))

    def test_a_guessed_generation_rate_prevents_an_identical_hopeless_call(self):
        # The guess affords only MIN_OUTPUT_TOKENS, so admission refuses rather
        # than repeating a zero-token timeout.
        LocalModelRates(self.rates_path, model="m").record_timeout(
            prompt_tokens=200, timeout_seconds=300.0, minimum_output_tokens=MIN_OUTPUT_TOKENS)
        client = _CountingServer()
        _, _, log = self.run_worker(client)
        self.assertEqual(client.calls, 0)
        self.assertEqual(len(log.all("local_too_slow")), 1)

    def _seed(self, prompt_rate, generation_rate):
        assert LocalModelRates(self.rates_path, model="m").record({
            "prompt_n": prompt_rate, "prompt_ms": 1000, "predicted_n": generation_rate, "predicted_ms": 1000,
        })

    def test_admission_refuses_when_the_output_budget_is_below_768(self):
        # 300 s * 0.8 * 0.5 = 120 s for generation at 3 tok/s = 360 tokens < 768.
        self._seed(50.0, 3.0)
        client = _CountingServer()
        _, result, log = self.run_worker(client)

        self.assertEqual(client.calls, 0)
        self.assertFalse(result.success)
        self.assertTrue(result.error.startswith(LOCAL_TOO_SLOW), result.error)
        self.assertIn("only 360 output tokens", result.error)
        self.assertIn("generation 3.00 tok/s", result.error)
        self.assertIn("needs 768", result.error)
        self.assertEqual(local_not_called_reason(result.error), LOCAL_TOO_SLOW)
        refused = log.all("local_too_slow")
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["generation_tokens_per_second"], 3.0)

    def test_admission_refuses_a_configured_output_cap_below_768_without_rates(self):
        client = _CountingServer()
        _, result, log = self.run_worker(client, max_tokens=512)

        self.assertEqual(client.calls, 0)
        self.assertTrue(result.error.startswith(LOCAL_TOO_SLOW), result.error)
        self.assertIn("only 512 output tokens", result.error)
        self.assertIn("configured max_output_tokens 512", result.error)
        self.assertIn("needs 768", result.error)
        self.assertEqual(len(log.all("local_too_slow")), 1)

    def test_admission_lets_a_call_through_with_enough_output_budget(self):
        self._seed(50.0, 10.0)  # 120 s * 10 tok/s = 1200 tokens
        client = _CountingServer()
        _, result, log = self.run_worker(client)
        self.assertGreater(client.calls, 0)
        self.assertFalse(log.all("local_too_slow"))
        self.assertFalse((result.error or "").startswith(LOCAL_TOO_SLOW))

    def test_admission_can_be_disabled(self):
        self._seed(50.0, 3.0)
        client = _CountingServer()
        with patch.dict("os.environ", {"LOCAL_MIN_USEFUL_PATCH_TOKENS": "0"}):
            self.run_worker(client)
        self.assertGreater(client.calls, 0)

    def test_admission_is_skipped_without_measured_rates(self):
        client = _CountingServer()
        self.run_worker(client)
        self.assertGreater(client.calls, 0)

    def test_context_too_small_is_still_its_own_reason(self):
        self.assertEqual(local_not_called_reason("context_too_small: x"), "context_too_small")
        self.assertIsNone(local_not_called_reason("local model patch failed after 3 rounds"))
        self.assertIsNone(local_not_called_reason(None))


class _TooSlowWorker:
    def run(self, *, working_dir, **_):
        return _result(False, error="local_too_slow: the time budget allows only 360 output tokens",
                       working_dir=working_dir)


class ShadowReplayTests(unittest.TestCase):
    def test_local_too_slow_is_its_own_replay_status(self):
        fx = ReplayFixture(self)
        ran_tests = []
        row = fx.replay(_TooSlowWorker(), test_runner=lambda *a: ran_tests.append(a))

        self.assertEqual(row["status"], lsr.STATUS_LOCAL_TOO_SLOW)
        self.assertTrue(row["error"].startswith("local_too_slow"))
        self.assertEqual(ran_tests, [])  # the model never ran: nothing to test
        self.assertEqual(fx.replay_worktrees(), [])
        stats = lsr.variant_summary(db_path=fx.db_path)["variants"][0]
        self.assertEqual(stats["local_too_slow"], 1)
        self.assertEqual(stats["scored"], 0)


    def test_an_existing_table_is_migrated_to_accept_local_too_slow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "agent.db")
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE local_replays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    experiment_id TEXT NOT NULL,
                    issue INTEGER NULL,
                    model TEXT NOT NULL DEFAULT '',
                    prompt_variant TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'scored', 'no_tests', 'error')),
                    patch_applied INTEGER NOT NULL DEFAULT 0 CHECK (patch_applied IN (0, 1)),
                    compiled INTEGER NOT NULL DEFAULT 0 CHECK (compiled IN (0, 1)),
                    tests_total INTEGER NOT NULL DEFAULT 0,
                    tests_passed INTEGER NOT NULL DEFAULT 0,
                    rounds INTEGER NOT NULL DEFAULT 0,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    seconds REAL NOT NULL DEFAULT 0,
                    error TEXT NULL,
                    base_commit TEXT NULL,
                    merged_commit TEXT NULL,
                    test_files_json TEXT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT NULL,
                    claim_token TEXT NULL,
                    lease_expires_at TEXT NULL,
                    UNIQUE(experiment_id, prompt_variant)
                );
                INSERT INTO local_replays (experiment_id, prompt_variant, status, error, created_at)
                VALUES ('old', 'v', 'error', 'local model call timed out after 300s', '2026-11-01');
            """)
            conn.commit()
            conn.close()

            init_db(path).close()
            init_db(path).close()  # idempotent

            conn = sqlite3.connect(path)
            try:
                rows = conn.execute("SELECT experiment_id, status, error, samples FROM local_replays").fetchall()
                self.assertEqual(rows, [("old", "error", "local model call timed out after 300s", 1)])
                conn.execute("INSERT INTO local_replays (experiment_id, prompt_variant, status, created_at) "
                             "VALUES ('new', 'v', 'local_too_slow', '2026-11-13')")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO local_replays (experiment_id, prompt_variant, status, created_at) "
                                 "VALUES ('bad', 'v', 'bogus', '2026-11-13')")
                indexes = {r[1] for r in conn.execute("PRAGMA index_list(local_replays)")}
                self.assertIn("idx_local_replays_created_at", indexes)
                self.assertIsNone(conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='local_replays_pre_667'").fetchone())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
