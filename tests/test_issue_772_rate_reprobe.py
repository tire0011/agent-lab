"""#772: a cheap rate re-probe breaks the local_too_slow deadlock.

Admission (#667) refuses before any call, so a median below the line could
never change. These tests use a fake llama-server with ``timings`` and a
temporary rates file.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from modules import local_coding_worker as lcw
from modules.local_coding_worker import (
    LocalCodingWorker, LocalTooSlowError, build_prompt_parts, probe_rates,
)
from modules.local_model_rates import LocalModelRates
from modules.measurement_health import _normalize

# 2.10 tok/s generation, 7.02 tok/s prompt: 756 affordable tokens at 900 s.
SLOW = {"prompt_n": 702, "prompt_ms": 100_000.0, "predicted_n": 210, "predicted_ms": 100_000.0}


def _probe_timings(generation: float) -> dict:
    return {"prompt_n": 150, "prompt_ms": 20_000.0,
            "predicted_n": 48, "predicted_ms": 48 / generation * 1000.0}


class _Server:
    timeout = 900.0

    def __init__(self, generation: float = 3.0, fail: BaseException | None = None) -> None:
        self.timings = _probe_timings(generation)
        self.fail = fail
        self.cache_prompt = True
        self.last_timings = None
        self.calls: list[dict] = []

    async def count_tokens(self, text):
        return max(1, (len(text.encode()) + 3) // 4)

    async def complete(self, prompt, *, system="", temperature=0.0, max_tokens=1024, seed=None):
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens, "seed": seed,
                           "temperature": temperature, "cache_prompt": self.cache_prompt})
        if self.fail is not None:
            raise self.fail
        self.last_timings = dict(self.timings)
        return "ok"


class RateReprobeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "repo").mkdir()
        self.path = self.dir / "rates.json"
        self.store = LocalModelRates(self.path, model="m")
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in (lcw.ENV_RATE_REPROBE_AFTER, lcw.ENV_RATE_MAX_AGE_H,
                     lcw.ENV_RATE_REPROBE_COOLDOWN_MIN, lcw.ENV_MIN_USEFUL_PATCH_TOKENS):
            os.environ.pop(name, None)

    def _fill(self, *, age_hours: float = 0.0, samples: int = 5) -> None:
        when = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        for _ in range(samples):
            self.store._append({"prompt_tokens_per_second": 7.02,
                                "generation_tokens_per_second": 2.10}, now=when)

    def _build(self, server, *, admission=True, store=True, events=None):
        return asyncio.run(build_prompt_parts(
            "Edit value.txt", "", str(self.dir / "repo"), server, max_output_tokens=2048,
            n_ctx=20_000, n_ctx_source="test", rates=self.store.medians(), admission=admission,
            rate_store=self.store if store else None,
            on_reprobe=events.append if events is not None else None,
        ))

    def _refused(self, server, events=None) -> str:
        with self.assertRaises(LocalTooSlowError) as caught:
            self._build(server, events=events)
        return str(caught.exception)

    # -- the rates store -------------------------------------------------

    def test_samples_carry_timestamps_and_age(self):
        self._fill(age_hours=2)
        data = json.loads(self.path.read_text())["models"]["m"]
        self.assertEqual(5, len(data["sampled_at"]))
        self.assertAlmostEqual(2 * 3600, self.store.newest_sample_age(), delta=60)
        self.assertIsNone(LocalModelRates(self.dir / "none.json").newest_sample_age())

    def test_legacy_file_without_timestamps_loads_and_counts_as_old(self):
        self.path.write_text(json.dumps({"models": {"m": {
            "prompt_tokens_per_second": [7.02], "generation_tokens_per_second": [2.10]}}}))
        self.assertEqual((7.02, 2.10), self.store.medians())
        self.assertEqual(math.inf, self.store.newest_sample_age())
        self.assertEqual(0, self.store.consecutive_refusals())
        server = _Server(generation=3.0)
        self._build(server)  # old -> probed on the first refusal and admitted
        self.assertEqual(1, len(server.calls))

    def test_real_record_resets_the_refusal_counter(self):
        self._fill()
        self.assertEqual(1, self.store.record_refusal())
        self.assertEqual(2, self.store.record_refusal())
        self.assertEqual(2, self.store.consecutive_refusals())
        self.assertTrue(self.store.record(SLOW))
        self.assertEqual(0, self.store.consecutive_refusals())
        self.assertEqual((7.02, 2.10), self.store.medians())  # rates unchanged otherwise

    # -- probe_rates ----------------------------------------------------

    def test_probe_is_small_deterministic_and_uncached(self):
        self._fill()
        server = _Server(generation=3.0)
        result = asyncio.run(probe_rates(server, self.store))
        self.assertTrue(result["stored"])
        call = server.calls[0]
        self.assertEqual(48, call["max_tokens"])
        self.assertEqual(lcw.PROBE_SEED, call["seed"])
        self.assertEqual(0.0, call["temperature"])
        self.assertFalse(call["cache_prompt"])
        self.assertTrue(server.cache_prompt)  # restored
        self.assertLess(asyncio.run(server.count_tokens(lcw.PROBE_PROMPT)), 220)
        self.assertAlmostEqual(3.0, self.store.medians()[1])

    # -- admission ------------------------------------------------------

    def test_three_refusals_with_old_rates_trigger_exactly_one_probe(self):
        self._fill(age_hours=48)
        server = _Server(generation=2.0)
        events: list[dict] = []
        for _ in range(3):
            self._refused(server, events)
        self.assertEqual(1, len(server.calls))
        self.assertEqual(1, len(events))
        self.assertEqual("old_rates", events[0]["trigger"])
        self.assertAlmostEqual(2.10, events[0]["old_generation_tokens_per_second"])
        self.assertAlmostEqual(2.0, events[0]["new_generation_tokens_per_second"])

    def test_repeated_refusals_with_fresh_rates_probe_on_the_third(self):
        self._fill()
        server = _Server(generation=2.0)
        messages = [self._refused(server) for _ in range(2)]
        self.assertEqual([], server.calls)
        self.assertIn("no re-probe yet (1 refusals in a row)", messages[1])
        self._refused(server)
        self.assertEqual(1, len(server.calls))

    def test_probe_at_three_tokens_per_second_admits_the_call(self):
        self._fill(age_hours=48)
        server = _Server(generation=3.0)
        events: list[dict] = []
        plan = self._build(server, events=events)
        self.assertIsNotNone(plan)
        self.assertTrue(events[0]["admitted"])
        self.assertGreaterEqual(events[0]["affordable_output_tokens"], 768)
        self.assertEqual(0, self.store.consecutive_refusals())

    def test_probe_at_two_still_refuses_and_cooldown_blocks_a_second(self):
        self._fill(age_hours=48)
        server = _Server(generation=2.0)
        first = self._refused(server)
        self.assertIn("the local model was not called [rates: newest sample", first)
        self.assertIn("re-probe measured generation 2.00 tok/s", first)
        self.assertIn("still too slow", first)
        # The fresh probe sample is no longer old, but refusals pile up again.
        second = self._refused(server)
        third = self._refused(server)
        self.assertIn("(cooldown)", third)
        self.assertEqual(1, len(server.calls))
        self.assertTrue(second.startswith("local_too_slow: the time budget allows only "))
        # measurement_health treats these rows as one streak.
        self.assertEqual(_normalize(first), _normalize(third))

    def test_cooldown_expiry_allows_another_probe(self):
        self._fill(age_hours=48)
        os.environ[lcw.ENV_RATE_REPROBE_COOLDOWN_MIN] = "0"
        server = _Server(generation=2.0)
        for _ in range(3):
            self._refused(server)
        self.assertEqual(2, len(server.calls))  # 1st: old rates, 3rd: refusals

    def test_failed_probe_still_refuses_and_starts_the_cooldown(self):
        self._fill(age_hours=48)
        server = _Server(fail=TimeoutError("timed out"))
        message = self._refused(server)
        self.assertIn("re-probe failed", message)
        self._refused(server)
        self.assertEqual(1, len(server.calls))
        self.assertAlmostEqual(2.10, self.store.medians()[1])  # window kept

    def test_admission_off_never_probes(self):
        self._fill(age_hours=48)
        server = _Server(generation=3.0)
        for _ in range(4):
            self._build(server, admission=False)
        self.assertEqual([], server.calls)
        self.assertEqual(0, self.store.consecutive_refusals())

    def test_without_a_rate_store_the_old_behaviour_holds(self):
        self._fill(age_hours=48)
        server = _Server(generation=3.0)
        with self.assertRaises(LocalTooSlowError) as caught:
            self._build(server, store=False)
        self.assertTrue(str(caught.exception).endswith("the local model was not called"))
        self.assertEqual([], server.calls)


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def info(self, _phase, event, message, **fields):
        self.events.append((event, message, fields))

    warning = info

    def all(self, name: str) -> list[dict]:
        return [fields for event, _message, fields in self.events if event == name]


class WorkerReprobeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "value.txt").write_text("old\n", encoding="utf-8")
        self.rates_path = Path(self._tmp.name) / "rates.json"
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        LocalModelRates(self.rates_path)._append(
            {"prompt_tokens_per_second": 7.02, "generation_tokens_per_second": 2.10}, now=old)

    def run_worker(self, server):
        log = _Log()
        worker = LocalCodingWorker(client=server, max_tokens=2048, rates_path=self.rates_path)
        result = worker.run(task="Edit value.txt", working_dir=str(self.repo),
                            sandbox="workspace-write", experiment_logger=log)
        return result, log

    def test_worker_logs_the_reprobe_and_reports_it_in_the_refusal(self):
        result, log = self.run_worker(_Server(generation=2.0))
        self.assertFalse(result.success)
        self.assertTrue(result.error.startswith("local_too_slow: the time budget allows only "))
        self.assertIn("re-probe measured generation 2.00 tok/s", result.error)
        (probe,) = log.all("rate_reprobe")
        self.assertFalse(probe["admitted"])
        self.assertAlmostEqual(2.10, probe["old_generation_tokens_per_second"])
        self.assertAlmostEqual(2.0, probe["new_generation_tokens_per_second"])
        self.assertEqual(1, len(log.all("local_too_slow")))


if __name__ == "__main__":
    unittest.main()
