"""One rate-based timeout drives both the shadow HTTP client and the monitor (#796).

Part 2a of #782: all 48 planner_shadow rows ended in 'local model call timed
out after 55s' because nexus_tick.py hard-coded ``LlamaCppClient(timeout=55)``
while the monitor joined on its own fixed timeout. Now the pure function
:func:`modules.nexus_planner.shadow_timeout_seconds` computes one T from the
measured LocalModelRates medians (``prompt_tokens/prompt_tps + 120/gen_tps + 10``,
capped at 240 s, fallback 120 s without samples), the monitor passes T to the
shadow callable (``shadow(prompt, timeout, allowed)``) whose client is built with
``timeout=T``, and the monitor joins the worker on T+5 so the request can never
outlive the monitor. The clock starts after ``worker.start()``. No schema or DB
changes (#798 later adds the allowed-set enum and the reason maxLength).
"""

import inspect
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import nexus_planner
from modules.local_model_rates import LocalModelRates
from modules.nexus_planner import (
    IssuePlanner,
    estimate_tokens,
    shadow_timeout_seconds,
)
from modules.planner_shadow import list_shadow

PAID = '{"issue": 2, "reason": "paid choice"}'
ISSUES = [
    {"number": 1, "title": "one", "body": ""},
    {"number": 2, "title": "two", "body": ""},
]


class FakeRates:
    """A LocalModelRates stand-in: ``medians()`` returns the given pair."""

    def __init__(self, medians):
        self._medians = medians

    def medians(self):
        return self._medians


class ShadowTimeoutSecondsTests(unittest.TestCase):
    """The pure function with fake rates."""

    def test_normal_rates(self):
        # 837 tokens read at 20 t/s plus a 120-token answer at 4 t/s plus 10 s.
        seconds = shadow_timeout_seconds(837, FakeRates((20.0, 4.0)))
        self.assertAlmostEqual(seconds, 837 / 20 + 120 / 4 + 10)
        self.assertAlmostEqual(seconds, 81.85)

    def test_capped_at_240_seconds(self):
        self.assertEqual(shadow_timeout_seconds(10 ** 9, FakeRates((1.0, 0.5))), 240.0)
        # Exactly at the cap stays at the cap: 4000/20 + 120/4 + 10 = 240.
        self.assertEqual(shadow_timeout_seconds(4000, FakeRates((20.0, 4.0))), 240.0)
        # Just above the cap is still the cap.
        self.assertEqual(shadow_timeout_seconds(4001, FakeRates((20.0, 4.0))), 240.0)

    def test_no_samples_fall_back_to_120(self):
        for medians in ((None, None), (None, 4.0), (20.0, None)):
            with self.subTest(medians=medians):
                self.assertEqual(shadow_timeout_seconds(837, FakeRates(medians)), 120.0)

    def test_zero_or_negative_rates_fall_back_to_120(self):
        for medians in ((0.0, 4.0), (20.0, 0.0), (0, 0), (-3.0, 4.0), (20.0, -1.0)):
            with self.subTest(medians=medians):
                self.assertEqual(shadow_timeout_seconds(837, FakeRates(medians)), 120.0)

    def test_accepts_a_plain_medians_pair(self):
        self.assertAlmostEqual(shadow_timeout_seconds(100, (10.0, 10.0)),
                               100 / 10 + 120 / 10 + 10)

    def test_real_rates_object_without_samples_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            rates = LocalModelRates(str(Path(tmp) / "missing.json"), model="m")
            self.assertEqual(rates.medians(), (None, None))
            self.assertEqual(shadow_timeout_seconds(500, rates), 120.0)

    def test_real_rates_object_reads_its_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rates.json"
            path.write_text(json.dumps({"models": {"m": {
                "prompt_tokens_per_second": [20.0, 20.0],
                "generation_tokens_per_second": [4.0, 4.0],
            }}}), encoding="utf-8")
            rates = LocalModelRates(path, model="m")
            self.assertEqual(rates.medians(), (20.0, 4.0))
            self.assertAlmostEqual(shadow_timeout_seconds(837, rates),
                                   837 / 20 + 120 / 4 + 10)


