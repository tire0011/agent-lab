"""Explicit heartbeats decide liveness for long-running work (#162).

Before this, "is this session alive?" was answered from ``updated_at``, which
only says the row changed recently. That is wrong in both directions: a healthy
controller turn can spend many minutes inside one local inference call without
writing anything, while a dead one keeps its last ``updated_at`` forever - which
is why #160 had to wait six hours before calling anything abandoned.

These tests pin the replacement: a worker claims work, says so periodically, and
work whose worker went quiet is reconciled to an explicit terminal outcome.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules import heartbeat as heartbeat_module
from modules.heartbeat import (
    DEFAULT_STALE_HEARTBEAT_SECONDS,
    MIN_STALE_HEARTBEAT_SECONDS,
    Heartbeat,
    is_stale,
    resolve_interval,
    resolve_stale_timeout,
    worker_id,
)
from modules.nexus_task_store import (
    STATUS_ABANDONED,
    TERMINAL_REASON_HEARTBEAT_TIMEOUT,
    NexusTaskStore,
)


def _ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


class HeartbeatTimeoutTests(unittest.TestCase):
    """The timeout is configuration, but not every value is safe to honour."""

    def test_zero_and_negative_fall_back_to_the_default(self):
        # Honouring 0 literally would declare every claim in flight dead.
        self.assertEqual(resolve_stale_timeout(0), DEFAULT_STALE_HEARTBEAT_SECONDS)
        self.assertEqual(resolve_stale_timeout(-30), DEFAULT_STALE_HEARTBEAT_SECONDS)

    def test_unparsable_configuration_falls_back_to_the_default(self):
        for value in ("", "   ", "soon", None, object()):
            self.assertEqual(resolve_stale_timeout(value), DEFAULT_STALE_HEARTBEAT_SECONDS)

    def test_small_values_are_raised_to_the_floor(self):
        resolved = resolve_stale_timeout(5)

        self.assertGreaterEqual(resolved, MIN_STALE_HEARTBEAT_SECONDS)
        # Also at least a few missed beats: one late heartbeat is not death.
        self.assertGreaterEqual(resolved, resolve_interval() * 2)

    def test_a_deliberately_long_timeout_is_kept(self):
        self.assertEqual(resolve_stale_timeout(6 * 60 * 60), 6 * 60 * 60)

    def test_interval_is_bounded_but_configurable(self):
        self.assertEqual(resolve_interval(45), 45)
        self.assertGreaterEqual(resolve_interval(0), 1)
        self.assertLessEqual(resolve_interval(10**9), 3600)

    def test_work_that_never_beat_is_not_called_stale(self):
        # No claim means no owner to declare dead; that is a different problem.
        self.assertFalse(is_stale(None, 60))

    def test_worker_identity_is_stable_within_a_process(self):
        self.assertEqual(worker_id(), worker_id())
        self.assertIn(":", worker_id())


class HeartbeatThreadTests(unittest.TestCase):
    def test_a_failing_refresh_never_escapes_into_the_work(self):
        def boom() -> bool:
            raise RuntimeError("db gone")

        beat = Heartbeat(boom, interval_seconds=1, name="test-beat")

        self.assertTrue(beat.beat_once())  # survivable: retried next tick
        self.assertFalse(beat.lost_claim)

    def test_losing_the_claim_stops_the_beating(self):
        stopped = threading.Event()

        def refresh() -> bool:
            stopped.set()
            return False

        beat = Heartbeat(refresh, interval_seconds=1, name="test-beat")
        beat.start()
        self.addCleanup(beat.stop)

        self.assertTrue(stopped.wait(timeout=10))
        self.assertTrue(beat.lost_claim)

    def test_it_beats_while_the_work_runs(self):
        beats = threading.Semaphore(0)
        beat = Heartbeat(
            lambda: (beats.release(), True)[1], interval_seconds=1, name="test-beat"
        )
        beat.start()
        self.addCleanup(beat.stop)

        self.assertTrue(beats.acquire(timeout=10))


class SessionClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "agent.db")
        self.store = NexusTaskStore(db_path=self.db_path)
        self.addCleanup(self.store.close)

    def _session(
        self,
        source_id: str,
        *,
        status: str = "active",
        worker: str | None = "worker-a",
        last_heartbeat_at: str | None = None,
        updated_at: str | None = None,
    ) -> str:
        session = self.store.get_or_create(
            source_type="task", source_id=source_id, title=f"t-{source_id}"
        )
        if worker is not None:
            self.store.claim_session(session.id, worker_id=worker)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE nexus_task_sessions SET status=? WHERE id=?", (status, session.id)
        )
        if last_heartbeat_at is not None:
            conn.execute(
                "UPDATE nexus_task_sessions SET last_heartbeat_at=? WHERE id=?",
                (last_heartbeat_at, session.id),
            )
        if updated_at is not None:
            conn.execute(
                "UPDATE nexus_task_sessions SET updated_at=? WHERE id=?",
                (updated_at, session.id),
            )
        conn.commit()
        conn.close()
        return session.id

    # -- ownership ----------------------------------------------------------

    def test_claiming_records_who_owns_the_work(self):
        sid = self._session("s1", worker="worker-a")

        session = self.store.get(sid)

        self.assertEqual(session.worker_id, "worker-a")
        self.assertIsNotNone(session.claimed_at)
        self.assertIsNotNone(session.last_heartbeat_at)

    def test_a_heartbeat_from_a_foreign_worker_is_refused(self):
        # A worker whose claim was reaped or taken over must not be able to
        # keep that claim alive from the outside.
        sid = self._session("s2", worker="worker-a")

        self.assertFalse(self.store.heartbeat(sid, worker_id="worker-b"))
        self.assertTrue(self.store.heartbeat(sid, worker_id="worker-a"))

    def test_a_heartbeat_does_not_disguise_itself_as_activity(self):
        # updated_at must keep meaning "the session changed", otherwise the
        # heartbeat would silently re-arm every timestamp-based check.
        sid = self._session("s3", worker="worker-a")
        before = self.store.get(sid).updated_at

        self.store.heartbeat(sid, worker_id="worker-a")
        after = self.store.get(sid)

        self.assertEqual(after.updated_at, before)
        self.assertNotEqual(after.last_heartbeat_at, before)

    # -- healthy work -------------------------------------------------------

    def test_slow_but_beating_work_is_left_alone(self):
        # The case #160's six-hour threshold existed to protect: a turn that
        # writes nothing for a long time but is demonstrably alive.
        sid = self._session(
            "s4", worker="worker-a", updated_at=_ago(hours=9), last_heartbeat_at=_ago(seconds=5)
        )

        self.assertEqual(self.store.reconcile_stale_heartbeats(120), [])
        self.assertEqual(self.store.get(sid).status, "active")

    def test_the_legacy_updated_at_reaper_defers_to_a_live_heartbeat(self):
        # Otherwise a healthy nine-hour claim would still be reaped by #160's
        # path, and the heartbeat would buy nothing.
        sid = self._session(
            "s5", worker="worker-a", updated_at=_ago(days=2), last_heartbeat_at=_ago(seconds=5)
        )

        self.store.abandon_stale_sessions(6 * 60 * 60)

        self.assertEqual(self.store.get(sid).status, "active")

    def test_sessions_that_never_claimed_still_fall_to_the_legacy_reaper(self):
        # Rows written before #162 have no owner to ask; they must not become
        # invisible now that a better signal exists.
        sid = self._session("s6", worker=None, updated_at=_ago(days=12))

        self.store.abandon_stale_sessions(6 * 60 * 60)

        self.assertEqual(self.store.get(sid).status, STATUS_ABANDONED)

    # -- dead work ----------------------------------------------------------

    def test_a_silent_worker_is_reconciled_with_an_explicit_reason(self):
        sid = self._session("s7", worker="worker-a", last_heartbeat_at=_ago(minutes=30))

        released = self.store.reconcile_stale_heartbeats(120)

        self.assertEqual([s.id for s in released], [sid])
        session = self.store.get(sid)
        self.assertEqual(session.status, STATUS_ABANDONED)
        self.assertEqual(session.terminal_reason, TERMINAL_REASON_HEARTBEAT_TIMEOUT)
        self.assertIsNone(session.last_heartbeat_at)
        # Who died stays on the row: ownership is the point of a claim.
        self.assertEqual(session.worker_id, "worker-a")

    def test_reconciling_records_why_in_the_journal(self):
        sid = self._session("s8", worker="worker-a", last_heartbeat_at=_ago(minutes=30))

        self.store.reconcile_stale_heartbeats(120)

        events = self.store.recent_events(sid, kind=TERMINAL_REASON_HEARTBEAT_TIMEOUT)
        self.assertEqual(len(events), 1)
        self.assertIn("worker-a", events[0]["content"])
        self.assertEqual(events[0]["metadata"]["worker_id"], "worker-a")

    def test_reconciling_is_idempotent(self):
        self._session("s9", worker="worker-a", last_heartbeat_at=_ago(minutes=30))

        first = self.store.reconcile_stale_heartbeats(120)
        second = self.store.reconcile_stale_heartbeats(120)
        third = self.store.reconcile_stale_heartbeats(120)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(third, [])

    def test_reconciling_never_reports_a_session_it_did_not_change(self):
        self._session("s10", worker="worker-a", last_heartbeat_at=_ago(minutes=30))
        self._session("s11", worker="worker-b", last_heartbeat_at=_ago(seconds=3))

        listed = {s.id for s in self.store.stale_heartbeat_sessions(120)}
        released = {s.id for s in self.store.reconcile_stale_heartbeats(120)}

        self.assertEqual(listed, released)
        self.assertEqual(len(listed), 1)

    def test_an_invalid_timeout_cannot_reap_live_work(self):
        sid = self._session("s12", worker="worker-a", last_heartbeat_at=_ago(seconds=2))

        for timeout in (0, -1, "nonsense", None):
            self.assertEqual(self.store.reconcile_stale_heartbeats(timeout), [])
        self.assertEqual(self.store.get(sid).status, "active")

    # -- resting states -----------------------------------------------------

    def test_a_session_waiting_for_a_human_is_never_called_dead(self):
        # Nobody is supposed to be working, so silence is correct here.
        sid = self._session(
            "s13",
            status="waiting_for_user",
            worker="worker-a",
            last_heartbeat_at=_ago(days=30),
        )

        self.store.reconcile_stale_heartbeats(120)

        self.assertEqual(self.store.get(sid).status, "waiting_for_user")

    def test_a_blocked_session_is_never_called_dead(self):
        sid = self._session(
            "s14",
            status="blocked_missing_capability",
            worker="worker-a",
            last_heartbeat_at=_ago(days=30),
        )

        self.store.reconcile_stale_heartbeats(120)

        self.assertEqual(self.store.get(sid).status, "blocked_missing_capability")

    def test_finished_work_is_never_reopened(self):
        sid = self._session(
            "s15", status="action_selected", worker="worker-a", last_heartbeat_at=_ago(days=30)
        )

        self.store.reconcile_stale_heartbeats(120)

        self.assertEqual(self.store.get(sid).status, "action_selected")

    def test_work_waiting_on_the_local_model_still_has_to_beat(self):
        # waiting_for_llm is not a resting state: the worker is right there,
        # blocked on its own inference job, so it can and must keep beating.
        sid = self._session(
            "s16", status="waiting_for_llm", worker="worker-a", last_heartbeat_at=_ago(minutes=30)
        )

        self.store.reconcile_stale_heartbeats(120)

        self.assertEqual(self.store.get(sid).status, STATUS_ABANDONED)

    # -- restart / takeover -------------------------------------------------

    def test_a_restarted_worker_can_take_over_its_reconciled_session(self):
        sid = self._session("s17", worker="worker-a", last_heartbeat_at=_ago(minutes=30))
        self.store.reconcile_stale_heartbeats(120)

        self.store.claim_session(sid, worker_id="worker-a-restarted")
        self.store.set_status(sid, "active")

        session = self.store.get(sid)
        self.assertEqual(session.worker_id, "worker-a-restarted")
        self.assertIsNone(session.terminal_reason)
        self.assertIsNotNone(session.last_heartbeat_at)
        # And the worker that died can no longer touch it.
        self.assertFalse(self.store.heartbeat(sid, worker_id="worker-a"))

    def test_a_taken_over_session_is_only_reaped_once_for_one_death(self):
        sid = self._session("s18", worker="worker-a", last_heartbeat_at=_ago(minutes=30))
        self.store.reconcile_stale_heartbeats(120)
        self.store.claim_session(sid, worker_id="worker-b")
        self.store.set_status(sid, "active")

        self.assertEqual(self.store.reconcile_stale_heartbeats(120), [])
        self.assertEqual(self.store.get(sid).status, "active")

    def test_releasing_a_claim_states_the_outcome(self):
        sid = self._session("s19", worker="worker-a")

        self.store.release_claim(sid, reason="action_selected", worker_id="worker-a")

        session = self.store.get(sid)
        self.assertEqual(session.terminal_reason, "action_selected")
        self.assertIsNone(session.last_heartbeat_at)
        self.assertEqual(self.store.reconcile_stale_heartbeats(120), [])

    def test_a_foreign_worker_cannot_close_someone_elses_claim(self):
        sid = self._session("s20", worker="worker-a")

        self.assertFalse(
            self.store.release_claim(sid, reason="answer", worker_id="worker-b")
        )
        self.assertIsNotNone(self.store.get(sid).last_heartbeat_at)

    # -- observability ------------------------------------------------------

    def test_liveness_reports_owners_ages_and_staleness(self):
        live = self._session("s21", worker="worker-live", last_heartbeat_at=_ago(seconds=4))
        dead = self._session("s22", worker="worker-dead", last_heartbeat_at=_ago(minutes=45))
        self._session("s23", status="waiting_for_user", worker="worker-rest")

        snapshot = self.store.liveness(120)

        by_id = {claim["session_id"]: claim for claim in snapshot["claims"]}
        self.assertFalse(by_id[live]["stale"])
        self.assertTrue(by_id[dead]["stale"])
        self.assertEqual(by_id[dead]["worker_id"], "worker-dead")
        self.assertGreater(by_id[dead]["heartbeat_age_seconds"], 120)
        self.assertEqual(snapshot["live_claims"], 1)
        self.assertEqual(snapshot["stale_claims"], 1)
        self.assertEqual(snapshot["stale_after_seconds"], 120)
        self.assertEqual(snapshot["counts_by_status"]["waiting_for_user"], 1)

    def test_liveness_counts_working_sessions_that_never_claimed(self):
        # The blind spot heartbeats exist to close is reported, not hidden.
        self._session("s24", worker=None)

        snapshot = self.store.liveness(120)

        self.assertEqual(snapshot["unclaimed_working_sessions"], 1)
        self.assertEqual(snapshot["claims"], [])

    def test_liveness_cannot_be_asked_to_call_live_work_dead(self):
        self._session("s25", worker="worker-a", last_heartbeat_at=_ago(seconds=2))

        snapshot = self.store.liveness(1)

        self.assertGreaterEqual(snapshot["stale_after_seconds"], MIN_STALE_HEARTBEAT_SECONDS)
        self.assertEqual(snapshot["stale_claims"], 0)

    def test_migration_adds_the_columns_to_an_existing_database(self):
        # The live database predates #162; reopening it must not lose rows.
        sid = self._session("s26", worker="worker-a")
        self.store.close()

        reopened = NexusTaskStore(db_path=self.db_path)
        self.addCleanup(reopened.close)
        self.store = reopened

        self.assertEqual(reopened.get(sid).worker_id, "worker-a")


class HeartbeatEnvironmentTests(unittest.TestCase):
    def test_the_timeout_can_be_configured_from_the_environment(self):
        original = heartbeat_module.os.environ.get(heartbeat_module.STALE_ENV_VAR)
        heartbeat_module.os.environ[heartbeat_module.STALE_ENV_VAR] = "1800"
        try:
            self.assertEqual(resolve_stale_timeout(), 1800)
        finally:
            if original is None:
                heartbeat_module.os.environ.pop(heartbeat_module.STALE_ENV_VAR, None)
            else:
                heartbeat_module.os.environ[heartbeat_module.STALE_ENV_VAR] = original


if __name__ == "__main__":
    unittest.main()
