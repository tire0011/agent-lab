"""Heartbeat state is inspectable from outside the process (#162).

A liveness signal only helps if Nexus and an operator can ask for it. This
endpoint answers "who owns this work, and when did they last say they were
alive" without changing anything.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api

# Resolved from the environment at import time, so the suite reads it back
# rather than assuming which value won.
_KEY = {"x-api-key": api.API_KEY}
from modules.nexus_task_store import NexusTaskStore


class NexusLivenessApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "agent.db")
        self.store = NexusTaskStore(db_path=self.db_path)
        self.addCleanup(self.store.close)
        api.app.dependency_overrides[api.get_nexus_task_store] = lambda: self.store
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)

    def _claim(self, source_id: str, worker: str, *, heartbeat_minutes_ago: float) -> str:
        session = self.store.get_or_create(source_type="task", source_id=source_id)
        self.store.claim_session(session.id, worker_id=worker)
        stamp = (
            datetime.now(timezone.utc) - timedelta(minutes=heartbeat_minutes_ago)
        ).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE nexus_task_sessions SET last_heartbeat_at=? WHERE id=?",
            (stamp, session.id),
        )
        conn.commit()
        conn.close()
        return session.id

    def test_liveness_requires_the_api_key(self):
        self.assertEqual(self.client.get("/diagnostics/nexus/liveness").status_code, 401)

    def test_liveness_reports_owner_age_and_staleness(self):
        live = self._claim("live", "worker-live", heartbeat_minutes_ago=0.1)
        dead = self._claim("dead", "worker-dead", heartbeat_minutes_ago=120)

        response = self.client.get(
            "/diagnostics/nexus/liveness", headers=_KEY
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        claims = {claim["session_id"]: claim for claim in body["claims"]}
        self.assertFalse(claims[live]["stale"])
        self.assertTrue(claims[dead]["stale"])
        self.assertEqual(claims[dead]["worker_id"], "worker-dead")
        self.assertGreater(claims[dead]["heartbeat_age_seconds"], 3600)
        self.assertEqual(body["live_claims"], 1)
        self.assertEqual(body["stale_claims"], 1)
        self.assertGreaterEqual(body["stale_after_seconds"], 60)

    def test_liveness_never_reaps_what_it_reports(self):
        dead = self._claim("dead", "worker-dead", heartbeat_minutes_ago=120)

        self.client.get("/diagnostics/nexus/liveness", headers=_KEY)

        self.assertEqual(self.store.get(dead).status, "active")

    def test_a_tiny_timeout_query_cannot_make_live_work_look_dead(self):
        self._claim("live", "worker-live", heartbeat_minutes_ago=0.05)

        response = self.client.get(
            "/diagnostics/nexus/liveness?stale_after_seconds=1",
            headers=_KEY,
        )

        body = response.json()
        self.assertGreaterEqual(body["stale_after_seconds"], 60)
        self.assertEqual(body["stale_claims"], 0)

    def test_a_negative_timeout_query_is_rejected(self):
        response = self.client.get(
            "/diagnostics/nexus/liveness?stale_after_seconds=-5",
            headers=_KEY,
        )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
