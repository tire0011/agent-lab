"""One diagnostic backend answers "what happened to this work?" (#188).

Diagnosing a Nexus failure used to mean composing ad-hoc shell commands
through the mailbox. These tests pin the replacement: a reference an operator
plausibly has - a mailbox task id, an experiment id, an issue number, an inbox
filename - resolves to one bounded answer carrying lifecycle state, branch,
commit, PR, worker, last progress and last error.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules import central_logging
from modules.nexus_diagnostics import (
    LIFECYCLE_FAILED,
    LIFECYCLE_MERGED,
    LIFECYCLE_RUNNING,
    LIFECYCLE_SUCCEEDED,
    LIFECYCLE_WAITING,
    PROGRESS_COMPLETED,
    PROGRESS_INTERRUPTED,
    PROGRESS_RUNNING,
    PROGRESS_SOURCE_HEARTBEAT,
    PROGRESS_SOURCE_LOG,
    DiagnosticsError,
    NexusDiagnostics,
    assess_progress,
    render_report,
    resolve_service,
)
from modules.run_store import init_db


def _iso(minutes_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class DiagnosticsFixture(unittest.TestCase):
    """A database and log directory shaped like a real Nexus deployment."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = str(self.root / "agent.db")
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()
        init_db(self.db_path).close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    def diagnostics(self, **kwargs) -> NexusDiagnostics:
        diagnostics = NexusDiagnostics(
            db_path=self.db_path, log_dir=self.log_dir, **kwargs
        )
        self.addCleanup(diagnostics.close)
        return diagnostics

    # -- fixture builders -------------------------------------------------

    def add_task(
        self,
        *,
        status: str = "running",
        experiment_id: str | None = None,
        error: str | None = None,
        file_path: str = "inbox/2026-01-01-task.md",
        updated_minutes_ago: float = 1.0,
    ) -> str:
        task_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, "
            "created_by, status, received_at, processed_at) "
            "VALUES (?, 'deadbeef', 'task', 'chatgpt', 'processed', ?, ?)",
            (file_path, _iso(60), _iso(60)),
        )
        msg_id = self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        self.conn.execute(
            """INSERT INTO mailbox_task_approvals
                 (id, mailbox_msg_id, task_body, sender, file_path, received_at,
                  status, experiment_id, error, created_at, updated_at)
               VALUES (?, ?, ?, 'chatgpt', ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                msg_id,
                "Implement the thing",
                file_path,
                _iso(60),
                status,
                experiment_id,
                error,
                _iso(60),
                _iso(updated_minutes_ago),
            ),
        )
        self.conn.commit()
        return task_id

    def add_experiment(
        self,
        *,
        status: str = "implementing",
        branch: str | None = None,
        error: str | None = None,
        ended_at: str | None = None,
        created_minutes_ago: float = 60.0,
    ) -> str:
        experiment_id = uuid.uuid4().hex
        self.conn.execute(
            """INSERT INTO experiments (id, goal, status, created_at, branch_name,
                                        error, ended_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                experiment_id,
                "Solve issue #188",
                status,
                _iso(created_minutes_ago),
                branch,
                error,
                ended_at,
            ),
        )
        self.conn.commit()
        return experiment_id

    def link_github(
        self,
        *,
        experiment_id: str | None = None,
        source_id: str | None = None,
        issue_number: int = 188,
        branch: str = "nexus/issue-188",
        pr_number: int | None = None,
    ) -> None:
        self._ensure_github_schema()
        self.conn.execute(
            """INSERT INTO github_work_links
                 (source_type, source_id, experiment_id, issue_number, issue_title,
                  branch_name, pr_number, created_at, updated_at)
               VALUES ('task', ?, ?, ?, 'Add diagnostics', ?, ?, ?, ?)""",
            (source_id, experiment_id, issue_number, branch, pr_number, _iso(60), _iso(5)),
        )
        self.conn.commit()

    def _ensure_github_schema(self) -> None:
        """Create the GitHub link tables the way production does.

        They are owned by ``GitHubWorkTrackingService`` and created lazily by
        it, so letting the real service build them is what keeps this fixture
        from drifting away from the schema the backend actually reads.
        """
        from modules.github_work_tracking import GitHubWorkTrackingService

        service = GitHubWorkTrackingService(db_path=self.db_path, repo_root=str(self.root))
        service.close()

    def add_adoption(self, experiment_id: str, *, status: str, commit: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO experiment_adoptions
                 (id, experiment_id, status, target_branch, integrated_commit_sha,
                  created_at, updated_at)
               VALUES (?, ?, ?, 'main', ?, ?, ?)""",
            (uuid.uuid4().hex, experiment_id, status, commit, _iso(30), _iso(5)),
        )
        self.conn.commit()

    def add_session(
        self,
        *,
        source_id: str,
        status: str = "active",
        worker_id: str = "vm:1234:abcd",
        heartbeat_minutes_ago: float | None = 1.0,
    ) -> str:
        from modules.nexus_task_store import NexusTaskStore

        store = NexusTaskStore(db_path=self.db_path)
        self.addCleanup(store.close)
        session = store.get_or_create(source_type="task", source_id=source_id)
        store.claim_session(session.id, worker_id=worker_id)
        heartbeat = None if heartbeat_minutes_ago is None else _iso(heartbeat_minutes_ago)
        self.conn.execute(
            "UPDATE nexus_task_sessions SET status=?, last_heartbeat_at=? WHERE id=?",
            (status, heartbeat, session.id),
        )
        self.conn.commit()
        return session.id

    def write_log(self, name: str, entries: list[dict]) -> None:
        path = self.log_dir / name
        with path.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def log_entry(self, *, minutes_ago: float, event: str, message: str, **extra) -> dict:
        stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return {
            "timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": "INFO",
            "logger": "nexus.test",
            "phase": "implementation",
            "event": event,
            "message": message,
            **extra,
        }


class ProgressVerdictTests(unittest.TestCase):
    """The heartbeat rule, extended to work that has no heartbeat of its own."""

    def test_recent_heartbeat_is_running(self) -> None:
        verdict = assess_progress(
            LIFECYCLE_RUNNING, last_heartbeat_at=_iso(0.5), stale_after_seconds=900
        )
        self.assertEqual(PROGRESS_RUNNING, verdict.state)
        self.assertEqual(PROGRESS_SOURCE_HEARTBEAT, verdict.source)

    def test_silence_past_the_timeout_is_interrupted(self) -> None:
        verdict = assess_progress(
            LIFECYCLE_RUNNING, last_heartbeat_at=_iso(120), stale_after_seconds=900
        )
        self.assertEqual(PROGRESS_INTERRUPTED, verdict.state)
        self.assertIn("stale timeout", verdict.reason)

    def test_log_entry_stands_in_for_a_missing_heartbeat(self) -> None:
        """A mailbox task has no heartbeat; its newest log line is the signal."""
        verdict = assess_progress(
            LIFECYCLE_RUNNING,
            last_heartbeat_at=None,
            last_log_at=_iso(0.5),
            updated_at=_iso(300),
            stale_after_seconds=900,
        )
        self.assertEqual(PROGRESS_RUNNING, verdict.state)
        self.assertEqual(PROGRESS_SOURCE_LOG, verdict.source)

    def test_heartbeat_wins_over_an_older_log_entry(self) -> None:
        verdict = assess_progress(
            LIFECYCLE_RUNNING,
            last_heartbeat_at=_iso(0.2),
            last_log_at=_iso(30),
            stale_after_seconds=900,
        )
        self.assertEqual(PROGRESS_SOURCE_HEARTBEAT, verdict.source)
        self.assertEqual(PROGRESS_RUNNING, verdict.state)

    def test_a_newer_log_entry_wins_over_a_stale_heartbeat(self) -> None:
        """Ordering is by instant, not by ISO spelling: the three sources use
        three different formats and a string compare gets close calls wrong."""
        verdict = assess_progress(
            LIFECYCLE_RUNNING,
            last_heartbeat_at=_iso(30),
            last_log_at=_iso(0.2),
            stale_after_seconds=900,
        )
        self.assertEqual(PROGRESS_SOURCE_LOG, verdict.source)
        self.assertEqual(PROGRESS_RUNNING, verdict.state)

    def test_terminal_work_is_completed_not_interrupted(self) -> None:
        verdict = assess_progress(
            LIFECYCLE_SUCCEEDED, last_heartbeat_at=_iso(5000), stale_after_seconds=900
        )
        self.assertEqual(PROGRESS_COMPLETED, verdict.state)

    def test_resting_work_is_never_called_interrupted(self) -> None:
        """A task awaiting approval has no worker by design (#162)."""
        verdict = assess_progress(
            LIFECYCLE_WAITING, updated_at=_iso(5000), stale_after_seconds=900
        )
        self.assertEqual(PROGRESS_RUNNING, verdict.state)

    def test_a_tiny_timeout_cannot_declare_live_work_dead(self) -> None:
        """The clamped floor of modules.heartbeat applies here too."""
        verdict = assess_progress(
            LIFECYCLE_RUNNING, last_heartbeat_at=_iso(0.4), stale_after_seconds=1
        )
        self.assertEqual(PROGRESS_RUNNING, verdict.state)
        self.assertGreaterEqual(verdict.stale_after_seconds, 60)


class ReferenceResolutionTests(DiagnosticsFixture):
    def test_full_ids_resolve_to_their_subsystem(self) -> None:
        task_id = self.add_task()
        experiment_id = self.add_experiment()
        diagnostics = self.diagnostics()
        self.assertEqual(("task", task_id), diagnostics.resolve(task_id))
        self.assertEqual(("experiment", experiment_id), diagnostics.resolve(experiment_id))

    def test_a_unique_prefix_is_enough(self) -> None:
        experiment_id = self.add_experiment()
        diagnostics = self.diagnostics()
        self.assertEqual(
            ("experiment", experiment_id), diagnostics.resolve(experiment_id[:8])
        )

    def test_issue_reference_resolves_through_the_github_link(self) -> None:
        experiment_id = self.add_experiment()
        self.link_github(experiment_id=experiment_id, issue_number=186)
        diagnostics = self.diagnostics()
        self.assertEqual(("experiment", experiment_id), diagnostics.resolve("#186"))
        self.assertEqual(("experiment", experiment_id), diagnostics.resolve("186"))
        self.assertEqual(("experiment", experiment_id), diagnostics.resolve("issue 186"))

    def test_pull_request_reference_resolves_too(self) -> None:
        experiment_id = self.add_experiment()
        self.link_github(experiment_id=experiment_id, issue_number=186, pr_number=191)
        diagnostics = self.diagnostics()
        self.assertEqual(("experiment", experiment_id), diagnostics.resolve("PR #191"))

    def test_inbox_filename_resolves_to_its_task(self) -> None:
        task_id = self.add_task(file_path="inbox/2026-09-15-implement-188.md")
        diagnostics = self.diagnostics()
        self.assertEqual(
            ("task", task_id), diagnostics.resolve("2026-09-15-implement-188.md")
        )

    def test_a_bare_number_falls_through_to_id_resolution(self) -> None:
        """"#188" promises an issue; a bare "188" is only a guess."""
        experiment_id = self.add_experiment()
        self.conn.execute(
            "UPDATE experiments SET id = '188abc' WHERE id = ?", (experiment_id,)
        )
        self.conn.commit()
        diagnostics = self.diagnostics()
        self.assertEqual(("experiment", "188abc"), diagnostics.resolve("188"))
        with self.assertRaises(DiagnosticsError):
            diagnostics.resolve("#188")

    def test_an_underscore_in_a_prefix_is_not_a_wildcard(self) -> None:
        """'_' is a legal id character and a LIKE wildcard; it must not match
        any other character, or one operator's shorthand answers for another
        task entirely."""
        for identifier in ("ab_cd1111", "abXcd2222"):
            self.conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at) "
                "VALUES (?, 'goal', 'queued', ?)",
                (identifier, _iso(10)),
            )
        self.conn.commit()
        self.assertEqual(("experiment", "ab_cd1111"), self.diagnostics().resolve("ab_cd"))

    def test_an_ambiguous_prefix_is_refused_rather_than_guessed(self) -> None:
        for identifier in ("dupe1111", "dupe2222"):
            self.conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at) "
                "VALUES (?, 'goal', 'queued', ?)",
                (identifier, _iso(10)),
            )
        self.conn.commit()
        with self.assertRaises(DiagnosticsError):
            self.diagnostics().resolve("dupe")

    def test_an_unknown_reference_says_so(self) -> None:
        diagnostics = self.diagnostics()
        with self.assertRaises(DiagnosticsError):
            diagnostics.resolve("no-such-thing")
        with self.assertRaises(DiagnosticsError):
            diagnostics.resolve("")


