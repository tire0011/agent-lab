"""Diagnostics classify what happened from heartbeat and terminal state (#163).

A diagnosis used to say "running", "interrupted" or "completed" from whichever
timestamp happened to be newest. These tests pin the sharper answer built on
the #162 contract: success, explicit failure, cancellation, a worker that
stopped heartbeating, a restart, healthy running work, and - just as
important - "unknown" when the evidence is missing or contradicts itself.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("AGENT_API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from modules.failure_analysis_service import _with_lifecycle_context
from modules.nexus_diagnostics import (
    EVIDENCE_HEARTBEAT,
    EVIDENCE_LOG,
    EVIDENCE_TERMINAL_STATE,
    LIFECYCLE_ABANDONED,
    LIFECYCLE_FAILED,
    LIFECYCLE_QUEUED,
    LIFECYCLE_RUNNING,
    LIFECYCLE_SUCCEEDED,
    LIFECYCLE_UNKNOWN,
    LIFECYCLE_WAITING,
    OUTCOME_CANCELLED,
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    OUTCOME_RUNNING,
    OUTCOME_STALE,
    OUTCOME_SUCCEEDED,
    OUTCOME_UNKNOWN,
    OUTCOME_WAITING,
    RETRY_INSUFFICIENT_EVIDENCE,
    RETRY_NOT_NEEDED,
    RETRY_NOT_RECOMMENDED,
    RETRY_POSSIBLE,
    NexusDiagnostics,
    classify_outcome,
    render_report,
)
from modules.nexus_task_store import (
    TERMINAL_REASON_TURN_CANCELLED,
    TERMINAL_REASON_TURN_FAILED,
    NexusTaskStore,
)
from tests.test_nexus_diagnostics import DiagnosticsFixture, _iso

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


class ClassifyOutcomeTests(unittest.TestCase):
    """The rule itself, on plain facts and a fixed clock."""

    def classify(self, lifecycle: str, **kwargs):
        kwargs.setdefault("stale_after_seconds", 900)
        return classify_outcome(lifecycle, now=NOW, **kwargs)

    def test_clean_success(self) -> None:
        result = self.classify(LIFECYCLE_SUCCEEDED, status="succeeded", updated_at=_ago(5))
        self.assertEqual(OUTCOME_SUCCEEDED, result.outcome)
        self.assertTrue(result.conclusive)
        self.assertEqual(EVIDENCE_TERMINAL_STATE, result.evidence)
        self.assertEqual(RETRY_NOT_NEEDED, result.retry)

    def test_explicit_failure_reports_the_error_and_where(self) -> None:
        result = self.classify(
            LIFECYCLE_FAILED,
            status="failed",
            error="tests failed: 3 errors",
            last_event={"at": _ago(2), "kind": "tests_failed", "phase": "verifying"},
        )
        self.assertEqual(OUTCOME_FAILED, result.outcome)
        self.assertEqual("tests failed: 3 errors", result.probable_cause)
        self.assertIn("verifying", result.explanation)
        self.assertEqual(RETRY_POSSIBLE, result.retry)

    def test_failure_without_error_is_flagged_not_invented(self) -> None:
        result = self.classify(LIFECYCLE_FAILED, status="failed")
        self.assertEqual(OUTCOME_FAILED, result.outcome)
        self.assertIsNone(result.probable_cause)
        self.assertTrue(result.evidence_gaps)

    def test_cancellation_by_status_and_by_terminal_reason(self) -> None:
        for kwargs in (
            {"lifecycle": "cancelled", "status": "cancelled"},
            {
                "lifecycle": LIFECYCLE_FAILED,
                "status": "failed",
                "terminal_reason": TERMINAL_REASON_TURN_CANCELLED,
                "own_claim": True,
            },
        ):
            with self.subTest(**kwargs):
                lifecycle = kwargs.pop("lifecycle")
                result = self.classify(lifecycle, **kwargs)
                self.assertEqual(OUTCOME_CANCELLED, result.outcome)
                self.assertEqual(RETRY_NOT_RECOMMENDED, result.retry)

    def test_running_work_whose_heartbeat_stopped_is_stale(self) -> None:
        result = self.classify(
            LIFECYCLE_RUNNING,
            status="active",
            last_heartbeat_at=_ago(40),
            own_claim=True,
            updated_at=_ago(1),  # a fresh row is not proof of life
            last_event={"at": _ago(41), "kind": "tool_call", "message": "git push"},
        )
        self.assertEqual(OUTCOME_STALE, result.outcome)
        self.assertEqual(EVIDENCE_HEARTBEAT, result.evidence)
        self.assertEqual(_ago(40), result.last_healthy_heartbeat_at)
        self.assertEqual("tool_call", result.last_event["kind"])
        self.assertEqual(RETRY_POSSIBLE, result.retry)

    def test_reconciled_heartbeat_timeout_keeps_the_last_heartbeat(self) -> None:
        result = self.classify(
            LIFECYCLE_ABANDONED,
            status="abandoned",
            terminal_reason="heartbeat_timeout",
            recorded_heartbeat_at=_ago(30),
            own_claim=True,
        )
        self.assertEqual(OUTCOME_STALE, result.outcome)
        self.assertTrue(result.conclusive)
        self.assertEqual(_ago(30), result.last_healthy_heartbeat_at)
        # Crash, kill and restart look alike here; the cause must say so.
        self.assertIn("not distinguishable", result.probable_cause)

    def test_restart_marker_is_interrupted(self) -> None:
        result = self.classify(
            LIFECYCLE_ABANDONED,
            status="interrupted",
            error="interrupted by service restart",
        )
        self.assertEqual(OUTCOME_INTERRUPTED, result.outcome)
        self.assertEqual("interrupted by service restart", result.probable_cause)

    def test_silence_alone_is_never_called_a_restart(self) -> None:
        result = self.classify(
            LIFECYCLE_RUNNING, status="running", last_heartbeat_at=_ago(120)
        )
        self.assertEqual(OUTCOME_STALE, result.outcome)
        self.assertNotIn("restart", (result.probable_cause or "").replace(
            "crash, kill or restart", ""
        ))

    def test_healthy_heartbeat_is_running(self) -> None:
        result = self.classify(
            LIFECYCLE_RUNNING, status="active", last_heartbeat_at=_ago(0.5), own_claim=True
        )
        self.assertEqual(OUTCOME_RUNNING, result.outcome)
        self.assertTrue(result.conclusive)
        self.assertEqual(_ago(0.5), result.last_healthy_heartbeat_at)

    def test_log_activity_without_heartbeat_is_running_but_inconclusive(self) -> None:
        result = self.classify(LIFECYCLE_RUNNING, status="implementing", last_log_at=_ago(2))
        self.assertEqual(OUTCOME_RUNNING, result.outcome)
        self.assertFalse(result.conclusive)
        self.assertEqual(EVIDENCE_LOG, result.evidence)
        self.assertTrue(any("no heartbeat" in gap for gap in result.evidence_gaps))

    def test_log_timestamps_with_z_suffix_count_as_liveness(self) -> None:
        """JSONL logs write ``...Z``; Python 3.10 cannot parse that natively."""
        stamp = (NOW - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        result = self.classify(LIFECYCLE_RUNNING, status="implementing", last_log_at=stamp)
        self.assertEqual(OUTCOME_RUNNING, result.outcome)
        self.assertEqual(EVIDENCE_LOG, result.evidence)

    def test_updated_at_alone_is_not_proof_of_life(self) -> None:
        result = self.classify(LIFECYCLE_RUNNING, status="running", updated_at=_ago(1))
        self.assertEqual(OUTCOME_UNKNOWN, result.outcome)
        self.assertFalse(result.conclusive)
        self.assertEqual(RETRY_INSUFFICIENT_EVIDENCE, result.retry)

    def test_freshly_queued_work_is_waiting(self) -> None:
        result = self.classify(LIFECYCLE_QUEUED, status="approved", updated_at=_ago(1))
        self.assertEqual(OUTCOME_WAITING, result.outcome)

    def test_resting_work_is_waiting_not_stale(self) -> None:
        result = self.classify(
            LIFECYCLE_WAITING, status="waiting_for_user", updated_at=_ago(60 * 24)
        )
        self.assertEqual(OUTCOME_WAITING, result.outcome)

    def test_contradictory_records_are_unknown(self) -> None:
        cases = {
            "terminal but still heartbeating": dict(
                lifecycle=LIFECYCLE_SUCCEEDED,
                status="succeeded",
                last_heartbeat_at=_ago(0.2),
                own_claim=True,
            ),
            "working but claim released": dict(
                lifecycle=LIFECYCLE_RUNNING,
                status="active",
                terminal_reason="action_selected",
                own_claim=True,
            ),
            "success with a failure reason": dict(
                lifecycle=LIFECYCLE_SUCCEEDED,
                status="succeeded",
                terminal_reason=TERMINAL_REASON_TURN_FAILED,
            ),
            "unrecognised status": dict(lifecycle=LIFECYCLE_UNKNOWN, status="weird"),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                lifecycle = kwargs.pop("lifecycle")
                result = self.classify(lifecycle, **kwargs)
                self.assertEqual(OUTCOME_UNKNOWN, result.outcome)
                self.assertFalse(result.conclusive)
                self.assertTrue(result.evidence_gaps)
                self.assertEqual(RETRY_INSUFFICIENT_EVIDENCE, result.retry)


class DriverSessionRuleTests(unittest.TestCase):
    """How the session driving a subject without its own claim is weighed."""

    def classify(self, lifecycle: str = LIFECYCLE_RUNNING, **kwargs):
        kwargs.setdefault("status", "running")
        kwargs.setdefault("stale_after_seconds", 900)
        return classify_outcome(lifecycle, now=NOW, **kwargs)

    def reconciled(self, **kwargs):
        return self.classify(
            driver_status="abandoned",
            driver_terminal_reason="heartbeat_timeout",
            driver_ended_at=_ago(10),
            recorded_heartbeat_at=_ago(30),
            **kwargs,
        )

    def test_reconciled_driver_is_conclusively_stale(self) -> None:
        result = self.reconciled(updated_at=_ago(1))
        self.assertEqual(OUTCOME_STALE, result.outcome)
        self.assertTrue(result.conclusive)
        self.assertEqual(EVIDENCE_HEARTBEAT, result.evidence)
        self.assertEqual("heartbeat_timeout", result.terminal_reason)
        self.assertEqual(_ago(30), result.last_healthy_heartbeat_at)
        self.assertEqual(RETRY_POSSIBLE, result.retry)
        self.assertEqual([], result.evidence_gaps)

    def test_queued_work_is_stale_too_once_its_driver_was_reconciled(self) -> None:
        result = self.reconciled(lifecycle=LIFECYCLE_QUEUED, updated_at=_ago(1))
        self.assertEqual(OUTCOME_STALE, result.outcome)

    def test_logs_from_before_the_reconciliation_do_not_outvote_it(self) -> None:
        result = self.reconciled(last_log_at=_ago(11))
        self.assertEqual(OUTCOME_STALE, result.outcome)
        self.assertTrue(result.conclusive)

    def test_logs_after_the_reconciliation_are_a_contradiction(self) -> None:
        result = self.reconciled(last_log_at=_ago(2))
        self.assertEqual(OUTCOME_UNKNOWN, result.outcome)
        self.assertFalse(result.conclusive)
        self.assertTrue(result.evidence_gaps)
        self.assertEqual(RETRY_INSUFFICIENT_EVIDENCE, result.retry)

    def test_old_logs_after_the_reconciliation_are_no_sign_of_life(self) -> None:
        result = self.classify(
            driver_status="abandoned",
            driver_terminal_reason="heartbeat_timeout",
            driver_ended_at=_ago(60),
            last_log_at=_ago(40),
        )
        self.assertEqual(OUTCOME_STALE, result.outcome)

    def test_expired_driver_heartbeat_with_later_fresh_logs_is_a_contradiction(self) -> None:
        result = self.classify(last_heartbeat_at=_ago(40), last_log_at=_ago(2))
        self.assertEqual(OUTCOME_UNKNOWN, result.outcome)
        self.assertFalse(result.conclusive)

    def test_expired_own_heartbeat_is_not_outvoted_by_logs(self) -> None:
        result = self.classify(
            last_heartbeat_at=_ago(40), own_claim=True, last_log_at=_ago(2)
        )
        self.assertEqual(OUTCOME_STALE, result.outcome)

    def test_driver_abandoned_by_inactivity_is_only_inconclusively_stale(self) -> None:
        result = self.classify(driver_status="abandoned", driver_ended_at=_ago(10))
        self.assertEqual(OUTCOME_STALE, result.outcome)
        self.assertFalse(result.conclusive)
        self.assertEqual(RETRY_INSUFFICIENT_EVIDENCE, result.retry)
        self.assertTrue(result.evidence_gaps)

    def test_a_driver_ending_does_not_override_a_terminal_subject(self) -> None:
        cases = {
            OUTCOME_SUCCEEDED: (LIFECYCLE_SUCCEEDED, "succeeded"),
            OUTCOME_FAILED: (LIFECYCLE_FAILED, "failed"),
            OUTCOME_WAITING: (LIFECYCLE_WAITING, "pending_approval"),
        }
        for expected, (lifecycle, status) in cases.items():
            with self.subTest(expected):
                result = self.classify(
                    lifecycle,
                    status=status,
                    driver_status="abandoned",
                    driver_terminal_reason="heartbeat_timeout",
                    driver_ended_at=_ago(10),
                )
                self.assertEqual(expected, result.outcome)
                self.assertIsNone(result.terminal_reason)

    def test_a_driver_that_ended_normally_says_nothing_about_liveness(self) -> None:
        result = self.classify(
            driver_status="action_selected",
            driver_terminal_reason="start_experiment",
            driver_ended_at=_ago(10),
            updated_at=_ago(1),
        )
        self.assertEqual(OUTCOME_UNKNOWN, result.outcome)


class SessionOutcomeTests(DiagnosticsFixture):
    """Controller sessions, driven through the real #162 store methods."""

    def store(self) -> NexusTaskStore:
        store = NexusTaskStore(db_path=self.db_path)
        self.addCleanup(store.close)
        return store

    def claimed(self, store: NexusTaskStore) -> str:
        session = store.get_or_create(source_type="task", source_id=os.urandom(8).hex())
        store.claim_session(session.id, worker_id="vm:1:aaaa")
        return session.id

    def test_clean_finish(self) -> None:
        store = self.store()
        session_id = self.claimed(store)
        store.finalize_turn(
            session_id,
            worker_id="vm:1:aaaa",
            status="action_selected",
            reason="start_experiment",
            event_kind="start_experiment",
            event_content="Starting an experiment",
        )
        outcome = self.diagnostics().status(session_id).outcome
        self.assertEqual(OUTCOME_SUCCEEDED, outcome.outcome)
        self.assertEqual("start_experiment", outcome.last_event["kind"])

    def test_failed_turn_quotes_the_recorded_exception(self) -> None:
        store = self.store()
        session_id = self.claimed(store)
        store.finalize_turn(
            session_id,
            worker_id="vm:1:aaaa",
            status="failed",
            reason=TERMINAL_REASON_TURN_FAILED,
            event_role="system",
            event_kind=TERMINAL_REASON_TURN_FAILED,
            event_content="Controller turn ended without a decision: RuntimeError: boom",
        )
        outcome = self.diagnostics().status(session_id).outcome
        self.assertEqual(OUTCOME_FAILED, outcome.outcome)
        self.assertIn("RuntimeError: boom", outcome.probable_cause)

    def test_cancelled_turn(self) -> None:
        store = self.store()
        session_id = self.claimed(store)
        store.finalize_turn(
            session_id,
            worker_id="vm:1:aaaa",
            status="failed",
            reason=TERMINAL_REASON_TURN_CANCELLED,
            event_role="system",
            event_kind=TERMINAL_REASON_TURN_CANCELLED,
            event_content="Controller turn ended without a decision: cancelled",
        )
        outcome = self.diagnostics().status(session_id).outcome
        self.assertEqual(OUTCOME_CANCELLED, outcome.outcome)

    def test_reconciled_stale_claim_reports_last_healthy_heartbeat(self) -> None:
        store = self.store()
        session_id = self.claimed(store)
        store.append_event(session_id, role="assistant", kind="tool_call", content="git push")
        old = _iso(30)
        self.conn.execute(
            "UPDATE nexus_task_sessions SET last_heartbeat_at=? WHERE id=?", (old, session_id)
        )
        self.conn.commit()
        self.assertEqual(1, len(store.reconcile_stale_heartbeats(60)))

        state = self.diagnostics().status(session_id)
        self.assertEqual(OUTCOME_STALE, state.outcome.outcome)
        self.assertEqual(old, state.outcome.last_healthy_heartbeat_at)
        self.assertEqual("heartbeat_timeout", state.outcome.terminal_reason)
        # The reaper's own journal entry is not the worker's last step.
        self.assertEqual("tool_call", state.outcome.last_event["kind"])

    def test_live_claim_is_running(self) -> None:
        session_id = self.add_session(source_id="t-live", heartbeat_minutes_ago=0.2)
        outcome = self.diagnostics(stale_after_seconds=900).status(session_id).outcome
        self.assertEqual(OUTCOME_RUNNING, outcome.outcome)
        self.assertEqual(EVIDENCE_HEARTBEAT, outcome.evidence)

    def test_unreconciled_silent_claim_is_stale(self) -> None:
        session_id = self.add_session(source_id="t-dead", heartbeat_minutes_ago=45)
        outcome = self.diagnostics(stale_after_seconds=900).status(session_id).outcome
        self.assertEqual(OUTCOME_STALE, outcome.outcome)


