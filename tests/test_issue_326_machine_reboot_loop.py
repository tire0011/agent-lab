"""#326: a successful machine reboot ends the controller turn."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.task_orchestrator import ACTION_ANSWER, NexusTaskOrchestrator


class _Client:
    def __init__(self) -> None:
        self.responses = iter([
            '{"action":"find_tool","goal":"reboot the machine",'
            '"reason":"the reboot was requested"}',
            '{"status":"found","operation":"machine_reboot",'
            '"reason":"reboots the host","related_tools":[]}',
            '{"action":"call_tool","tool":"machine_reboot","arguments":{},'
            '"reason":"schedule the reboot"}',
            '{"action":"answer","message":"unexpected continuation",'
            '"reason":"the reboot call did not terminate the turn"}',
        ])
        self.calls = 0

    async def complete(self, *_args, **_kwargs):
        self.calls += 1
        return next(self.responses)


class _Registry:
    def __init__(self, db_path: str, repo_root: str) -> None:
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.calls = 0

    def catalog(self):
        return [SimpleNamespace(
            name="machine_reboot",
            description="Schedule a reboot of the machine.",
            tags=["machine", "reboot"],
            arguments=[],
            mutates_state=True,
            controller_allowed=True,
            to_dict=lambda: {
                "operation": "machine_reboot", "description": "Schedule a reboot",
                "arguments": [], "tags": ["machine", "reboot"], "mutates_state": True,
            },
        )]

    def execute(self, operation, args, *, caller):
        self.calls += 1
        return SimpleNamespace(to_dict=lambda: {
            "operation": operation,
            "success": True,
            "output_lines": ["reboot scheduled in 60 seconds"],
            "duration_ms": 1,
            "exit_code": 0,
            "error_summary": None,
        })


class MachineRebootLoopTests(unittest.TestCase):
    def test_successful_reboot_is_a_state_change_and_session_is_not_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _Client()
            registry = _Registry(str(Path(tmp) / "state.db"), tmp)
            orchestrator = NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=SimpleNamespace(),
                llama_client=client,
            )
            try:
                result = orchestrator.run("Reboot the machine now")
                session = orchestrator.task_store.get(result.session_id)
            finally:
                orchestrator.close()

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(registry.calls, 1, result.observations)
        self.assertEqual(client.calls, 2, "no controller call may be queued after reboot")
        self.assertEqual(session.status, "action_selected")
        evidence = result.observations[-1]["completion_evidence"]
        self.assertEqual(evidence["kind"], "state_change")


if __name__ == "__main__":
    unittest.main()