class TaskDiagnosticsTests(DiagnosticsFixture):
    def test_one_call_returns_lifecycle_and_correlated_logs(self) -> None:
        experiment_id = self.add_experiment(status="implementing", branch="nexus/issue-188")
        task_id = self.add_task(status="running", experiment_id=experiment_id)
        self.link_github(experiment_id=experiment_id, source_id=task_id, issue_number=188)
        self.add_session(source_id=task_id, heartbeat_minutes_ago=0.5)
        self.write_log(
            f"{central_logging.TASK_LOG_PREFIX}{task_id}.jsonl",
            [self.log_entry(minutes_ago=10, event="task_approved", message="approved")],
        )
        self.write_log(
            f"{experiment_id}.jsonl",
            [self.log_entry(minutes_ago=2, event="branch_created", message="branch ready")],
        )

        report = self.diagnostics().diagnose(task_id)

        self.assertEqual(LIFECYCLE_RUNNING, report.lifecycle.lifecycle)
        self.assertEqual("nexus/issue-188", report.lifecycle.branch)
        self.assertEqual(188, report.lifecycle.issue_number)
        self.assertEqual("vm:1234:abcd", report.lifecycle.worker_id)
        self.assertEqual(PROGRESS_RUNNING, report.lifecycle.progress.state)
        # Both logs, merged chronologically - the whole task path, not just
        # whichever id the caller happened to hold.
        self.assertEqual(
            ["task_approved", "branch_created"],
            [entry["event"] for entry in report.log_entries],
        )
        self.assertEqual(2, len(report.log_sources))

    def test_log_output_is_bounded_by_default_and_reports_truncation(self) -> None:
        task_id = self.add_task()
        self.write_log(
            f"{central_logging.TASK_LOG_PREFIX}{task_id}.jsonl",
            [
                self.log_entry(minutes_ago=100 - index, event="step", message=f"line {index}")
                for index in range(100)
            ],
        )
        report = self.diagnostics().diagnose(task_id)
        self.assertEqual(50, len(report.log_entries))
        self.assertTrue(report.truncated)
        # Bounded by default, with an explicit way to ask for more.
        wider = self.diagnostics().diagnose(task_id, lines=100)
        self.assertEqual(100, len(wider.log_entries))
        self.assertFalse(wider.truncated)

    def test_a_task_awaiting_approval_is_waiting_not_stalled(self) -> None:
        task_id = self.add_task(status="pending_approval", updated_minutes_ago=600)
        state = self.diagnostics().status(task_id)
        self.assertEqual(LIFECYCLE_WAITING, state.lifecycle)
        self.assertEqual(PROGRESS_RUNNING, state.progress.state)

    def test_approval_is_not_recorded_as_an_ending(self) -> None:
        """A task ends when its experiment does, not when it was approved."""
        experiment_id = self.add_experiment(status="succeeded", ended_at=_iso(20))
        task_id = self.add_task(status="succeeded", experiment_id=experiment_id)
        state = self.diagnostics().status(task_id)
        self.assertIsNotNone(state.ended_at)
        self.assertEqual(
            state.related["experiment"]["ended_at"], state.ended_at
        )

    def test_a_merged_task_reports_merged_and_its_commit(self) -> None:
        experiment_id = self.add_experiment(status="succeeded", ended_at=_iso(20))
        task_id = self.add_task(status="succeeded", experiment_id=experiment_id)
        self.add_adoption(experiment_id, status="adopted", commit="abc1234")
        self.link_github(experiment_id=experiment_id, issue_number=188, pr_number=195)
        state = self.diagnostics().status(task_id)
        self.assertEqual(LIFECYCLE_MERGED, state.lifecycle)
        self.assertEqual("abc1234", state.commit)
        self.assertEqual(195, state.pr_number)
        self.assertEqual(PROGRESS_COMPLETED, state.progress.state)

    def test_the_error_is_carried_up_from_whichever_layer_failed(self) -> None:
        experiment_id = self.add_experiment(
            status="failed", error="pytest exited 1", ended_at=_iso(5)
        )
        task_id = self.add_task(status="failed", experiment_id=experiment_id)
        state = self.diagnostics().status(task_id)
        self.assertEqual(LIFECYCLE_FAILED, state.lifecycle)
        self.assertEqual("pytest exited 1", state.last_error)

    def test_a_missing_log_is_a_note_rather_than_an_error(self) -> None:
        task_id = self.add_task()
        report = self.diagnostics().diagnose(task_id)
        self.assertEqual([], report.log_entries)
        self.assertTrue(any("no structured log entries" in note for note in report.notes))

    def test_rendering_is_stable_key_value_lines(self) -> None:
        experiment_id = self.add_experiment(branch="nexus/issue-188")
        task_id = self.add_task(experiment_id=experiment_id)
        lines = render_report(self.diagnostics().diagnose(task_id))
        rendered = "\n".join(lines)
        for key in ("subject:", "lifecycle:", "progress:", "branch:", "pr:", "worker:"):
            self.assertIn(key, rendered)