class TaskAndExperimentOutcomeTests(DiagnosticsFixture):
    def test_experiment_interrupted_by_restart(self) -> None:
        experiment_id = self.add_experiment(
            status="interrupted", error="interrupted by service restart", ended_at=_iso(5)
        )
        task_id = self.add_task(status="running", experiment_id=experiment_id)
        diagnostics = self.diagnostics()
        for reference in (experiment_id, task_id):
            with self.subTest(reference=reference):
                outcome = diagnostics.status(reference).outcome
                self.assertEqual(OUTCOME_INTERRUPTED, outcome.outcome)
                self.assertEqual(RETRY_POSSIBLE, outcome.retry)
        # An interrupted run is exactly what `last-failure` exists to find.
        report = diagnostics.last_failure()
        self.assertEqual(task_id, report.lifecycle.subject_id)

    def reconcile(self, session_id: str, heartbeat_minutes_ago: float = 30) -> str:
        """Expire a claim and reconcile it through the real #162 store path."""
        store = NexusTaskStore(db_path=self.db_path)
        self.addCleanup(store.close)
        old = _iso(heartbeat_minutes_ago)
        self.conn.execute(
            "UPDATE nexus_task_sessions SET status='active', last_heartbeat_at=? WHERE id=?",
            (old, session_id),
        )
        self.conn.commit()
        self.assertEqual(
            [session_id], [session.id for session in store.reconcile_stale_heartbeats(60)]
        )
        return old

    def assert_conclusively_stale(self, state, heartbeat: str) -> None:
        self.assertEqual(OUTCOME_STALE, state.outcome.outcome)
        self.assertTrue(state.outcome.conclusive)
        self.assertEqual(EVIDENCE_HEARTBEAT, state.outcome.evidence)
        self.assertEqual(heartbeat, state.outcome.last_healthy_heartbeat_at)
        self.assertEqual("heartbeat_timeout", state.outcome.terminal_reason)
        self.assertEqual(RETRY_POSSIBLE, state.outcome.retry)
        self.assertEqual("interrupted", state.progress.state)

    def test_running_task_inherits_its_reconciled_session(self) -> None:
        """The review finding on PR #405: this used to read as ``unknown``."""
        task_id = self.add_task(status="running")
        heartbeat = self.reconcile(self.add_session(source_id=task_id))
        self.assert_conclusively_stale(self.diagnostics().status(task_id), heartbeat)

    def test_older_logs_do_not_turn_a_reconciled_task_into_running(self) -> None:
        experiment_id = self.add_experiment(status="implementing")
        task_id = self.add_task(status="running", experiment_id=experiment_id)
        self.write_log(
            f"{experiment_id}.jsonl",
            [self.log_entry(minutes_ago=2, event="branch_created", message="branch ok")],
        )
        heartbeat = self.reconcile(self.add_session(source_id=task_id))
        state = self.diagnostics(stale_after_seconds=900).status(task_id)
        self.assert_conclusively_stale(state, heartbeat)
        self.assertEqual("branch_created", state.outcome.last_event["kind"])

    def test_logs_after_the_reconciliation_leave_the_task_unknown(self) -> None:
        experiment_id = self.add_experiment(status="implementing")
        task_id = self.add_task(status="running", experiment_id=experiment_id)
        self.reconcile(self.add_session(source_id=task_id))
        self.write_log(
            f"{experiment_id}.jsonl",
            [self.log_entry(minutes_ago=-0.1, event="tests_started", message="still going")],
        )
        outcome = self.diagnostics(stale_after_seconds=900).status(task_id).outcome
        self.assertEqual(OUTCOME_UNKNOWN, outcome.outcome)
        self.assertFalse(outcome.conclusive)

    def test_experiment_finds_the_reconciled_session_through_its_task(self) -> None:
        experiment_id = self.add_experiment(status="implementing")
        task_id = self.add_task(status="running", experiment_id=experiment_id)
        heartbeat = self.reconcile(self.add_session(source_id=task_id))
        self.assert_conclusively_stale(self.diagnostics().status(experiment_id), heartbeat)

    def test_logs_after_reconciliation_leave_the_experiment_unknown(self) -> None:
        """Fresh experiment logs cannot silently erase conflicting durable evidence."""
        experiment_id = self.add_experiment(status="implementing")
        task_id = self.add_task(status="running", experiment_id=experiment_id)
        self.reconcile(self.add_session(source_id=task_id))
        self.write_log(
            f"{experiment_id}.jsonl",
            [self.log_entry(minutes_ago=-0.1, event="tests_started", message="still going")],
        )

        outcome = self.diagnostics(stale_after_seconds=900).status(experiment_id).outcome

        self.assertEqual(OUTCOME_UNKNOWN, outcome.outcome)
        self.assertFalse(outcome.conclusive)
        self.assertTrue(any("logs continue" in gap for gap in outcome.evidence_gaps))

    def test_experiment_finds_the_session_waiting_on_it(self) -> None:
        experiment_id = self.add_experiment(status="implementing")
        session_id = self.add_session(source_id="some-idea")
        self.conn.execute(
            "UPDATE nexus_task_sessions SET waiting_experiment_id=? WHERE id=?",
            (experiment_id, session_id),
        )
        self.conn.commit()
        heartbeat = self.reconcile(session_id)
        self.assert_conclusively_stale(self.diagnostics().status(experiment_id), heartbeat)

    def test_a_finished_experiment_keeps_its_outcome_after_its_session_died(self) -> None:
        experiment_id = self.add_experiment(status="succeeded", ended_at=_iso(40))
        task_id = self.add_task(status="succeeded", experiment_id=experiment_id)
        self.reconcile(self.add_session(source_id=task_id))
        self.assertEqual(OUTCOME_SUCCEEDED, self.diagnostics().status(task_id).outcome.outcome)

    def test_a_reconciled_task_is_found_by_last_failure_and_rendered(self) -> None:
        task_id = self.add_task(status="running")
        heartbeat = self.reconcile(self.add_session(source_id=task_id))
        report = self.diagnostics().last_failure()
        self.assertEqual(task_id, report.lifecycle.subject_id)
        lines = render_report(report)
        self.assertIn("outcome: stale", lines)
        self.assertIn(f"last_healthy_heartbeat_at: {heartbeat}", lines)
        context = _with_lifecycle_context({}, report.lifecycle)
        self.assertEqual("stale", context["nexus_outcome"])
        self.assertEqual(heartbeat, context["nexus_last_healthy_heartbeat_at"])

    def test_cancelled_experiment(self) -> None:
        experiment_id = self.add_experiment(
            status="cancelled", error="cancelled by api", ended_at=_iso(5)
        )
        outcome = self.diagnostics().status(experiment_id).outcome
        self.assertEqual(OUTCOME_CANCELLED, outcome.outcome)

    def test_running_task_with_quiet_logs_names_its_last_step(self) -> None:
        experiment_id = self.add_experiment(status="implementing")
        task_id = self.add_task(status="running", experiment_id=experiment_id)
        self.write_log(
            f"{experiment_id}.jsonl",
            [
                self.log_entry(minutes_ago=50, event="branch_created", message="branch ok"),
                self.log_entry(minutes_ago=48, event="test_progress", message="tick"),
            ],
        )
        outcome = self.diagnostics(stale_after_seconds=900).status(task_id).outcome
        self.assertEqual(OUTCOME_STALE, outcome.outcome)
        self.assertFalse(outcome.conclusive)  # no heartbeat covers this phase
        self.assertEqual(RETRY_INSUFFICIENT_EVIDENCE, outcome.retry)
        # The liveness tick is not the last meaningful step.
        self.assertEqual("branch_created", outcome.last_event["kind"])

    def test_the_outcome_reaches_render_and_failure_analysis(self) -> None:
        experiment_id = self.add_experiment(
            status="failed", error="tests failed", ended_at=_iso(5)
        )
        diagnostics = self.diagnostics()
        report = diagnostics.status_report(experiment_id)
        lines = render_report(report)
        self.assertIn("outcome: failed", lines)
        self.assertIn("probable_cause: tests failed", lines)
        context = _with_lifecycle_context({}, report.lifecycle)
        self.assertEqual("failed", context["nexus_outcome"])
        self.assertEqual("possible", context["nexus_retry_hint"])


