from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.controller_observations import controller_observation_view
from modules.nexus_task_store import NexusTaskStore
from modules.operations_registry import OperationsRegistry
from modules.prompt_budget import compact_observations
from modules.tool_loop_guard import PersistentToolLoopGuard


class StagedToolResultTests(unittest.TestCase):
    def _issue(self) -> dict:
        body = (
            "Provide a staged controller view so Nexus can decide what to do next.\n\n"
            "## Acceptance criteria\n- preserve the title and state\n- page stored results\n\n"
            "## Detail\n" + "large diagnostic detail " * 500
        )
        return {
            "tool": "operation", "operation": "github_issue",
            "args": {"issue_number": 232}, "status": "succeeded", "event_id": 41,
            "output_lines": [
                'issue: {"number":232,"state":"open","title":"Staged controller view"}',
                *[
                    f"body[{index // 450 + 1}]: {body[index:index + 450]}"
                    for index in range(0, len(body), 450)
                ],
            ],
        }

    def test_large_result_uses_semantic_rung_and_keeps_decision_fields(self):
        [view] = controller_observation_view([self._issue()])
        [result], omitted = compact_observations([view], budget_chars=1800)
        self.assertEqual(omitted, [])
        self.assertTrue(result["semantic_summary"])
        self.assertEqual(result["title"], "Staged controller view")
        self.assertEqual(result["state"], "open")
        self.assertIn("staged controller view", result["goal"])
        self.assertIn("page stored results", result["acceptance_criteria"])
        self.assertNotIn("large diagnostic detail", json.dumps(result))

    def test_summary_that_cannot_fit_falls_back_to_identity_stub(self):
        [view] = controller_observation_view([self._issue()])
        [result], omitted = compact_observations([view], budget_chars=150)
        self.assertEqual(omitted, [0])
        self.assertEqual(result["operation"], "github_issue")
        self.assertEqual(result["args"], {"issue_number": 232})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["event_id"], 41)


class ReadToolResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "state.db")
        store = NexusTaskStore(self.db_path)
        session = store.get_or_create(source_type="test", source_id="232")
        self.event_id = store.append_event(
            session.id, role="tool", kind="operation",
            content=json.dumps({"result": "abcdefghijklmnopqrstuvwxyz", "other": 1}),
        )
        store.close()
        self.registry = OperationsRegistry(
            experiment_service=SimpleNamespace(), db_path=self.db_path,
            repo_root=self.tmp.name,
        )
        self.addCleanup(self.registry.close)

    def test_deterministic_slice_is_retrieved_by_event_id(self):
        before = self.registry._conn.total_changes
        args = {"event_id": self.event_id, "section": "result", "offset": 5, "limit": 7}
        first = self.registry.execute("read_tool_result", args)
        second = self.registry.execute("read_tool_result", args)
        self.assertTrue(first.success)
        self.assertEqual(first.output_lines, second.output_lines)
        payload = json.loads(first.output_lines[0])
        self.assertEqual(payload["content"], "fghijkl")
        self.assertEqual(payload["next_offset"], 12)
        self.assertEqual(self.registry._conn.total_changes, before)
        audit_count = self.registry._conn.execute(
            "SELECT COUNT(*) FROM operations_audit WHERE operation='read_tool_result'"
        ).fetchone()[0]
        self.assertEqual(audit_count, 0)

    def test_successive_ranges_are_progress_and_repeat_guidance_is_paging_specific(self):
        guard = PersistentToolLoopGuard(self.db_path)
        self.addCleanup(guard.close)
        first_args = {"operation": "read_tool_result", "args": {"event_id": self.event_id, "offset": 0, "limit": 5}}
        next_args = {"operation": "read_tool_result", "args": {"event_id": self.event_id, "offset": 5, "limit": 5}}
        first = guard.check("s", "operation", first_args, state_token="v1")
        guard.record_result(first.call_id, {"success": True})
        self.assertTrue(guard.check("s", "operation", next_args, state_token="v1").allowed)
        duplicate = guard.check("s", "operation", first_args, state_token="v1")
        self.assertFalse(duplicate.allowed)
        self.assertIn("offset", duplicate.observation["recovery"])


if __name__ == "__main__":
    unittest.main()
