"""#642: local calls expose timings, timeouts, and throughput-sized prompts.

All tests use fake clients; no llama-server is contacted.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from modules.experiment_handover import HANDOVER_HEADING
from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError
from modules.local_coding_worker import (
    _FILE_LEVELS,
    MIN_OUTPUT_TOKENS,
    ContextTooSmallError,
    LocalTooSlowError,
    LocalCodingWorker,
    build_prompt,
    time_budget,
)
from modules.local_model_rates import (
    ENV_RATES_PATH,
    MAX_SAMPLES,
    LocalModelRates,
    default_rates_path,
)

_TIMEOUT_ERROR = "local model call timed out after 300s (prompt ~100 tokens, max_tokens 1024)"
_FAST = {"prompt_n": 1000, "prompt_ms": 1000, "predicted_n": 100, "predicted_ms": 1000}


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, _phase, event, _message, **fields):
        self.events.append((event, fields))

    warning = info

    def all(self, name: str) -> list[dict]:
        return [fields for event, fields in self.events if event == name]


class _Server:
    """A fake llama-server: answers (or raises) in order, the last one repeating."""

    timeout = 300.0

    def __init__(self, *answers: str | BaseException, timings: dict | list | None = None,
                 n_ctx: int = 20_000) -> None:
        self.answers = list(answers) or ["not a diff"]
        self.timings = timings
        self.n_ctx = n_ctx
        self.last_timings = None
        self.calls: list[dict] = []

    async def context_size(self):
        return self.n_ctx

    async def count_tokens(self, text):
        return max(1, (len(text.encode()) + 3) // 4)

    async def complete(self, prompt, *, system="", temperature=0.0, max_tokens=1024, **_kwargs):
        call_no = len(self.calls)
        index = min(call_no, len(self.answers) - 1)
        self.calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens})
        if isinstance(self.timings, list):
            # Per call; after the list ends the attribute is left untouched, so
            # only the worker's own reset keeps stale timings out of the log.
            if call_no < len(self.timings):
                self.last_timings = self.timings[call_no]
        else:
            self.last_timings = self.timings
        answer = self.answers[index]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _big_handover_task() -> str:
    handover = "\n".join(f"finding {number}" for number in range(4000))
    return f"Edit value.txt\n\n{HANDOVER_HEADING}\n{handover}"


class _RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "value.txt").write_text("old\n", encoding="utf-8")
        self.rates_path = Path(self.temp.name) / "rates.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed_rates(self, prompt_rate: float, generation_rate: float) -> None:
        # timings that produce exactly these rates (tokens over one second)
        assert LocalModelRates(self.rates_path).record({
            "prompt_n": prompt_rate, "prompt_ms": 1000,
            "predicted_n": generation_rate, "predicted_ms": 1000,
        })

    def run_worker(self, server, *, task="Edit value.txt", max_tokens=1024, **kwargs):
        log = _Log()
        worker = LocalCodingWorker(client=server, max_tokens=max_tokens,
                                   rates_path=self.rates_path, **kwargs)
        result = worker.run(task=task, working_dir=str(self.repo), sandbox="workspace-write",
                            experiment_logger=log)
        return worker, result, log


class ClientTests(unittest.TestCase):
    def _mock_post(self, cls, **post):
        mocked = AsyncMock()
        mocked.__aenter__ = AsyncMock(return_value=mocked)
        mocked.__aexit__ = AsyncMock(return_value=None)
        mocked.post = AsyncMock(**post)
        cls.return_value = mocked

    def test_client_timeout_names_limit_prompt_and_output(self):
        client = LlamaCppClient(base_url="http://llama.test/v1", timeout=300)
        with patch("modules.llama_cpp_client.httpx.AsyncClient") as cls:
            self._mock_post(cls, side_effect=httpx.ReadTimeout(""))
            with self.assertRaises(LlamaCppClientError) as caught:
                asyncio.run(client.chat_completion(
                    [{"role": "user", "content": "a reasonably sized prompt"}], max_tokens=77,
                ))
        self.assertRegex(str(caught.exception),
                         r"^local model call timed out after 300s \(prompt ~\d+ tokens, max_tokens 77\)$")
        self.assertIsNone(client.last_timings)

    def test_client_timeout_message_survives_non_numeric_timeout(self):
        client = LlamaCppClient(base_url="http://llama.test/v1", timeout=httpx.Timeout(5.0))
        with patch("modules.llama_cpp_client.httpx.AsyncClient") as cls:
            self._mock_post(cls, side_effect=httpx.ConnectTimeout(""))
            with self.assertRaisesRegex(LlamaCppClientError, "timed out after"):
                asyncio.run(client.chat_completion([{"role": "user", "content": "x"}]))

    def test_client_keeps_llama_server_timings_and_resets_them(self):
        timings = {"prompt_n": 100, "prompt_ms": 5000, "predicted_n": 20,
                   "predicted_ms": 4000, "ignored": 1}
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [], "timings": timings}
        client = LlamaCppClient(base_url="http://llama.test/v1")
        with patch("modules.llama_cpp_client.httpx.AsyncClient") as cls:
            self._mock_post(cls, return_value=response)
            asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
        self.assertEqual({key: timings[key] for key in timings if key != "ignored"}, client.last_timings)
        # A second call that fails must not leave the first call's timings behind.
        with patch("modules.llama_cpp_client.httpx.AsyncClient") as cls:
            self._mock_post(cls, side_effect=httpx.ReadTimeout(""))
            with self.assertRaises(LlamaCppClientError):
                asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
        self.assertIsNone(client.last_timings)

    def test_client_requests_fixed_slot_cache_and_normalizes_cached_tokens(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "choices": [],
            "timings": {"prompt_n": 100, "prompt_ms": 50, "cache_n": 75},
        }
        client = LlamaCppClient(base_url="http://llama.test/v1")
        with patch.dict(os.environ, {"LLAMA_CPP_SLOT": "3"}), \
                patch("modules.llama_cpp_client.httpx.AsyncClient") as cls:
            client = LlamaCppClient(base_url="http://llama.test/v1")
            self._mock_post(cls, return_value=response)
            asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
            body = cls.return_value.__aenter__.return_value.post.call_args.kwargs["json"]
        self.assertIs(True, body["cache_prompt"])
        self.assertEqual(75, client.last_timings["tokens_cached"])

    def test_slot_n_past_is_not_misreported_as_reused_tokens(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "choices": [], "timings": {"prompt_n": 10}, "n_past": 999,
        }
        client = LlamaCppClient(base_url="http://llama.test/v1")
        with patch("modules.llama_cpp_client.httpx.AsyncClient") as cls:
            self._mock_post(cls, return_value=response)
            asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
        self.assertNotIn("tokens_cached", client.last_timings)

    def test_cache_can_be_disabled_for_replay_measurement(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [], "timings": {"prompt_n": 1}}
        with patch.dict(os.environ, {"LLAMA_CPP_CACHE_PROMPT": "off"}), \
                patch("modules.llama_cpp_client.httpx.AsyncClient") as cls:
            client = LlamaCppClient(base_url="http://llama.test/v1")
            self._mock_post(cls, return_value=response)
            asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
            body = cls.return_value.__aenter__.return_value.post.call_args.kwargs["json"]
        self.assertIs(False, body["cache_prompt"])


class WorkerTimeoutTests(_RepoCase):
    def test_patch_timeout_is_counted_and_stops_cleanly(self):
        server = _Server(LlamaCppClientError(_TIMEOUT_ERROR))
        _, result, log = self.run_worker(server)
        self.assertFalse(result.success)
        self.assertIn("timed out after 300s", result.error)
        self.assertEqual(1, len(server.calls))  # no retry after a timeout
        self.assertEqual("timeout", log.all("local_model_call")[0]["outcome"])
        rounds = log.all("local_patch_round")
        self.assertEqual([("model_timeout", 1)], [(r["outcome"], r["round"]) for r in rounds])

    def test_raw_httpx_timeout_with_empty_message_gets_a_named_reason(self):
        server = _Server(httpx.ReadTimeout(""))
        _, result, log = self.run_worker(server)
        self.assertFalse(result.success)
        self.assertTrue(result.error.strip())
        self.assertIn("timed out after 300", result.error)
        self.assertEqual("model_timeout", log.all("local_patch_round")[0]["outcome"])

    def test_timeout_halfway_keeps_earlier_rounds_and_stops(self):
        server = _Server("not a diff", LlamaCppClientError(_TIMEOUT_ERROR), "never asked")
        _, result, log = self.run_worker(server)
        self.assertFalse(result.success)
        self.assertEqual(2, len(server.calls))
        outcomes = [r["outcome"] for r in log.all("local_patch_round")]
        self.assertEqual(2, len(outcomes))
        self.assertEqual("model_timeout", outcomes[1])
        self.assertEqual(["ok", "timeout"], [e["outcome"] for e in log.all("local_model_call")])

    def test_other_empty_errors_never_leave_an_empty_error(self):
        server = _Server(RuntimeError(""))
        _, result, log = self.run_worker(server)
        self.assertFalse(result.success)
        self.assertEqual("RuntimeError (no message)", result.error)
        self.assertEqual("error", log.all("local_model_call")[0]["outcome"])

    def test_plan_call_timeout_is_logged_and_hopeless_patch_is_refused(self):
        server = _Server(LlamaCppClientError(_TIMEOUT_ERROR), "not a diff")
        worker, result, log = self.run_worker(server, split_tasks=True, planner=lambda _p: None,
                                              task=_big_handover_task())
        calls = log.all("local_model_call")
        self.assertEqual(("plan", "timeout"), (calls[0]["call_type"], calls[0]["outcome"]))
        self.assertEqual(len(calls), 1)
        # The initial plan remains inspectable; no second, time-squeezed patch
        # plan or model call is created after the timeout.
        self.assertIsNotNone(worker.last_plan)
        self.assertIsNone(worker.last_plan.time_budget_tokens)
        self.assertTrue(result.error.startswith("local_too_slow"), result.error)
        self.assertEqual(len(log.all("local_too_slow")), 1)
        self.assertEqual(log.all("local_patch_round"), [])

    def test_first_patch_timeout_refuses_the_next_hopeless_run(self):
        task = _big_handover_task()
        first, result, _ = self.run_worker(_Server(LlamaCppClientError(_TIMEOUT_ERROR)),
                                           task=task)
        self.assertFalse(result.success)
        self.assertNotEqual((None, None), first.rates.medians())
        second, second_result, log = self.run_worker(
            _Server("not a diff"), task=task, split_tasks=True)
        self.assertIsNone(second.last_plan)
        self.assertTrue(second_result.error.startswith("local_too_slow"), second_result.error)
        self.assertEqual(log.all("local_model_call"), [])
        self.assertEqual(len(log.all("local_too_slow")), 1)

    def test_interrupted_model_call_is_logged_and_propagated_without_a_fake_sample(self):
        server = _Server(KeyboardInterrupt())
        log = _Log()
        worker = LocalCodingWorker(client=server, max_tokens=1024, rates_path=self.rates_path)
        with self.assertRaises(KeyboardInterrupt):
            worker.run(task="Edit value.txt", working_dir=str(self.repo),
                       sandbox="workspace-write", experiment_logger=log)
        self.assertEqual("interrupted", log.all("local_model_call")[0]["outcome"])
        self.assertEqual((None, None), worker.rates.medians())


class TimingsLoggingTests(_RepoCase):
    def test_timings_are_forwarded_to_model_call_event(self):
        timings = {"prompt_n": 80, "prompt_ms": 4000, "predicted_n": 10, "predicted_ms": 2000}
        _, _, log = self.run_worker(_Server("not a diff", timings=timings))
        event = log.all("local_model_call")[0]
        self.assertEqual(timings, event["timings"])
        self.assertEqual(20.0, event["prompt_tokens_per_second"])
        self.assertEqual(5.0, event["generation_tokens_per_second"])
        self.assertEqual("patch", event["call_type"])
        self.assertEqual(1, event["round"])
        self.assertGreaterEqual(event["wall_seconds"], 0)

    def test_cached_prompt_share_is_logged(self):
        timings = {"prompt_n": 30, "prompt_ms": 1000, "predicted_n": 1,
                   "predicted_ms": 100, "tokens_cached": 70}
        _, _, log = self.run_worker(_Server("not a diff", timings=timings))
        event = log.all("local_model_call")[0]
        self.assertEqual(70, event["prompt_tokens_cached"])
        self.assertEqual(100, event["prompt_tokens_total"])
        self.assertEqual(0.7, event["prompt_cache_ratio"])

    def test_second_run_does_not_reuse_stale_timings(self):
        server = _Server("not a diff", timings=[_FAST])
        worker = LocalCodingWorker(client=server, max_tokens=1024, rates_path=self.rates_path)
        for _ in range(2):
            log = _Log()
            worker.run(task="Edit value.txt", working_dir=str(self.repo),
                       sandbox="workspace-write", experiment_logger=log)
        self.assertTrue(all(e["timings"] is None for e in log.all("local_model_call")))
        # Only the one call that reported timings was measured.
        self.assertEqual((1000.0, 100.0), worker.rates.medians())

    def test_budget_fields_are_logged_next_to_n_ctx(self):
        self.seed_rates(20.0, 20.0)
        _, _, log = self.run_worker(_Server("not a diff"), task=_big_handover_task())
        budget = log.all("local_prompt_budget")[0]
        self.assertEqual(20_000, budget["n_ctx"])
        self.assertEqual(3776, budget["time_budget_tokens"])
        self.assertTrue(budget["time_budget_limited"])
        self.assertEqual(1024, budget["output_tokens"])


class TimeBudgetTests(_RepoCase):
    def test_shadow_replay_budget_admits_useful_output_at_measured_rates(self):
        long = time_budget(1500, 768, (7.13, 2.61))
        short = time_budget(300, 768, (7.13, 2.61))
        self.assertGreaterEqual(long.output_tokens, 768)
        self.assertGreaterEqual(long.prompt_tokens, 6000)
        self.assertLess(short.prompt_tokens, 6000)
        long_client = _Server()
        long_client.timeout = 1500.0
        plan = asyncio.run(build_prompt(
            "Edit value.txt", str(self.repo), long_client, max_output_tokens=768,
            rates=(7.13, 2.61),
        ))
        self.assertEqual(plan.output_tokens, 768)
        short_client = _Server()
        short_client.timeout = 300.0
        with self.assertRaisesRegex(LocalTooSlowError, r"300s timeout"):
            asyncio.run(build_prompt(
                "Edit value.txt", str(self.repo), short_client, max_output_tokens=768,
                rates=(7.13, 2.61),
            ))

    def test_each_call_timeout_is_capped_to_remaining_worker_budget(self):
        server = _Server()
        server.timeout = 1500.0
        worker = LocalCodingWorker(client=server, rates_path=self.rates_path)
        worker._configured_call_timeout = 1500.0
        worker._run_deadline = 1000.0
        with patch("modules.local_coding_worker.time.monotonic", return_value=250.0):
            self.assertEqual(worker._apply_remaining_call_timeout(), 750.0)
        self.assertEqual(server.timeout, 750.0)

    def test_measured_rates_cut_handover_but_absent_rates_do_not(self):
        server = _Server()
        task = _big_handover_task()
        unrestricted = asyncio.run(build_prompt(task, str(self.repo), server, max_output_tokens=1024))
        no_rates = asyncio.run(build_prompt(task, str(self.repo), server, max_output_tokens=1024,
                                            rates=(None, None)))
        half = asyncio.run(build_prompt(task, str(self.repo), server, max_output_tokens=1024,
                                        rates=(20.0, None)))
        fitted = asyncio.run(build_prompt(task, str(self.repo), server, max_output_tokens=1024,
                                          rates=(20.0, 20.0)))
        for plan in (unrestricted, no_rates, half):
            self.assertIsNone(plan.time_budget_tokens)
            self.assertFalse(plan.time_budget_limited)
            self.assertEqual(1024, plan.output_tokens)
            self.assertEqual(unrestricted.prompt, plan.prompt)
        self.assertFalse(unrestricted.handover_cut)  # n_ctx alone allows the whole handover
        self.assertEqual(3776, fitted.time_budget_tokens)  # (0.8*300 - 1024/20) * 20
        self.assertTrue(fitted.time_budget_limited)
        self.assertTrue(fitted.handover_cut)
        self.assertLess(fitted.handover_tokens_kept, unrestricted.handover_tokens_kept)
        self.assertLessEqual(fitted.prompt_tokens, fitted.time_budget_tokens)

    def test_no_measured_rates_changes_nothing_in_the_worker(self):
        _, _, log = self.run_worker(_Server(), task=_big_handover_task())
        self.assertFalse(self.rates_path.exists())
        budget = log.all("local_prompt_budget")[0]
        self.assertIsNone(budget["time_budget_tokens"])
        self.assertFalse(budget["handover_cut"])

    def test_slow_generation_and_default_max_tokens_do_not_disable_the_worker(self):
        # Regression for experiment 689600ce: 4096/2 tok/s = 2048 s > 240 s gave
        # time_budget_tokens=0 and every later run failed with context_too_small.
        # #667: such a call cannot produce a useful patch, so with the default
        # admission it is refused as local_too_slow (never context_too_small);
        # with admission off (LOCAL_MIN_USEFUL_PATCH_TOKENS=0) the #642 path holds.
        self.seed_rates(20.0, 2.0)
        plan = asyncio.run(build_prompt(_big_handover_task(), str(self.repo), _Server(),
                                        max_output_tokens=4096, rates=(20.0, 2.0), admission=False))
        self.assertEqual(2240, plan.time_budget_tokens)  # 128 s output leaves 112 s for prompt
        self.assertEqual(MIN_OUTPUT_TOKENS, plan.output_tokens)
        with patch.dict(os.environ, {"LOCAL_MIN_USEFUL_PATCH_TOKENS": "0"}):
            server = _Server()
            _, result, log = self.run_worker(server, task=_big_handover_task(), max_tokens=4096)
        self.assertFalse(log.all("local_context_too_small"))
        self.assertEqual(MIN_OUTPUT_TOKENS, server.calls[0]["max_tokens"])
        self.assertGreaterEqual(len(log.all("local_patch_round")), 1)
        self.assertNotIn("context_too_small", result.error or "")

        server = _Server()
        _, result, log = self.run_worker(server, task=_big_handover_task(), max_tokens=4096)
        self.assertFalse(server.calls)
        self.assertTrue(result.error.startswith("local_too_slow"), result.error)
        self.assertFalse(log.all("local_context_too_small"))

    def test_prompt_over_time_budget_still_calls_with_the_minimal_prompt(self):
        # 0.1 prompt tokens/s affords ~18 tokens: less than the instructions.
        self.seed_rates(0.1, 20.0)
        server = _Server()
        worker, _, log = self.run_worker(server, task=_big_handover_task())
        self.assertFalse(log.all("local_context_too_small"))
        self.assertTrue(server.calls)
        plan = worker.last_plan
        self.assertTrue(plan.time_budget_exceeded)
        self.assertTrue(plan.time_budget_limited)
        self.assertEqual(0, plan.handover_tokens_kept)
        # The changing tail is discarded before the stable file prefix (#645).
        self.assertEqual(0, plan.file_level)

    def test_n_ctx_remains_a_hard_limit(self):
        with self.assertRaises(ContextTooSmallError):
            asyncio.run(build_prompt("Edit value.txt " + "word " * 4000, str(self.repo),
                                     _Server(n_ctx=2048), max_output_tokens=1024, rates=(20.0, 20.0)))

    def test_worker_recovers_when_faster_rates_are_measured(self):
        self.seed_rates(0.1, 20.0)
        server = _Server("not a diff", timings=_FAST)
        worker, _, _ = self.run_worker(server, task=_big_handover_task())
        self.assertTrue(worker.last_plan.time_budget_exceeded)
        # The rejected rounds measured fast rates; the median moves past the slow seed.
        worker, _, _ = self.run_worker(_Server("not a diff", timings=_FAST), task=_big_handover_task())
        self.assertFalse(worker.last_plan.time_budget_exceeded)
        self.assertGreater(worker.last_plan.time_budget_tokens, 3776)

    def test_time_budget_rejects_unusable_inputs(self):
        for timeout, rates in ((None, (20, 20)), (0, (20, 20)), (300, (0, 20)),
                               (300, (20, float("nan"))), (float("inf"), (20, 20))):
            self.assertIsNone(time_budget(timeout, 1024, rates))

    def test_time_limited_prompt_skips_planning_and_spends_one_call_on_patch(self):
        self.seed_rates(20.0, 20.0)
        server = _Server("not a diff")
        _, _, log = self.run_worker(server, task=_big_handover_task(), split_tasks=True)
        self.assertEqual("patch", log.all("local_model_call")[0]["call_type"])
        split = log.all("local_task_split")[0]
        self.assertTrue(split["skipped"])
        self.assertIn("time budget", split["reason"])

    def test_prompt_within_time_budget_still_plans(self):
        self.seed_rates(20.0, 20.0)
        server = _Server("not a diff")
        _, _, log = self.run_worker(server, split_tasks=True, planner=lambda _p: None)
        self.assertEqual("plan", log.all("local_model_call")[0]["call_type"])


class RatesStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nested" / "rates.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_and_corrupt_files_read_as_unmeasured_and_recover(self):
        store = LocalModelRates(self.path)
        self.assertEqual((None, None), store.medians())
        self.path.parent.mkdir(parents=True)
        for broken in ("{not json", "[]", '{"models": []}', '{"models": {"default": 3}}',
                       '{"models": {"default": {"prompt_tokens_per_second": "x"}}}'):
            self.path.write_text(broken, encoding="utf-8")
            self.assertEqual((None, None), store.medians(), broken)
        self.assertTrue(store.record(_FAST))
        self.assertEqual((1000.0, 100.0), store.medians())

    def test_half_written_temporary_file_is_ignored(self):
        store = LocalModelRates(self.path)
        store.record(_FAST)
        (self.path.parent / f".{self.path.name}.crash.tmp").write_text("{trunc", encoding="utf-8")
        self.assertEqual((1000.0, 100.0), store.medians())
        store.record(_FAST)
        self.assertEqual([".rates.json.crash.tmp", "rates.json"],
                         sorted(p.name for p in self.path.parent.iterdir()))

    def test_unusable_timings_are_not_recorded(self):
        store = LocalModelRates(self.path)
        for timings in (None, {}, {"prompt_n": 0, "prompt_ms": 0},
                        {"prompt_n": 10, "prompt_ms": float("nan")},
                        {"prompt_n": True, "prompt_ms": 5}, {"predicted_n": 5, "predicted_ms": -1}):
            self.assertFalse(store.record(timings), timings)
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"models": {"default": {
            "prompt_tokens_per_second": [0, -5, 7.0], "generation_tokens_per_second": [None]}}}),
            encoding="utf-8")
        self.assertEqual((7.0, None), store.medians())

    def test_write_failure_never_raises(self):
        self.path.mkdir(parents=True)  # the target is a directory: replace fails
        self.assertFalse(LocalModelRates(self.path).record(_FAST))
        self.assertFalse(LocalModelRates(self.path).record_timeout(
            prompt_tokens=1000, timeout_seconds=300, minimum_output_tokens=256,
        ))

    def test_timeout_bootstrap_is_conservative_repeatable_and_bounded(self):
        store = LocalModelRates(self.path)
        for _ in range(MAX_SAMPLES + 5):
            self.assertTrue(store.record_timeout(
                prompt_tokens=6000, timeout_seconds=300, minimum_output_tokens=256,
            ))
        prompt_rate, generation_rate = store.medians()
        self.assertEqual(20.0, prompt_rate)
        self.assertAlmostEqual(256 / 120, generation_rate)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        entry = data["models"]["default"]
        # #667: guesses are kept apart from measurements.
        for key in ("estimated_prompt_tokens_per_second", "estimated_generation_tokens_per_second"):
            self.assertEqual(MAX_SAMPLES, len(entry[key]))
        self.assertFalse(entry["prompt_tokens_per_second"] or entry["generation_tokens_per_second"])

    def test_timeout_bootstrap_rejects_invalid_partial_observations(self):
        store = LocalModelRates(self.path)
        for prompt, timeout, output in ((0, 300, 256), (10, 0, 256),
                                        (10, float("nan"), 256), (10, 300, 0)):
            self.assertFalse(store.record_timeout(
                prompt_tokens=prompt, timeout_seconds=timeout,
                minimum_output_tokens=output,
            ))
        self.assertFalse(self.path.exists())

    def test_rolling_window_and_per_model_separation(self):
        slow = LocalModelRates(self.path, model="7b")
        fast = LocalModelRates(self.path, model="1.5b-q4")
        for _ in range(MAX_SAMPLES + 5):
            slow.record({"prompt_n": 10, "prompt_ms": 1000, "predicted_n": 2, "predicted_ms": 1000})
        fast.record(_FAST)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(MAX_SAMPLES, len(data["models"]["7b"]["prompt_tokens_per_second"]))
        self.assertEqual((10.0, 2.0), slow.medians())
        self.assertEqual((1000.0, 100.0), fast.medians())

    def test_default_path_is_isolated_from_cwd(self):
        with patch.dict(os.environ, {ENV_RATES_PATH: "/tmp/x/rates.json"}):
            self.assertEqual(Path("/tmp/x/rates.json"), default_rates_path())
        env = {k: v for k, v in os.environ.items() if k != ENV_RATES_PATH}
        env["AGENT_DB_PATH"] = "/var/lib/nexus/agent.db"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Path("/var/lib/nexus/local-model-rates.json"), default_rates_path())


if __name__ == "__main__":
    unittest.main()
