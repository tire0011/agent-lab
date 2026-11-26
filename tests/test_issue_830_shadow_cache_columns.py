"""#830 (retry of #799): planner_shadow ``prompt_variant`` / ``cached_prompt_tokens``.

The columns themselves, the ``BEGIN IMMEDIATE``-serialized migration, the
``compact_v1`` recording in ``_start_shadow`` and the ``list_shadow`` exposure
live in ``modules/planner_shadow.py`` and are covered by
``tests/test_issue_671_planner_shadow.py``. This file covers the review fix
fixes from experiments 30cc3d80 and e7569f74:

* A raw llama-server response whose ``timings.cache_n`` is a valid JSON
  integer with more digits than ``sys.get_int_max_str_digits()`` (4300 by
  default) made ``resp.json()`` raise ValueError, failing the whole shadow
  observation before ``record()`` could store NULL. Completion decoding now
  tolerates exactly that one literal (it becomes ``None``) without touching the
  process-wide conversion limit.
* The tolerance is limited to ``timings.cache_n`` of completion responses:
  model-generated JSON (``extract_json``, the router), other oversized
  literals in a completion and unrelated endpoints (``/tokenize``) stay strict.
* The fallback re-parses the original response bytes: invalid bytes still
  raise ``UnicodeDecodeError`` and a conflicting ``charset`` header cannot
  alter the content.

Also covered here: interruption / crash / halfway / rerun safety of the
migration and ``record()``, including rollback after an ``ALTER TABLE``.
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json
from modules.llama_router import _parse_router_response
from modules.nexus_planner import IssuePlanner
from modules.planner_shadow import list_shadow, record

PAID = '{"issue": 2, "reason": "paid choice"}'
ISSUES = [
    {"number": 1, "title": "one", "body": ""},
    {"number": 2, "title": "two", "body": ""},
]
LOCAL_ANSWER = '{"issue": 1, "reason": "local"}'
# Beyond sys.get_int_max_str_digits() (4300 by default): on the wire this is a
# valid JSON integer, but the stdlib decoder refuses to convert it to int.
HUGE_DIGITS = "9" * 5000


REPO_ROOT = Path(__file__).resolve().parents[1]


def _raw_completion_body(cache_n_literal: str, *, content: str = LOCAL_ANSWER,
                         prompt_tokens_literal: str = "120") -> bytes:
    """A raw /chat/completions body with ``timings.cache_n`` spliced in as a raw
    integer literal (json.dumps/int() could not produce it, the wire can)."""
    payload = {
        "id": "cmpl-1",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": "@PROMPT_TOKENS@", "completion_tokens": 4},
        "timings": {"prompt_n": 120, "prompt_ms": 400.0,
                    "predicted_n": 4, "predicted_ms": 100.0},
    }
    return json.dumps(payload, ensure_ascii=False).replace(
        '"prompt_ms": 400.0', '"prompt_ms": 400.0, "cache_n": ' + cache_n_literal
    ).replace('"@PROMPT_TOKENS@"', prompt_tokens_literal).encode()


def _json_transport(body: bytes, content_type: str = "application/json") -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(
        200, content=body, headers={"Content-Type": content_type}))


def _complete(body: bytes, content_type: str = "application/json") -> tuple[dict, LlamaCppClient]:
    client = LlamaCppClient(base_url="http://llama.test/v1", model="m", stream=False,
                            transport=_json_transport(body, content_type))
    return asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}])), client


def _sse(raw: str) -> bytes:
    return ("data: " + raw + "\n\n").encode()


class OversizedCacheResponseTests(unittest.TestCase):
    """Raw HTTP responses with an oversized cache_n decode without failing (#830)."""

    def test_raw_response_with_oversized_cache_n_keeps_the_choice(self):
        limit_before = sys.get_int_max_str_digits()
        body = _raw_completion_body(HUGE_DIGITS)
        client = LlamaCppClient(base_url="http://llama.test/v1", model="m", stream=False,
                                transport=_json_transport(body))
        data = asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
        # The valid choice survives; the oversized counter is unknown (None).
        self.assertEqual(data["choices"][0]["message"]["content"], LOCAL_ANSWER)
        self.assertIsNone(data["timings"]["cache_n"])
        self.assertEqual(client.last_timings["prompt_n"], 120)
        self.assertNotIn("tokens_cached", client.last_timings)
        # The process-wide conversion limit is never raised.
        self.assertEqual(sys.get_int_max_str_digits(), limit_before)

    def test_cache_n_at_the_conversion_limit_still_decodes_as_int(self):
        at_limit = "9" * sys.get_int_max_str_digits()
        body = _raw_completion_body(at_limit)
        client = LlamaCppClient(base_url="http://llama.test/v1", model="m", stream=False,
                                transport=_json_transport(body))
        data = asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
        self.assertIsInstance(data["timings"]["cache_n"], int)
        self.assertEqual(data["choices"][0]["message"]["content"], LOCAL_ANSWER)

    def test_malformed_body_still_raises(self):
        client = LlamaCppClient(base_url="http://llama.test/v1", model="m", stream=False,
                                transport=_json_transport(b"{not json"))
        with self.assertRaises(ValueError):
            asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))

    def test_streamed_response_with_oversized_cache_n_keeps_the_choice(self):
        limit_before = sys.get_int_max_str_digits()
        chunks = [
            _sse(json.dumps({"id": "x", "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]})),
            _sse(json.dumps({"id": "x", "choices": [
                {"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]})),
            _sse('{"id": "x", "choices": [], "timings": {"prompt_n": 30, "cache_n": '
                 + HUGE_DIGITS + "}}"),
            b"data: [DONE]\n\n",
        ]

        async def events():
            for chunk in chunks:
                yield chunk

        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=events()))
        client = LlamaCppClient(base_url="http://llama.test/v1", model="m", stream=True,
                                timeout=5, transport=transport)
        data = asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(client.last_timings["prompt_n"], 30)
        self.assertNotIn("tokens_cached", client.last_timings)
        self.assertEqual(sys.get_int_max_str_digits(), limit_before)


    def test_negative_oversized_cache_n_is_none(self):
        data, _ = _complete(_raw_completion_body("-" + HUGE_DIGITS))
        self.assertIsNone(data["timings"]["cache_n"])
        self.assertEqual(data["choices"][0]["message"]["content"], LOCAL_ANSWER)


class StrictDecodingTests(unittest.TestCase):
    """Review fix e7569f74 (1): only ``timings.cache_n`` of a completion is tolerated."""

    def test_oversized_literal_elsewhere_in_a_completion_still_raises(self):
        body = _raw_completion_body("7", prompt_tokens_literal=HUGE_DIGITS)
        with self.assertRaises(ValueError) as caught:
            _complete(body)
        self.assertNotIsInstance(caught.exception, json.JSONDecodeError)

    def test_oversized_cache_n_plus_another_oversized_literal_raises(self):
        body = _raw_completion_body(HUGE_DIGITS, prompt_tokens_literal=HUGE_DIGITS)
        with self.assertRaises(ValueError):
            _complete(body)

    def test_oversized_cache_n_outside_timings_raises(self):
        body = b'{"choices": [], "cache_n": ' + HUGE_DIGITS.encode() + b'}'
        with self.assertRaises(ValueError):
            _complete(body)

    def test_streamed_oversized_literal_outside_cache_n_still_raises(self):
        chunks = [
            _sse('{"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}}], '
                 '"usage": {"prompt_tokens": ' + HUGE_DIGITS + "}}"),
            b"data: [DONE]\n\n",
        ]

        async def events():
            for chunk in chunks:
                yield chunk

        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=events()))
        client = LlamaCppClient(base_url="http://llama.test/v1", model="m", stream=True,
                                timeout=5, transport=transport)
        with self.assertRaises(ValueError):
            asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))

    def test_extract_json_stays_strict_for_model_output(self):
        with self.assertRaises(ValueError) as caught:
            extract_json('{"issue": ' + HUGE_DIGITS + ', "reason": "x"}')
        self.assertNotIsInstance(caught.exception, LlamaCppClientError)
        # Malformed model JSON keeps its own error.
        with self.assertRaises(LlamaCppClientError):
            extract_json('{"issue": 1,}')

    def test_router_does_not_turn_an_oversized_confidence_into_infinity(self):
        with self.assertRaises(ValueError):
            _parse_router_response('{"provider": "local", "confidence": ' + HUGE_DIGITS + "}")

    def test_tokenize_keeps_its_int_contract(self):
        body = b'{"tokens": [1, ' + HUGE_DIGITS.encode() + b']}'
        client = LlamaCppClient(base_url="http://llama.test/v1", model="m",
                                transport=_json_transport(body))
        with self.assertRaises(ValueError):
            asyncio.run(client.tokenize("hi"))
        client = LlamaCppClient(base_url="http://llama.test/v1", model="m",
                                transport=_json_transport(b'{"tokens": [1, 2]}'))
        self.assertEqual(asyncio.run(client.tokenize("hi")), [1, 2])


class ByteDecodingTests(unittest.TestCase):
    """Review fix e7569f74 (2): the fallback parses the original bytes."""

    def test_invalid_utf8_with_oversized_cache_n_raises_unicode_error(self):
        body = _raw_completion_body(HUGE_DIGITS).replace(b'cmpl-1', b'cmpl-\xff1')
        with self.assertRaises(UnicodeDecodeError):
            _complete(body)

    def test_invalid_utf8_without_oversized_cache_n_raises_unicode_error(self):
        body = _raw_completion_body("7").replace(b'cmpl-1', b'cmpl-\xff1')
        with self.assertRaises(UnicodeDecodeError):
            _complete(body)

    def test_conflicting_charset_cannot_alter_the_content(self):
        answer = '{"issue": 1, "reason": "Größe ✓"}'
        body = _raw_completion_body(HUGE_DIGITS, content=answer)
        data, _ = _complete(body, "application/json; charset=iso-8859-1")
        self.assertEqual(data["choices"][0]["message"]["content"], answer)
        self.assertIsNone(data["timings"]["cache_n"])

    def test_utf16_body_with_oversized_cache_n_decodes_like_strict_json(self):
        body = _raw_completion_body(HUGE_DIGITS).decode().encode("utf-16")
        data, _ = _complete(body)
        self.assertEqual(data["choices"][0]["message"]["content"], LOCAL_ANSWER)
        self.assertIsNone(data["timings"]["cache_n"])

    def test_repeated_decoding_is_stable(self):
        body = _raw_completion_body(HUGE_DIGITS)
        first, _ = _complete(body)
        second, _ = _complete(body)
        self.assertEqual(first, second)


class ShadowOversizedCacheTests(unittest.TestCase):
    """End to end: the shadow callable of nexus_tick.py over a fake llama-server
    whose timings.cache_n is oversized; record() stores NULL and keeps the result."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def wait_for_row(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if list_shadow(self.db)["count"]:
                return list_shadow(self.db)["rows"][0]
            time.sleep(0.01)
        self.fail("shadow row was not written")

    def shadow_like_nexus_tick(self, body: bytes):
        def shadow(prompt, timeout, allowed):
            client = LlamaCppClient(base_url="http://llama.test/v1", model="m",
                                    stream=False, timeout=timeout,
                                    transport=_json_transport(body))
            response = asyncio.run(client.chat_completion(
                [{"role": "user", "content": prompt}], max_tokens=120))
            timings = response.get("timings")
            cached = timings.get("cache_n") if isinstance(timings, dict) else None
            return str(response["choices"][0]["message"]["content"]), cached
        return shadow

    def test_oversized_cache_n_is_null_and_the_result_stays_valid(self):
        planner = IssuePlanner(lambda _: PAID,
                               shadow=self.shadow_like_nexus_tick(_raw_completion_body(HUGE_DIGITS)),
                               db_path=self.db)
        choice = planner.choose(ISSUES, tick_id="tick-huge")
        self.assertEqual(choice.issue, 2)
        row = self.wait_for_row()
        self.assertEqual(row["local_issue"], 1)
        self.assertEqual(row["local_valid"], 1)
        self.assertIsNone(row["error"])
        self.assertEqual(row["prompt_variant"], "compact_v1")
        self.assertIsNone(row["cached_prompt_tokens"])

    def test_normal_cache_n_is_stored(self):
        planner = IssuePlanner(lambda _: PAID,
                               shadow=self.shadow_like_nexus_tick(_raw_completion_body("70")),
                               db_path=self.db)
        planner.choose(ISSUES, tick_id="tick-70")
        row = self.wait_for_row()
        self.assertEqual(row["cached_prompt_tokens"], 70)
        self.assertEqual(row["local_valid"], 1)
        self.assertIsNone(row["error"])


class MigrationRobustnessTests(unittest.TestCase):
    """Interrupted / crashed / halfway / rerun safety of the migration and record()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def create_legacy_table(self, *, with_created_at: bool = False):
        with sqlite3.connect(self.db) as conn:
            conn.execute("""
                CREATE TABLE planner_shadow (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, tick_id TEXT NOT NULL,
                    paid_issue INTEGER NOT NULL, local_issue INTEGER, local_valid INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL, seconds REAL NOT NULL, error TEXT
                )
            """)
            if with_created_at:
                # A pre-#830 process crashed between its autocommitted ALTERs:
                # created_at landed, the two new columns did not.
                conn.execute("ALTER TABLE planner_shadow ADD COLUMN created_at TEXT")
            conn.execute(
                "INSERT INTO planner_shadow VALUES (1, 'legacy', 2, 1, 1, 12, 0.5, NULL"
                + (", '2026-01-01T00:00:00+00:00'" if with_created_at else "") + ")")

    def test_migration_crashed_halfway_is_completed_and_keeps_rows(self):
        self.create_legacy_table(with_created_at=True)
        record(self.db, tick_id="new", paid_issue=2, local_issue=1, local_valid=1,
               prompt_tokens=12, seconds=0.5, error=None,
               prompt_variant="compact_v1", cached_prompt_tokens=8)
        rows = list_shadow(self.db)["rows"]
        self.assertEqual([row["tick_id"] for row in rows], ["new", "legacy"])
        legacy = rows[1]
        self.assertEqual((legacy["paid_issue"], legacy["local_issue"], legacy["local_valid"],
                          legacy["prompt_tokens"], legacy["seconds"], legacy["error"]),
                         (2, 1, 1, 12, 0.5, None))
        self.assertIsNone(legacy["prompt_variant"])
        self.assertIsNone(legacy["cached_prompt_tokens"])
        new = rows[0]
        self.assertEqual(new["prompt_variant"], "compact_v1")
        self.assertEqual(new["cached_prompt_tokens"], 8)
        with sqlite3.connect(self.db) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
        self.assertIn("prompt_variant", columns)
        self.assertIn("cached_prompt_tokens", columns)
        # Running the migration again (list after record) changes nothing.
        self.assertEqual(list_shadow(self.db)["rows"], rows)

    def test_sequential_list_record_list_on_legacy_table(self):
        self.create_legacy_table()
        self.assertEqual(len(list_shadow(self.db)["rows"]), 1)  # list migrates first
        record(self.db, tick_id="new", paid_issue=2, local_issue=1, local_valid=1,
               prompt_tokens=12, seconds=0.5, error=None, prompt_variant="compact_v1")
        rows = list_shadow(self.db)["rows"]
        self.assertEqual([row["tick_id"] for row in rows], ["new", "legacy"])
        self.assertEqual(rows[0]["prompt_variant"], "compact_v1")
        self.assertIsNone(rows[0]["cached_prompt_tokens"])

    def test_failed_record_leaves_no_partial_row_and_a_retry_stores_one(self):
        record(self.db, tick_id="ok", paid_issue=2, local_issue=1, local_valid=1,
               prompt_tokens=10, seconds=0.1, error=None)
        # local_valid=5 violates the CHECK constraint: the insert fails halfway.
        with self.assertRaises(sqlite3.IntegrityError):
            record(self.db, tick_id="bad", paid_issue=2, local_issue=1, local_valid=5,
                   prompt_tokens=10, seconds=0.1, error=None, prompt_variant="compact_v1")
        rows = list_shadow(self.db)["rows"]
        self.assertEqual([row["tick_id"] for row in rows], ["ok"])
        # A rerun of the same tick with valid values stores exactly one row.
        record(self.db, tick_id="bad", paid_issue=2, local_issue=1, local_valid=1,
               prompt_tokens=10, seconds=0.2, error=None,
               prompt_variant="compact_v1", cached_prompt_tokens=3)
        rows = list_shadow(self.db)["rows"]
        self.assertEqual([row["tick_id"] for row in rows], ["bad", "ok"])
        self.assertEqual(rows[0]["cached_prompt_tokens"], 3)
        self.assertIsNone(rows[0]["error"])

    def test_repeated_record_for_one_tick_keeps_a_single_first_row(self):
        for seconds in (0.1, 0.2, 0.3):
            record(self.db, tick_id="tick", paid_issue=2, local_issue=1, local_valid=1,
                   prompt_tokens=10, seconds=seconds, error=None, prompt_variant="compact_v1",
                   cached_prompt_tokens=seconds and 5)
        rows = list_shadow(self.db)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seconds"], 0.1)
        self.assertEqual(rows[0]["cached_prompt_tokens"], 5)
        # Repeated reads are stable.
        self.assertEqual(list_shadow(self.db), list_shadow(self.db))

    def test_failure_after_alter_table_rolls_the_migration_back(self):
        self.create_legacy_table()
        # "error" is missing: the INSERT fails after ensure_table() has run
        # its ALTER TABLE statements inside the same BEGIN IMMEDIATE.
        with self.assertRaises(sqlite3.ProgrammingError):
            record(self.db, tick_id="bad", paid_issue=2, local_issue=1, local_valid=1,
                   prompt_tokens=10, seconds=0.1, prompt_variant="compact_v1")
        with sqlite3.connect(self.db) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
            rows = conn.execute("SELECT tick_id FROM planner_shadow").fetchall()
        self.assertNotIn("created_at", columns)
        self.assertNotIn("prompt_variant", columns)
        self.assertNotIn("cached_prompt_tokens", columns)
        self.assertEqual(rows, [("legacy",)])
        # The rerun migrates and stores the row.
        record(self.db, tick_id="bad", paid_issue=2, local_issue=1, local_valid=1,
               prompt_tokens=10, seconds=0.1, error=None, prompt_variant="compact_v1")
        rows = list_shadow(self.db)["rows"]
        self.assertEqual([row["tick_id"] for row in rows], ["bad", "legacy"])
        self.assertEqual(rows[0]["prompt_variant"], "compact_v1")

    def test_process_killed_mid_migration_leaves_the_old_table_intact(self):
        self.create_legacy_table()
        script = (
            "import os, sqlite3, sys\n"
            "from modules.planner_shadow import ensure_table\n"
            "conn = sqlite3.connect(sys.argv[1], timeout=30)\n"
            "conn.execute('BEGIN IMMEDIATE')\n"
            "ensure_table(conn)\n"
            "os._exit(3)\n"  # dies holding the uncommitted ALTERs
        )
        proc = subprocess.run([sys.executable, "-c", script, self.db], cwd=REPO_ROOT,
                              env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
                              capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        with sqlite3.connect(self.db) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(planner_shadow)")}
        self.assertNotIn("prompt_variant", columns)
        # The next caller migrates normally; the legacy row survives.
        record(self.db, tick_id="after-crash", paid_issue=2, local_issue=1, local_valid=1,
               prompt_tokens=10, seconds=0.1, error=None,
               prompt_variant="compact_v1", cached_prompt_tokens=4)
        rows = list_shadow(self.db)["rows"]
        self.assertEqual([row["tick_id"] for row in rows], ["after-crash", "legacy"])
        self.assertEqual(rows[0]["cached_prompt_tokens"], 4)
        self.assertIsNone(rows[1]["cached_prompt_tokens"])

    def test_malformed_cache_values_are_null_and_the_row_stays_valid(self):
        for index, value in enumerate([float("nan"), float("inf"), -1, 2**63, True,
                                       "12", 1.5, None]):
            record(self.db, tick_id=f"t{index}", paid_issue=2, local_issue=1, local_valid=1,
                   prompt_tokens=10, seconds=0.1, error=None,
                   prompt_variant="compact_v1", cached_prompt_tokens=value)
        rows = list_shadow(self.db)["rows"]
        self.assertEqual(len(rows), 8)
        for row in rows:
            self.assertIsNone(row["cached_prompt_tokens"], row["tick_id"])
            self.assertEqual(row["local_valid"], 1)
            self.assertIsNone(row["error"])
        record(self.db, tick_id="max", paid_issue=2, local_issue=1, local_valid=1,
               prompt_tokens=10, seconds=0.1, error=None, cached_prompt_tokens=2**63 - 1)
        self.assertEqual(list_shadow(self.db)["rows"][0]["cached_prompt_tokens"], 2**63 - 1)

    def test_oversized_string_cache_value_is_stored_as_null(self):
        # This is what response decoding produces for an oversized cache_n.
        record(self.db, tick_id="tick-str", paid_issue=2, local_issue=1, local_valid=1,
               prompt_tokens=10, seconds=0.1, error=None,
               cached_prompt_tokens=HUGE_DIGITS)
        row = list_shadow(self.db)["rows"][0]
        self.assertIsNone(row["cached_prompt_tokens"])
        self.assertEqual(row["local_valid"], 1)
        self.assertIsNone(row["error"])


if __name__ == "__main__":
    unittest.main()