class LastFailureTests(DiagnosticsFixture):
    def test_the_latest_explicit_failure_is_found(self) -> None:
        old = self.add_experiment(
            status="failed", error="old failure", ended_at=_iso(600), created_minutes_ago=700
        )
        recent = self.add_experiment(
            status="failed", error="recent failure", ended_at=_iso(5), created_minutes_ago=30
        )
        self.assertTrue(old)
        report = self.diagnostics().last_failure()
        self.assertEqual(recent, report.lifecycle.subject_id)
        self.assertEqual("recent failure", report.lifecycle.last_error)

    def test_a_stalled_experiment_counts_as_a_failure(self) -> None:
        """A process that died mid-run never wrote a `failed` row; the stall
        is the only evidence there is."""
        stalled = self.add_experiment(status="implementing", created_minutes_ago=240)
        report = self.diagnostics().last_failure()
        self.assertEqual(stalled, report.lifecycle.subject_id)
        self.assertEqual(PROGRESS_INTERRUPTED, report.lifecycle.progress.state)
        self.assertTrue(any("most recent failed/stalled" in note for note in report.notes))

    def test_a_task_and_its_experiment_are_reported_once(self) -> None:
        """Both sides of one piece of work are candidates; the task view wins
        because it already carries the experiment's lifecycle and logs."""
        experiment_id = self.add_experiment(
            status="failed", error="boom", ended_at=_iso(5), created_minutes_ago=30
        )
        task_id = self.add_task(
            status="failed", experiment_id=experiment_id, updated_minutes_ago=5
        )
        candidates = self.diagnostics()._failure_candidates()
        self.assertEqual([("task", task_id)], [(kind, sid) for _, kind, sid in candidates])

    def test_healthy_work_alone_yields_an_explicit_answer(self) -> None:
        experiment_id = self.add_experiment(
            status="succeeded", ended_at=_iso(3), created_minutes_ago=10
        )
        self.assertTrue(experiment_id)
        with self.assertRaises(DiagnosticsError):
            self.diagnostics().last_failure()


