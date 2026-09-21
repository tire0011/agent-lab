from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.operation_contract import OperationSpec
from modules.task_orchestrator import (
    ACTION_ANSWER,
    ACTION_MISSING_CAPABILITY,
    NexusTaskOrchestrator,
)


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("unexpected model call")
        return self.responses.pop(0)


class _MutableRegistry:
    def __init__(self, root: str):
        self.repo_root = Path(root)
        self.db_path = str(self.repo_root / "agent.db")
        self.enabled = False
        self.calls: list[tuple[str, dict, str]] = []
        self.spec = OperationSpec(
            name="test_missing_capability",
            description="Provide the deliberately missing test capability.",
            arguments=[],
            tags=["test", "missing", "capability"],
            mutates_state=False,
        )

    def catalog(self):
        return [self.spec] if self.enabled else []

    def execute(self, operation, args, *, caller):
        self.calls.append((operation, dict(args), caller))
        return SimpleNamespace(
            to_dict=lambda: {
                "operation": operation,
                "success": True,
                "output_lines": ["capability executed"],
                "duration_ms": 1,
                "exit_code": 0,
                "error_summary": None,
            }
        )


class MissingCapabilityResumeTests(unittest.TestCase):
    def test_same_session_resumes_after_capability_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = _MutableRegistry(tmp)
            client = _Client([
                '{"action":"find_tool","goal":"test_missing_capability",'
                '"reason":"the task requires the test capability"}',
                '{"action":"missing_capability","message":"Need test_missing_capability.",'
                '"reason":"the registry does not expose it",'
                '"information_gap":"test_missing_capability",'
                '"gap_kind":"missing_capability",'
                '"capability":"test_missing_capability","required_inputs":[]}',
                '{"action":"find_tool","goal":"test_missing_capability",'
                '"reason":"the required capability is now available"}',
                '{"status":"found","operation":"test_missing_capability",'
                '"reason":"exact capability match","arguments":{},"related_tools":[]}',
                '{"action":"answer","message":"Completed with the new capability.",'
                '"reason":"the capability executed successfully"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=SimpleNamespace(),
                llama_client=client,
                max_steps=4,
            )
            self.addCleanup(orchestrator.close)

            original_task = "Complete work that requires test_missing_capability."
            blocked = orchestrator.run(
                original_task,
                source_type="task",
                source_id="issue-237-e2e",
            )

            self.assertEqual(blocked.action, ACTION_MISSING_CAPABILITY)
            session_id = blocked.session_id
            self.assertIsNotNone(session_id)
            session = orchestrator.task_store.get(session_id)
            self.assertEqual(session.status, "blocked_missing_capability")

            blockers = orchestrator.task_store.blocked_missing_capabilities()
            self.assertEqual(len(blockers), 1)
            self.assertEqual(blockers[0]["session"].id, session_id)
            self.assertEqual(blockers[0]["capability"], "test_missing_capability")
            self.assertEqual(blockers[0]["trigger_task"], original_task)

            registry.enabled = True
            resumed = orchestrator.resume_available_capabilities()

            self.assertEqual(len(resumed), 1)
            self.assertEqual(resumed[0].action, ACTION_ANSWER)
            self.assertEqual(resumed[0].session_id, session_id)
            self.assertEqual(
                [call[0] for call in registry.calls],
                ["test_missing_capability"],
            )

            session_rows = orchestrator.task_store._conn.execute(
                "SELECT id FROM nexus_task_sessions"
            ).fetchall()
            self.assertEqual([row["id"] for row in session_rows], [session_id])

            inputs = orchestrator.task_store._conn.execute(
                """SELECT content FROM nexus_task_events
                   WHERE session_id=? AND role='user' AND kind='input'
                   ORDER BY id""",
                (session_id,),
            ).fetchall()
            self.assertEqual([row["content"] for row in inputs], [original_task])

            events = orchestrator.task_store.recent_events(session_id, limit=50)
            self.assertTrue(any(event["kind"] == "capability_available" for event in events))
            self.assertTrue(any(event["kind"] == "operation" for event in events))
            self.assertEqual(
                orchestrator.task_store.get(session_id).status,
                "action_selected",
            )

    def test_unavailable_capability_does_not_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = _MutableRegistry(tmp)
            client = _Client([
                '{"action":"missing_capability","message":"Need test_missing_capability.",'
                '"reason":"not available","information_gap":"test_missing_capability",'
                '"gap_kind":"missing_capability","capability":"test_missing_capability"}',
            ])
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=SimpleNamespace(),
                llama_client=client,
                max_steps=1,
            )
            self.addCleanup(orchestrator.close)

            blocked = orchestrator.run("Need the missing test capability")
            self.assertEqual(blocked.action, ACTION_MISSING_CAPABILITY)

            self.assertEqual(orchestrator.resume_available_capabilities(), [])
            self.assertEqual(
                orchestrator.task_store.get(blocked.session_id).status,
                "blocked_missing_capability",
            )


if __name__ == "__main__":
    unittest.main()