class ShadowMonitorTimeoutTests(unittest.TestCase):
    """The monitor computes T, hands it to the shadow callable and joins on T+5."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")
        env = patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("NEXUS_PLANNER_SHADOW_PROMPT_TOKENS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def wait_for_row(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if list_shadow(self.db)["count"]:
                return list_shadow(self.db)["rows"][0]
            time.sleep(0.01)
        self.fail("shadow row was not written")

    def test_shadow_callable_receives_the_same_t_the_monitor_used(self):
        rates = FakeRates((50.0, 5.0))
        received = []

        def fake_shadow(prompt, timeout, allowed):
            received.append((prompt, timeout))
            return '{"issue": 1, "reason": "local"}'

        planner = IssuePlanner(lambda _: PAID, shadow=fake_shadow,
                               db_path=self.db, rates=rates)
        choice = planner.choose(ISSUES, tick_id="tick-t")
        self.assertEqual(choice.issue, 2)
        row = self.wait_for_row()
        self.assertEqual(len(received), 1)
        prompt, timeout = received[0]
        # The T handed to the callable is exactly the rate-based timeout for the
        # prompt that was sent, computed from the same rates the monitor used.
        expected = shadow_timeout_seconds(estimate_tokens(prompt), rates)
        self.assertEqual(timeout, expected)
        self.assertGreater(timeout, 0)
        # It finished well within the monitor's join, so the row is clean.
        self.assertIsNone(row["error"])
        self.assertEqual(row["local_issue"], 1)
        self.assertEqual(row["local_valid"], 1)
        self.assertEqual(row["prompt_tokens"], estimate_tokens(prompt))

    def test_sleeping_past_t_records_timeout_with_seconds_at_least_t(self):
        t = 0.05
        grace = 0.25
        received = []
        with patch.object(nexus_planner, "shadow_timeout_seconds", lambda pt, r: t), \
                patch.object(nexus_planner, "SHADOW_MONITOR_GRACE_SECONDS", grace):
            def slow(prompt, timeout, allowed):
                received.append(timeout)
                time.sleep(1.0)
                return "{}"

            planner = IssuePlanner(lambda _: PAID, shadow=slow,
                                   db_path=self.db, rates=FakeRates((1.0, 1.0)))
            started = time.monotonic()
            choice = planner.choose(ISSUES, tick_id="tick-slow")
            # choose() never blocks on the shadow.
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(choice.issue, 2)
            row = self.wait_for_row()
        # The callable still received the same T.
        self.assertEqual(received, [t])
        match = re.fullmatch(r"timeout after (\S+)s", row["error"] or "")
        self.assertIsNotNone(match, row["error"])
        seconds = float(match.group(1))
        self.assertGreaterEqual(seconds, t)
        # The monitor joined on exactly T + grace, so the request (bounded by
        # T) could never outlive it.
        self.assertAlmostEqual(seconds, t + grace)
        self.assertGreaterEqual(row["seconds"], t)
        self.assertIsNone(row["local_issue"])
        self.assertEqual(row["local_valid"], 0)

    def test_monitor_joins_on_t_plus_grace_and_clock_starts_after_worker_start(self):
        source = inspect.getsource(nexus_planner.IssuePlanner._start_shadow)
        self.assertIn("worker.join(join_timeout)", source)
        self.assertIn("join_timeout = timeout + SHADOW_MONITOR_GRACE_SECONDS", source)
        self.assertIn("self._shadow(prompt, timeout, allowed)", source)
        # The clock starts after worker.start(), not before it.
        self.assertLess(source.index("worker.start()"),
                        source.index("started = time.monotonic()"))


class NexusTickShadowWiringTests(unittest.TestCase):
    """nexus_tick.py shadow() builds the client with the T it is given."""

    def test_shadow_builds_the_client_with_the_passed_timeout(self):
        import nexus_tick
        from modules import llama_cpp_client

        captured = {}

        class FakeClient:
            model = "fake-model"

            def __init__(self, base_url=None, timeout=300.0, model=None, *, stream=None,
                         transport=None):
                captured["timeout"] = timeout
                captured["stream"] = stream

            async def chat_completion(self, messages, max_tokens=1024, **kwargs):
                captured["messages"] = messages
                captured["max_tokens"] = max_tokens
                captured["response_format"] = kwargs.get("response_format")
                return {
                    "choices": [{"message": {"content": '{"issue": 1, "reason": "x"}'}}],
                    "timings": {"cache_n": 42},
                }

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(llama_cpp_client, "LlamaCppClient", FakeClient):
            planner = nexus_tick._planner(tmp, db_path=str(Path(tmp) / "agent.db"))
            shadow = planner._shadow
            self.assertIsNotNone(shadow)
            result = shadow("the compact prompt", 123.0, {1, 2})
        self.assertEqual(result, ('{"issue": 1, "reason": "x"}', 42))
        # LlamaCppClient(timeout=T, stream=False) with the same T the monitor used.
        self.assertEqual(captured["timeout"], 123.0)
        self.assertIs(captured["stream"], False)
        self.assertEqual(captured["messages"], [{"role": "user", "content": "the compact prompt"}])
        self.assertEqual(captured["max_tokens"], 120)
        # #798: the schema's issue enum is the allowed set that was passed in.
        schema = captured["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["issue"]["enum"], [1, 2])

    def test_planner_gets_rates_for_the_shadow_clients_model(self):
        import nexus_tick
        from modules import llama_cpp_client, local_model_rates

        seen = {}

        class FakeClient:
            model = "fake-model"

            def __init__(self, *args, **kwargs):
                pass

        real_rates = local_model_rates.LocalModelRates

        def spying_rates(path=None, *, model=None):
            seen["model"] = model
            return real_rates(path, model=model)

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(llama_cpp_client, "LlamaCppClient", FakeClient), \
                patch.object(local_model_rates, "LocalModelRates", spying_rates):
            planner = nexus_tick._planner(tmp, db_path=str(Path(tmp) / "agent.db"))
        self.assertEqual(seen["model"], "fake-model")
        self.assertIsNotNone(planner._rates)


if __name__ == "__main__":
    unittest.main()
