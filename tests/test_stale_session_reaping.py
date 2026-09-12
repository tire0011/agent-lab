"""Abandoned controller sessions get an explicit end state (#158).

A session only reaches a terminal status through the final decision path, so a
turn that dies mid-flight leaves its row `active` forever. On the live database
17 of the 41 sessions ever started were in that state, untouched for 8 to 19
days, with nothing reaping or reporting them.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.nexus_task_store import NexusTaskStore


def _ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


class StaleSessionReapingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "agent.db")
        self.store = NexusTaskStore(db_path=self.db_path)
        self.addCleanup(self.store.close)

    def _session(self, source_id: str, *, status: str, updated_at: str) -> str:
        session = self.store.get_or_create(
            source_type="task", source_id=source_id, title=f"t-{source_id}"
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE nexus_task_sessions SET status=?, updated_at=? WHERE id=?",
            (status, updated_at, session.id),
        )
        conn.commit()
        conn.close()
        return session.id

    def test_long_dead_session_is_marked_abandoned(self):
        sid = self._session("s1", status="active", updated_at=_ago(days=19))

        reaped = self.store.abandon_stale_sessions(6 * 60 * 60)

        self.assertEqual([s.id for s in reaped], [sid])
        self.assertEqual(self.store.get(sid).status, "abandoned")

    def test_slow_but_live_session_is_left_alone(self):
        # The mistake to avoid: a healthy turn that is merely slow. Local
        # inference plus tool calls can legitimately run for many minutes.
        sid = self._session("s2", status="active", updated_at=_ago(minutes=20))

        reaped = self.store.abandon_stale_sessions(6 * 60 * 60)

        self.assertEqual(reaped, [])
        self.assertEqual(self.store.get(sid).status, "active")

    def test_finished_sessions_are_never_touched(self):
        sid = self._session("s3", status="action_selected", updated_at=_ago(days=30))

        self.store.abandon_stale_sessions(6 * 60 * 60)

        self.assertEqual(self.store.get(sid).status, "action_selected")

    def test_session_waiting_for_a_human_is_not_abandoned(self):
        # Waiting for a reply is a legitimate resting state, not a dead turn.
        sid = self._session("s4", status="waiting_for_user", updated_at=_ago(days=30))

        self.store.abandon_stale_sessions(6 * 60 * 60)

        self.assertEqual(self.store.get(sid).status, "waiting_for_user")

    def test_reaping_records_why(self):
        sid = self._session("s5", status="active", updated_at=_ago(days=9))

        self.store.abandon_stale_sessions(6 * 60 * 60)

        kinds = [e["kind"] for e in self.store.history(sid, limit=10)]
        self.assertIn("abandoned", kinds)

    def test_reaping_is_idempotent(self):
        self._session("s6", status="active", updated_at=_ago(days=9))

        first = self.store.abandon_stale_sessions(6 * 60 * 60)
        second = self.store.abandon_stale_sessions(6 * 60 * 60)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_threshold_has_a_floor(self):
        # A zero or negative threshold would abandon everything in flight.
        sid = self._session("s7", status="active", updated_at=datetime.now(timezone.utc).isoformat())

        self.store.abandon_stale_sessions(0)

        self.assertEqual(self.store.get(sid).status, "active")

    def test_reported_sessions_match_what_is_reaped(self):
        self._session("s8", status="active", updated_at=_ago(days=12))
        self._session("s9", status="active", updated_at=_ago(minutes=5))

        listed = {s.id for s in self.store.stale_sessions(6 * 60 * 60)}
        reaped = {s.id for s in self.store.abandon_stale_sessions(6 * 60 * 60)}

        self.assertEqual(listed, reaped)
        self.assertEqual(len(listed), 1)


if __name__ == "__main__":
    unittest.main()
