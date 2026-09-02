from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modules.tool_loop_guard import PersistentToolLoopGuard, call_signature


class ToolLoopGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_signature_normalizes_nested_argument_order(self):
        first = call_signature("operation", {"args": {"b": 2, "a": 1}})
        second = call_signature("operation", {"args": {"a": 1, "b": 2}})
        self.assertEqual(first, second)

    def test_duplicate_references_original_result_without_copying_it(self):
        guard = PersistentToolLoopGuard(self.db_path)
        try:
            first = guard.check("session", "memory_search", {"query": "existing"}, state_token="v1")
            guard.record_result(first.call_id, {"tool": "memory_search", "results": ["large result"]})
            duplicate = guard.check("session", "memory_search", {"query": "existing"}, state_token="v1")
        finally:
            guard.close()

        self.assertFalse(duplicate.allowed)
        self.assertEqual(duplicate.observation["event"], "duplicate_tool_call")
        self.assertEqual(duplicate.observation["previous_call_id"], first.call_id)
        self.assertIn("result_ref", duplicate.observation)
        self.assertNotIn("results", duplicate.observation)

    def test_changed_state_with_retry_reason_is_allowed(self):
        guard = PersistentToolLoopGuard(self.db_path)
        try:
            first = guard.check("session", "operation", {"args": {"id": 1}}, state_token="v1")
            guard.record_result(first.call_id, {"success": False})
            retry = guard.check(
                "session",
                "operation",
                {"args": {"id": 1}},
                state_token="v2",
                reason="repository state changed after recovery",
            )
        finally:
            guard.close()
        self.assertTrue(retry.allowed)

    def test_changed_state_retry_without_reason_is_rejected(self):
        guard = PersistentToolLoopGuard(self.db_path)
        try:
            first = guard.check("session", "operation", {}, state_token="v1")
            guard.record_result(first.call_id, {"success": False})
            retry = guard.check("session", "operation", {}, state_token="v2")
        finally:
            guard.close()
        self.assertFalse(retry.allowed)
        self.assertEqual(retry.observation["event"], "retry_reason_required")

    def test_cycle_and_calls_survive_restart(self):
        guard = PersistentToolLoopGuard(self.db_path)
        first = guard.check("session", "memory_search", {"query": "a"}, state_token="v1")
        guard.record_result(first.call_id, {"results": []})
        second = guard.check("session", "memory_search", {"query": "b"}, state_token="v1")
        guard.record_result(second.call_id, {"results": []})
        guard.close()

        restarted = PersistentToolLoopGuard(self.db_path)
        try:
            cycle = restarted.check("session", "memory_search", {"query": "a"}, state_token="v1")
        finally:
            restarted.close()
        self.assertFalse(cycle.allowed)
        self.assertEqual(cycle.observation["event"], "non_progressing_cycle")
        self.assertEqual(cycle.observation["previous_call_id"], first.call_id)

    def test_persistent_step_budget_is_configurable(self):
        guard = PersistentToolLoopGuard(self.db_path, max_total_steps=2)
        try:
            self.assertEqual(guard.claim_step("session", "v1"), (True, 1))
            self.assertEqual(guard.claim_step("session", "v1"), (True, 2))
            self.assertEqual(guard.claim_step("session", "v1"), (False, 3))
            self.assertEqual(guard.claim_step("session", "v2"), (True, 1))
        finally:
            guard.close()

    def test_repeated_duplicate_reaches_configured_stop_limit(self):
        guard = PersistentToolLoopGuard(self.db_path, max_identical_calls=3)
        try:
            first = guard.check("session", "memory_search", {"query": "same"}, state_token="v1")
            guard.record_result(first.call_id, {"results": []})
            first_duplicate = guard.check(
                "session", "memory_search", {"query": "same"}, state_token="v1"
            )
            second_duplicate = guard.check(
                "session", "memory_search", {"query": "same"}, state_token="v1"
            )
        finally:
            guard.close()
        self.assertFalse(first_duplicate.stop)
        self.assertTrue(second_duplicate.stop)


if __name__ == "__main__":
    unittest.main()
