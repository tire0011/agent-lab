"""#562: read_tool_result pages only through this session's stored tool results."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from modules.nexus_task_store import NexusTaskStore
from modules.prompt_budget import _stub
from modules.task_orchestrator import (
    _ORCHESTRATOR_SCHEMA,
    ACTION_ANSWER,
    NexusTaskOrchestrator,
)
from modules.tool_loop_guard import PersistentToolLoopGuard
from modules.tool_result_reader import (
    MIN_LIMIT_CHARS,
    REFUSAL_INVALID_ARGUMENTS,
    REFUSAL_NOT_READABLE,
    TOOL_READ_TOOL_RESULT,
    duplicate_read_guidance,
    limit_cap,
    normalize_arguments,
    read_tool_result,
)

LONG_RESULT = json.dumps(
    {"tool": "command", "command": "pytest", "result": "".join(f"line {i}\n" for i in range(500))},
    separators=(",", ":"),
)
SECRET = "metadata-must-never-leak"


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = NexusTaskStore(str(Path(self._tmp.name) / "state.db"))
        self.session = self.store.get_or_create(source_type="chat", source_id="s-1", title="one").id
        self.other = self.store.get_or_create(source_type="chat", source_id="s-2", title="two").id
        self.tool_event = self.store.append_event(
            self.session, role="tool", kind="command", content=LONG_RESULT,
            metadata={"reason": SECRET},
        )

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def read(self, session_id=None, **decision):
        return read_tool_result(
            self.store, session_id or self.session, decision, cap=1000
        )


class SliceTests(_StoreCase):
    def test_a_slice_is_returned_by_event_id_and_offset(self):
        result = self.read(event_id=self.tool_event, offset=100, limit=50)
        self.assertEqual(result["tool"], TOOL_READ_TOOL_RESULT)
        self.assertEqual(result["content"], LONG_RESULT[100:150])
        self.assertEqual(result["total_chars"], len(LONG_RESULT))
        self.assertEqual(result["next_offset"], 150)
        self.assertFalse(result["complete"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_the_same_arguments_always_give_the_same_slice(self):
        first = self.read(event_id=self.tool_event, offset=7, limit=300)
        second = self.read(event_id=self.tool_event, offset=7, limit=300)
        self.assertEqual(first, second)

    def test_consecutive_ranges_reassemble_the_full_result(self):
        parts, offset = [], 0
        while offset is not None:
            result = self.read(event_id=self.tool_event, offset=offset, limit=1000)
            parts.append(result["content"])
            offset = result["next_offset"]
        self.assertEqual("".join(parts), LONG_RESULT)
        self.assertTrue(result["complete"])

    def test_an_offset_past_the_end_returns_an_empty_slice_and_the_total(self):
        result = self.read(event_id=self.tool_event, offset=len(LONG_RESULT) + 50, limit=10)
        self.assertEqual(result["content"], "")
        self.assertEqual(result["total_chars"], len(LONG_RESULT))
        self.assertTrue(result["complete"])
        self.assertIsNone(result["next_offset"])

    def test_a_limit_above_the_cap_is_reduced_to_the_cap(self):
        result = self.read(event_id=self.tool_event, offset=0, limit=50_000)
        self.assertEqual(result["limit"], 1000)
        self.assertEqual(len(result["content"]), 1000)
        self.assertEqual(result["content"], LONG_RESULT[:1000])

    def test_a_missing_limit_defaults_to_the_cap(self):
        result = self.read(event_id=self.tool_event)
        self.assertEqual(result["offset"], 0)
        self.assertEqual(result["limit"], 1000)

    def test_the_cap_follows_the_observation_budget(self):
        self.assertEqual(limit_cap(6000), 5600)
        self.assertEqual(limit_cap(10), MIN_LIMIT_CHARS)
        arguments, error = normalize_arguments({"event_id": 3, "limit": 9999}, limit_cap(6000))
        self.assertIsNone(error)
        self.assertEqual(arguments, {"event_id": 3, "offset": 0, "limit": 5600})

    def test_invalid_arguments_are_refused_without_content(self):
        for decision in (
            {"event_id": "abc"},
            {"event_id": self.tool_event, "offset": -1},
            {"event_id": self.tool_event, "limit": 0},
            {},
        ):
            with self.subTest(decision=decision):
                result = self.read(**decision)
                self.assertEqual(result["error"], REFUSAL_INVALID_ARGUMENTS)
                self.assertNotIn("content", result)


class AccessRuleTests(_StoreCase):
    def assertRefused(self, result):
        self.assertEqual(result["error"], REFUSAL_NOT_READABLE)
        self.assertNotIn("content", result)
        self.assertNotIn("total_chars", result)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_user_assistant_and_system_events_are_refused(self):
        for role in ("user", "assistant", "system"):
            with self.subTest(role=role):
                # Even with a tool-result kind: role alone decides.
                event_id = self.store.append_event(
                    self.session, role=role, kind="command", content=SECRET,
                )
                self.assertRefused(self.read(event_id=event_id))

    def test_another_sessions_tool_result_is_refused(self):
        event_id = self.store.append_event(
            self.other, role="tool", kind="command", content=SECRET,
        )
        self.assertRefused(self.read(event_id=event_id))
        # ...and the owning session can read it, so the refusal is the session rule.
        self.assertEqual(
            self.read(session_id=self.other, event_id=event_id)["content"], SECRET
        )

    def test_an_unknown_id_is_refused(self):
        self.assertRefused(self.read(event_id=self.tool_event + 999))

    def test_a_tool_event_that_is_not_a_tool_call_result_is_refused(self):
        for kind in ("goal_truncated", "command_result", TOOL_READ_TOOL_RESULT):
            with self.subTest(kind=kind):
                event_id = self.store.append_event(
                    self.session, role="tool", kind=kind, content=SECRET,
                )
                self.assertRefused(self.read(event_id=event_id))

    def test_the_refusal_does_not_reveal_whether_the_id_exists(self):
        foreign = self.store.append_event(self.other, role="tool", kind="command", content="x")
        unknown = self.read(event_id=foreign + 999)
        cross = self.read(event_id=foreign)
        self.assertEqual(
            {k: v for k, v in unknown.items() if k != "event_id"},
            {k: v for k, v in cross.items() if k != "event_id"},
        )


class LoopGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.guard = PersistentToolLoopGuard(str(Path(self._tmp.name) / "state.db"))

    def tearDown(self):
        self.guard.close()
        self._tmp.cleanup()

    def check(self, offset, limit=100):
        return self.guard.check(
            "s", TOOL_READ_TOOL_RESULT,
            {"event_id": 5, "offset": offset, "limit": limit},
            state_token="t", reason="page",
        )

    def test_two_different_ranges_of_one_event_both_pass(self):
        first = self.check(0)
        self.guard.record_result(first.call_id, {"content": "a"})
        second = self.check(100)
        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)

    def test_the_same_range_twice_is_refused_with_the_next_offset(self):
        first = self.check(200)
        self.guard.record_result(first.call_id, {"content": "a"})
        repeat = self.check(200)
        self.assertFalse(repeat.allowed)
        guidance = duplicate_read_guidance({"event_id": 5, "offset": 200, "limit": 100})
        self.assertEqual(guidance["next_offset"], 300)
        self.assertIn("offset=300", guidance["recovery"])
        self.assertNotIn("different_tool", guidance["recovery"])


class _Registry:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)

    def catalog(self):
        return []


class _Delegation:
    def catalog(self):
        return []


class _Client:
    """Fake controller; a callable response sees the database to name the event id."""

    def __init__(self, db_path, responses):
        self.db_path = db_path
        self.responses = list(responses)
        self.prompts = []

    def command_event_id(self):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT id FROM nexus_task_events WHERE role='tool' AND kind='command'"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        del temperature, max_tokens, response_format
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        return response(self) if callable(response) else response


def _read(offset, limit):
    return lambda client: json.dumps({
        "action": TOOL_READ_TOOL_RESULT,
        "event_id": client.command_event_id(),
        "offset": offset,
        "limit": limit,
        "reason": "page through the command output",
    })


class OrchestratorTests(unittest.TestCase):
    def test_controller_pages_and_a_repeat_is_sent_to_the_next_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "state.db")
            client = _Client(db_path, [
                '{"action":"command","command":"printf abcdefghijklmnop",'
                '"reason":"produce output"}',
                _read(0, 5),
                _read(0, 5),
                _read(5, 5),
                '{"action":"answer","experiment_id":null,"message":"read it",'
                '"reason":"paged through the result"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=_Registry(db_path, tmp),
                experiment_service=MagicMock(),
                llama_client=client,
                delegation_service=_Delegation(),
            )
            try:
                result = orchestrator.run("Run the command and read its output in pages")
            finally:
                orchestrator.close()

            self.assertEqual(result.action, ACTION_ANSWER)
            reads = [o for o in result.observations if o.get("tool") == TOOL_READ_TOOL_RESULT]
            self.assertEqual(len(reads), 3)
            first, repeat, second = reads
            stored = json.loads(json.dumps(result.observations[0]))
            stored.pop("event_id")
            full = json.dumps(stored, separators=(",", ":"))
            self.assertEqual(first["content"], full[0:5])
            self.assertEqual(repeat["event"], "duplicate_tool_call")
            self.assertNotIn("content", repeat)
            self.assertEqual(repeat["next_offset"], 5)
            self.assertIn("offset=5", repeat["recovery"])
            self.assertEqual(second["content"], full[5:10])

    def test_the_tool_is_in_the_controller_protocol(self):
        actions = _ORCHESTRATOR_SCHEMA["properties"]["action"]["enum"]
        self.assertIn(TOOL_READ_TOOL_RESULT, actions)
        self.assertIn("event_id", _ORCHESTRATOR_SCHEMA["properties"])
        self.assertIn("offset", _ORCHESTRATOR_SCHEMA["properties"])


class NoteTests(unittest.TestCase):
    def test_a_stub_of_a_readable_result_names_the_reader(self):
        stub = _stub({"tool": "command", "command": "pytest", "event_id": 42})
        self.assertIn("read_tool_result(event_id=42", stub["note"])

    def test_a_stub_of_an_unreadable_result_does_not_invite_a_read(self):
        stub = _stub({"tool": TOOL_READ_TOOL_RESULT, "event_id": 43})
        self.assertNotIn("read_tool_result(", stub["note"])


if __name__ == "__main__":
    unittest.main()