class OutcomeApiTests(DiagnosticsFixture):
    def test_status_route_returns_the_outcome(self) -> None:
        diagnostics = NexusDiagnostics(db_path=self.db_path, log_dir=self.log_dir)
        self.addCleanup(diagnostics.close)
        api.app.dependency_overrides[api.get_nexus_diagnostics] = lambda: diagnostics
        self.addCleanup(api.app.dependency_overrides.clear)
        experiment_id = self.add_experiment(status="succeeded", ended_at=_iso(1))

        response = TestClient(api.app).get(
            f"/diagnostics/status/{experiment_id}", headers={"x-api-key": api.API_KEY}
        )
        self.assertEqual(200, response.status_code)
        outcome = response.json()["lifecycle"]["outcome"]
        self.assertEqual("succeeded", outcome["outcome"])
        self.assertTrue(outcome["conclusive"])
        self.assertEqual("not_needed", outcome["retry"])

    def test_status_route_reports_a_reconciled_session_on_its_task(self) -> None:
        diagnostics = NexusDiagnostics(db_path=self.db_path, log_dir=self.log_dir)
        self.addCleanup(diagnostics.close)
        api.app.dependency_overrides[api.get_nexus_diagnostics] = lambda: diagnostics
        self.addCleanup(api.app.dependency_overrides.clear)
        task_id = self.add_task(status="running")
        session_id = self.add_session(source_id=task_id, heartbeat_minutes_ago=30)
        store = NexusTaskStore(db_path=self.db_path)
        self.addCleanup(store.close)
        self.assertEqual(1, len(store.reconcile_stale_heartbeats(60)))

        response = TestClient(api.app).get(
            f"/diagnostics/status/{task_id}", headers={"x-api-key": api.API_KEY}
        )
        self.assertEqual(200, response.status_code)
        lifecycle = response.json()["lifecycle"]
        self.assertEqual("stale", lifecycle["outcome"]["outcome"])
        self.assertTrue(lifecycle["outcome"]["conclusive"])
        self.assertEqual(session_id, lifecycle["correlation_ids"]["session_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
