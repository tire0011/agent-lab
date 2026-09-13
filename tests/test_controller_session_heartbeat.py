"""A controller turn claims its session, beats, and closes the claim (#162).

The store tests pin the contract; this pins that the controller actually
honours it, because an unclaimed turn is exactly the state heartbeats were
added to remove.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.nexus_task_store import (
    STATUS_FAILED,
    TERMINAL_REASON_TURN_FAILED,
    NexusTaskStore,
)
from modules.task_orchestrator import ACTION_ANSWER, NexusTaskOrchestrator


class _Registry:
    def __init__(self, db_path: str, repo_root: str):
        self.db_path = db_path
        self.repo_root = Path(repo_root)

    def catalog(self):
        return []

    def execute(self, operation, args, *, caller):
        return SimpleNamespace(to_dict=lambda: {"operation": operation, "success": True})


class _Delegation:
    def catalog(self):
        return []


class _Client:
    """Answers one turn, and looks at the session row while the turn runs."""

    def __init__(self, responses, observer=None):
        self.responses = list(responses)
        self.observer = observer

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        if self.observer is not None:
            self.observer()
        return self.responses.pop(0)


class ControllerSessionHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "state.db")
        self.store = NexusTaskStore(db_path=self.db_path)
        self.addCleanup(self.store.close)

    def _orchestrator(self, client) -> NexusTaskOrchestrator:
        orchestrator = NexusTaskOrchestrator(
            operations_registry=_Registry(self.db_path, self._tmp.name),
            experiment_service=MagicMock(),
            llama_client=client,
            delegation_service=_Delegation(),
            task_store=self.store,
        )
        self.addCleanup(orchestrator.close)
        return orchestrator

    def test_a_running_turn_is_owned_and_beating(self):
        seen: list[tuple[str | None, str | None]] = []

        def observe() -> None:
            for session in self.store.liveness()["claims"]:
                seen.append((session["worker_id"], session["status"]))

        orchestrator = self._orchestrator(
            _Client(['{"action":"answer","message":"done","reason":"done"}'], observe)
        )

        result = orchestrator.run("inspect something")

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(len(seen), 1)
        worker, status = seen[0]
        self.assertTrue(worker)
        self.assertIn(status, ("active", "waiting_for_llm"))

    def test_a_finished_turn_leaves_no_claim_and_states_its_outcome(self):
        orchestrator = self._orchestrator(
            _Client(['{"action":"answer","message":"done","reason":"done"}'])
        )

        result = orchestrator.run("inspect something")

        session = self.store.get(result.session_id)
        self.assertEqual(session.terminal_reason, ACTION_ANSWER)
        self.assertIsNone(session.last_heartbeat_at)
        self.assertTrue(session.worker_id)
        # Nothing for the reaper to find: the turn ended on purpose.
        self.assertEqual(self.store.reconcile_stale_heartbeats(60), [])
        self.assertEqual(self.store.liveness()["claims"], [])

    def _only_session_id(self) -> str:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT id FROM nexus_task_sessions LIMIT 1").fetchone()
        conn.close()
        return row[0]

    def test_a_turn_that_raises_records_what_happened(self):
        # Left to the reaper this would eventually read `heartbeat_timeout` -
        # "nobody ever told us" - when in fact we watched it fail. A process
        # that is killed outright still falls to the reaper; an exception we
        # caught should not.
        class _Boom:
            async def complete(self, **kwargs):
                raise RuntimeError("local model died mid-turn")

        orchestrator = self._orchestrator(_Boom())

        with self.assertRaises(Exception):
            orchestrator.run("inspect something")

        session = self.store.get(self._only_session_id())
        self.assertEqual(session.status, STATUS_FAILED)
        self.assertEqual(session.terminal_reason, TERMINAL_REASON_TURN_FAILED)
        self.assertIsNone(session.last_heartbeat_at)
        events = self.store.recent_events(session.id, kind=TERMINAL_REASON_TURN_FAILED)
        self.assertEqual(len(events), 1)
        self.assertIn("RuntimeError", events[0]["content"])
        # Nothing left for the reaper to find, so nothing to misclassify later.
        self.assertEqual(self.store.liveness()["claims"], [])
        self.assertEqual(self.store.reconcile_stale_heartbeats(60), [])

    def test_a_turn_taken_over_mid_flight_cannot_write_its_own_ending(self):
        # A restarted worker claims the session while this turn is still
        # thinking. Everything the old turn writes afterwards - its status
        # flips, its decision, its claim release - must miss.
        taken_over: list[str] = []

        def take_over() -> None:
            session_id = self._only_session_id()
            self.store.claim_session(session_id, worker_id="worker-restarted")
            taken_over.append(session_id)

        orchestrator = self._orchestrator(
            _Client(['{"action":"answer","message":"done","reason":"done"}'], take_over)
        )

        orchestrator.run("inspect something")

        session = self.store.get(taken_over[0])
        self.assertEqual(session.worker_id, "worker-restarted")
        # The old worker's `finally: active` and its final decision both miss,
        # so the row still reads as the new owner left it.
        self.assertEqual(session.status, "waiting_for_llm")
        self.assertIsNone(session.terminal_reason)
        self.assertIsNotNone(session.last_heartbeat_at)
        self.assertEqual(self.store.recent_events(session.id, kind="answer"), [])


if __name__ == "__main__":
    unittest.main()
