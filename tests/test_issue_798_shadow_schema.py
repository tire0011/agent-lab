"""The shadow's json_schema pins the answer to the allowed issues (#798).

Part 2c of #782: ``nexus_tick.py`` ``shadow()`` now receives the allowed issue
numbers from ``_start_shadow`` (``shadow(prompt, timeout, allowed)``) and builds
its ``response_format`` from them: ``issue`` is an integer whose ``enum`` is
``sorted(allowed)`` and ``reason`` is a string with ``maxLength`` 160, both
required, no additional properties. The monitor keeps its own validation: a
non-object JSON (array) is ``invalid JSON``, a missing or non-integer issue is
``invalid issue``, and a number outside the allowed set is
``issue is not allowed``.
"""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import nexus_planner
from modules.nexus_planner import IssuePlanner
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


class ShadowMonitorTests(unittest.TestCase):
    """Fake callable + temp SQLite: _start_shadow passes allowed; the monitor
    still records each invalid answer with its own error string."""

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

    def test_start_shadow_passes_the_allowed_set_to_the_callable(self):
        received = []

        def fake_shadow(prompt, timeout, allowed):
            received.append((prompt, timeout, allowed))
            return '{"issue": 1, "reason": "local"}'

        planner = IssuePlanner(lambda _: PAID, shadow=fake_shadow,
                               db_path=self.db, rates=FakeRates((50.0, 5.0)))
        choice = planner.choose(ISSUES, tick_id="tick-allowed")
        self.assertEqual(choice.issue, 2)
        row = self.wait_for_row()
        self.assertEqual(len(received), 1)
        _prompt, _timeout, allowed = received[0]
        # The callable got exactly the allowed set choose() computed.
        self.assertEqual(allowed, {1, 2})
        self.assertIsNone(row["error"])
        self.assertEqual(row["local_issue"], 1)
        self.assertEqual(row["local_valid"], 1)

    def test_answer_outside_the_allowed_set_is_issue_is_not_allowed(self):
        planner = IssuePlanner(
            lambda _: PAID,
            shadow=lambda prompt, timeout, allowed: '{"issue": 99, "reason": "local"}',
            db_path=self.db, rates=FakeRates((50.0, 5.0)),
        )
        planner.choose(ISSUES, tick_id="tick-outside")
        row = self.wait_for_row()
        self.assertEqual(row["error"], "issue is not allowed")
        self.assertEqual(row["local_issue"], 99)
        self.assertEqual(row["local_valid"], 0)

    def test_array_answer_is_invalid_json(self):
        planner = IssuePlanner(
            lambda _: PAID,
            shadow=lambda prompt, timeout, allowed: '[1, 2]',
            db_path=self.db, rates=FakeRates((50.0, 5.0)),
        )
        planner.choose(ISSUES, tick_id="tick-array")
        row = self.wait_for_row()
        self.assertEqual(row["error"], "invalid JSON")
        self.assertIsNone(row["local_issue"])
        self.assertEqual(row["local_valid"], 0)

    def test_missing_or_non_integer_issue_is_invalid_issue(self):
        for answer in ('{"reason": "no issue"}', '{"issue": "abc", "reason": "x"}',
                       '{"issue": null, "reason": "x"}'):
            with self.subTest(answer=answer):
                db = str(Path(self.tmp.name) / f"{abs(hash(answer))}.db")
                planner = IssuePlanner(
                    lambda _: PAID,
                    shadow=lambda prompt, timeout, allowed, a=answer: a,
                    db_path=db, rates=FakeRates((50.0, 5.0)),
                )
                planner.choose(ISSUES, tick_id=f"tick-{abs(hash(answer))}")
                deadline = time.monotonic() + 2
                row = None
                while time.monotonic() < deadline:
                    if list_shadow(db)["count"]:
                        row = list_shadow(db)["rows"][0]
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(row, "shadow row was not written")
                self.assertEqual(row["error"], "invalid issue")
                self.assertIsNone(row["local_issue"])
                self.assertEqual(row["local_valid"], 0)

    def test_zero_env_makes_no_call(self):
        called = []
        with patch.dict(os.environ, {"NEXUS_PLANNER_SHADOW": "0"}):
            planner = IssuePlanner(
                lambda _: PAID,
                shadow=lambda prompt, timeout, allowed: called.append(prompt) or "{}",
                db_path=self.db, rates=FakeRates((50.0, 5.0)),
            )
            choice = planner.choose(ISSUES, tick_id="tick-off")
        self.assertEqual(choice.issue, 2)
        time.sleep(0.05)
        self.assertEqual(called, [])
        with sqlite3.connect(self.db) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='planner_shadow'"
            ).fetchone()
        self.assertIsNone(table)


class NexusTickShadowSchemaTests(unittest.TestCase):
    """nexus_tick.py shadow() builds the response_format from the allowed set."""

    def test_schema_enum_equals_sorted_allowed_and_reason_max_length_160(self):
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
                return {"choices": [{"message": {"content": '{"issue": 3, "reason": "x"}'}}]}

        allowed = {7, 3, 5}  # deliberately unsorted
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(llama_cpp_client, "LlamaCppClient", FakeClient):
            planner = nexus_tick._planner(tmp, db_path=str(Path(tmp) / "agent.db"))
            shadow = planner._shadow
            self.assertIsNotNone(shadow)
            result = shadow("the compact prompt", 123.0, allowed)
        self.assertEqual(result, ('{"issue": 3, "reason": "x"}', None))
        self.assertEqual(captured["timeout"], 123.0)
        self.assertIs(captured["stream"], False)
        self.assertEqual(captured["messages"], [{"role": "user", "content": "the compact prompt"}])
        self.assertEqual(captured["max_tokens"], 120)

        response_format = captured["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "planner_shadow")
        schema = response_format["json_schema"]["schema"]
        self.assertEqual(schema["type"], "object")
        # The enum in the built schema is exactly the allowed set, sorted.
        self.assertEqual(schema["properties"]["issue"],
                         {"type": "integer", "enum": sorted(allowed)})
        self.assertEqual(schema["properties"]["issue"]["enum"], [3, 5, 7])
        self.assertEqual(schema["properties"]["reason"],
                         {"type": "string", "maxLength": 160})
        self.assertEqual(schema["required"], ["issue", "reason"])
        self.assertIs(schema["additionalProperties"], False)

    def test_shadow_source_builds_the_enum_from_the_allowed_argument(self):
        import inspect

        import nexus_tick

        source = inspect.getsource(nexus_tick._planner)
        self.assertIn("def shadow(prompt: str, timeout: float, allowed: set[int])", source)
        self.assertIn('"enum": sorted(allowed)', source)
        self.assertIn('"maxLength": 160', source)


if __name__ == "__main__":
    unittest.main()