class ServiceLogTests(DiagnosticsFixture):
    class _Reader:
        def __init__(self, entries: list[dict] | None = None, error: str | None = None) -> None:
            self.entries = entries or []
            self.error = error
            self.calls: list[tuple[str, int]] = []

        async def tail_journal_entries(self, service: str, lines: int, **kwargs):
            self.calls.append((service, lines))
            if self.error:
                raise RuntimeError(self.error)
            return self.entries

    def test_a_friendly_name_maps_onto_the_systemd_unit(self) -> None:
        self.assertEqual("nexus-mailbox", resolve_service("mailbox"))
        self.assertEqual("agent-api", resolve_service("api"))
        self.assertEqual("llama-server", resolve_service("llm"))
        self.assertEqual("agent-api", resolve_service(None))
        self.assertEqual("custom-unit", resolve_service("custom-unit.service"))

    def test_an_unsafe_service_name_never_reaches_journalctl(self) -> None:
        with self.assertRaises(DiagnosticsError):
            resolve_service("agent-api; rm -rf /")

    def test_service_logs_are_read_without_composing_a_command(self) -> None:
        reader = self._Reader(
            [
                {
                    "timestamp": "2026-09-15T10:00:00.000Z",
                    "level": "ERROR",
                    "message": "worker died",
                }
            ]
        )
        diagnostics = self.diagnostics(log_reader=reader)
        result = asyncio.run(diagnostics.service_logs("mailbox", lines=25))
        self.assertEqual("nexus-mailbox", result.service)
        self.assertEqual([("nexus-mailbox", 25)], reader.calls)
        self.assertEqual("worker died", result.entries[0]["message"])

    def test_an_unreadable_journal_is_reported_not_raised(self) -> None:
        diagnostics = self.diagnostics(log_reader=self._Reader(error="journalctl missing"))
        result = asyncio.run(diagnostics.service_logs("api"))
        self.assertIn("journalctl missing", result.error or "")
        self.assertEqual([], result.entries)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
