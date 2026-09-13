"""A controller turn claims its session, beats, and closes the claim (#162).

The store tests pin the contract; this pins that the controller actually
honours it, because an unclaimed turn is exactly the state heartbeats were
added to remove.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.nexus_task_store import STATUS_ABANDONED, NexusTaskStore
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

    def test_a_turn_that_crashes_stops_beating_and_is_reapable(self):
        class _Boom:
            async def complete(self, **kwargs):
                raise RuntimeError("local model died mid-turn")

        orchestrator = self._orchestrator(_Boom())

        with self.assertRaises(Exception):
            orchestrator.run("inspect something")

        claims = self.store.liveness()["claims"]
        self.assertEqual(len(claims), 1)
        self.assertTrue(claims[0]["worker_id"])
        # Still `active` with a frozen heartbeat - which is precisely what the
        # reaper is for, and what `updated_at` could never tell apart from a
        # slow healthy turn.
        self.assertEqual(claims[0]["status"], "active")

        # Once the silence outlasts the timeout, the claim is reconciled.
        session_id = claims[0]["session_id"]
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE nexus_task_sessions SET last_heartbeat_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), session_id),
        )
        conn.commit()
        conn.close()

        released = self.store.reconcile_stale_heartbeats(120)

        self.assertEqual([s.id for s in released], [session_id])
        self.assertEqual(self.store.get(session_id).status, STATUS_ABANDONED)


if __name__ == "__main__":
    unittest.main()
