"""#811: every experiment that produces no publishable changes gets a recorded,
countable cause (column + log event + error token), and the most frequent cause
- the agent writing outside its worktree - is prevented by scrubbing the
deployed checkout's path from the prompt (see tests/test_checkout_guard.py)."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules import no_changes_cause as ncc
from modules.central_logging import ExperimentLogger
from modules.checkout_guard import EVENT as OUTSIDE_EVENT
from modules.codex_worker import CodexResult
from modules.experiment_service import (
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_TIMED_OUT,
    ExperimentService,
)
from modules.github_work_tracking import GitHubWorkTrackingError
from modules.run_store import init_db
from modules.weekly_review import starter_pack

NOW = "2026-11-23T10:00:00+00:00"


def _result(**overrides) -> CodexResult:
    defaults = dict(
        success=True, exit_code=0, stdout="", stderr="", runtime_ms=10,
        timed_out=False, stdout_truncated=False, stderr_truncated=False,
        thread_id="tid-1", final_message="done", usage=None, task="task",
        working_dir="/wt", sandbox="workspace-write", error=None,
    )
    defaults.update(overrides)
    return CodexResult(**defaults)


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "a@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "module.py").write_text("'''Doc.'''\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_placeholder.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def _write_log(log_dir: Path, experiment_id: str, entries: list[dict]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{experiment_id}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        for entry in entries:
            base = {"timestamp": NOW, "level": "ERROR", "logger": "test",
                    "phase": "implementation", "event": "x", "experiment_id": experiment_id}
            base.update(entry)
            fh.write(json.dumps(base) + "\n")


def _add_experiment(conn, experiment_id: str, *, status: str, error: str | None = None,
                    diff_text: str | None = None, cause: str | None = None,
                    created_at: str = NOW) -> None:
    conn.execute(
        "INSERT INTO experiments (id, goal, status, created_at, error, diff_text, no_changes_cause) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (experiment_id, "goal", status, created_at, error, diff_text, cause),
    )
    conn.commit()


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        self.conn = init_db(self.db_path)
        self.conn.row_factory = None
        self.addCleanup(self.conn.close)
        self.log_dir = Path(self.tmp.name) / "logs"

    def classify(self, experiment_id: str, **kwargs) -> str:
        return ncc.classify_no_changes_cause(self.conn, experiment_id, self.log_dir, **kwargs)

    def test_outside_worktree_from_the_guard_event(self):
        _add_experiment(self.conn, "e1", status="failed",
                        error="coding agent mistral wrote outside its worktree into /agent: x.py")
        _write_log(self.log_dir, "e1", [{"event": OUTSIDE_EVENT, "message": "wrote outside"}])
        self.assertEqual(self.classify("e1"), ncc.CAUSE_OUTSIDE_WORKTREE)

    def test_outside_worktree_from_the_error_text_alone(self):
        _add_experiment(self.conn, "e1", status="failed",
                        error="coding agent vibe wrote outside its worktree into /agent: a.py, b.py")
        self.assertEqual(self.classify("e1"), ncc.CAUSE_OUTSIDE_WORKTREE)

    def test_timed_out_status_is_a_limit_or_timeout(self):
        _add_experiment(self.conn, "e1", status="timed_out",
                        error="implementation phase timed out; GitHub checkpoint failed: "
                              "implementation produced no publishable source/test/documentation changes")
        self.assertEqual(self.classify("e1"), ncc.CAUSE_LIMIT_OR_TIMEOUT)

    def test_watchdog_stall_and_provider_limit_are_limit_or_timeout(self):
        _add_experiment(self.conn, "e1", status="failed",
                        error="Vibe stalled: no output for 1800s; GitHub checkpoint failed: "
                              "implementation produced no publishable changes")
        self.assertEqual(self.classify("e1"), ncc.CAUSE_LIMIT_OR_TIMEOUT)
        _add_experiment(self.conn, "e2", status="failed",
                        error="You've hit your weekly limit · resets 12pm (UTC)")
        self.assertEqual(self.classify("e2"), ncc.CAUSE_LIMIT_OR_TIMEOUT)
        _add_experiment(self.conn, "e3", status="failed", error="http_402 payment required")
        self.assertEqual(self.classify("e3"), ncc.CAUSE_LIMIT_OR_TIMEOUT)

    def test_a_test_failure_quoting_a_limit_word_is_not_a_provider_limit(self):
        _add_experiment(self.conn, "e1", status="failed",
                        error="tests failed (exit code 1): tests/test_rate_limit_resets.py")
        self.assertEqual(self.classify("e1"), ncc.CAUSE_OTHER)

    def test_a_clean_run_that_only_answered_is_answered_without_edits(self):
        _add_experiment(self.conn, "e1", status="needs_review",
                        error="GitHub checkpoint failed before verification: implementation "
                              "produced no publishable source/test/documentation changes")
        self.assertEqual(self.classify("e1"), ncc.CAUSE_NO_EDITS)

    def test_the_agent_finding_the_work_already_on_main(self):
        _add_experiment(self.conn, "e1", status="needs_review",
                        error="GitHub checkpoint failed before verification: implementation "
                              "produced no publishable source/test/documentation changes")
        _write_log(self.log_dir, "e1", [
            {"event": "vibe_call_completed", "level": "INFO",
             "stdout": "The change is already implemented on main, nothing to do."},
        ])
        self.assertEqual(self.classify("e1"), ncc.CAUSE_ALREADY_ON_MAIN)

    def test_a_worker_crash_is_other(self):
        _add_experiment(self.conn, "e1", status="failed",
                        error="Claude produced no parseable result object; GitHub checkpoint "
                              "failed: implementation produced no publishable changes")
        self.assertEqual(self.classify("e1"), ncc.CAUSE_OTHER)

    def test_extra_error_supplies_the_not_yet_persisted_worker_error(self):
        # The pipeline classifies interrupted runs before the terminal status is
        # written, so the worker's error arrives as extra_error.
        _add_experiment(self.conn, "e1", status="implementing", error=None)
        self.assertEqual(
            self.classify("e1", extra_error="Vibe reached the absolute cap of 10800s"),
            ncc.CAUSE_LIMIT_OR_TIMEOUT,
        )
        self.assertEqual(
            self.classify("e1", extra_error="coding agent x wrote outside its worktree into /agent: f"),
            ncc.CAUSE_OUTSIDE_WORKTREE,
        )

    def test_a_recorded_cause_wins_so_backfill_is_idempotent(self):
        _add_experiment(self.conn, "e1", status="needs_review", cause=ncc.CAUSE_NO_EDITS,
                        error="GitHub checkpoint failed before verification: implementation "
                              "produced no publishable changes")
        self.assertEqual(self.classify("e1"), ncc.CAUSE_NO_EDITS)
        self.assertEqual(
            ncc.classify_no_changes_cause(self.conn, "e1", self.log_dir, use_recorded=False),
            ncc.CAUSE_NO_EDITS,
        )

    def test_unknown_experiment_is_other(self):
        self.assertEqual(self.classify("nope"), ncc.CAUSE_OTHER)

    def test_strip_checkpoint_error_removes_the_wrapping(self):
        self.assertEqual(
            ncc._strip_checkpoint_error(
                "GitHub checkpoint failed before verification: implementation produced "
                "no publishable source/test/documentation changes; excluded runtime paths: state/x"
            ),
            "",
        )
        self.assertEqual(
            ncc._strip_checkpoint_error(
                "worker exploded; GitHub checkpoint failed: implementation produced "
                "no publishable source/test/documentation changes"
            ),
            "worker exploded",
        )


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        self.conn = init_db(self.db_path)
        self.addCleanup(self.conn.close)
        self.log_dir = Path(self.tmp.name) / "logs"

    def test_the_last_no_change_runs_are_classified_with_counts_and_written_back(self):
        marker = ("implementation produced no publishable source/test/documentation changes")
        rows = [
            ("a1", "needs_review", "GitHub checkpoint failed before verification: " + marker, None),
            ("a2", "needs_review", "GitHub checkpoint failed before verification: " + marker, None),
            ("t1", "timed_out", "implementation phase timed out (Vibe stalled: no output for "
                                "1800s); GitHub checkpoint failed: " + marker, None),
            ("o1", "failed", "coding agent mistral wrote outside its worktree into /agent: x.py; "
                             "GitHub checkpoint failed: " + marker, None),
            ("r1", "rejected", "goal rejected by policy: ...", None),  # policy rejection: not a no-change run
            ("s1", "succeeded", None, "diff --git a/x b/x"),
            # Runs that stopped before the checkpoint/verification must NOT count as
            # no-change runs: diff_text is only written at the end of _verify, so an
            # empty diff alone would wrongly include them (review of f0195c70).
            ("c1", "failed", "unhandled exception in experiment pipeline: boom", None),
            ("i1", "interrupted", "experiment interrupted by service restart", None),
            ("x1", "cancelled", None, None),
            ("u1", "failed", "Claude produced no parseable result object", None),  # no GitHub tracking
            ("g1", "needs_review", "no safe test command could be determined for this worktree", ""),
        ]
        for exp_id, status, error, diff in rows:
            _add_experiment(self.conn, exp_id, status=status, error=error, diff_text=diff)
        _write_log(self.log_dir, "o1", [{"event": OUTSIDE_EVENT, "message": "wrote outside"}])
        _write_log(self.log_dir, "a1", [{"event": "vibe_call_completed", "message": "done"}])

        counts = ncc.classify_recent_no_change_runs(self.db_path, log_dir=self.log_dir, limit=20)

        self.assertEqual(counts, {
            ncc.CAUSE_NO_EDITS: 2,
            ncc.CAUSE_LIMIT_OR_TIMEOUT: 1,
            ncc.CAUSE_OUTSIDE_WORKTREE: 1,
        })
        self.conn.row_factory = None
        causes = dict(self.conn.execute(
            "SELECT id, no_changes_cause FROM experiments").fetchall())
        self.assertEqual(causes["a1"], ncc.CAUSE_NO_EDITS)
        self.assertEqual(causes["a2"], ncc.CAUSE_NO_EDITS)
        self.assertEqual(causes["t1"], ncc.CAUSE_LIMIT_OR_TIMEOUT)
        self.assertEqual(causes["o1"], ncc.CAUSE_OUTSIDE_WORKTREE)
        for not_a_no_change_run in ("r1", "s1", "c1", "i1", "x1", "u1", "g1"):
            self.assertIsNone(causes[not_a_no_change_run])
        # The cause is also an event in the experiment log.
        entries = ncc._log_entries(self.log_dir, "a1")
        self.assertTrue(any(e.get("event") == ncc.EVENT for e in entries))
        self.assertEqual(
            [e for e in entries if e.get("event") == ncc.EVENT][0]["no_changes_cause"],
            ncc.CAUSE_NO_EDITS,
        )

    def test_find_no_change_runs_requires_the_explicit_no_publishable_marker(self):
        marker = "implementation produced no publishable source/test/documentation changes"
        _add_experiment(self.conn, "yes1", status="needs_review", error=marker)
        _add_experiment(self.conn, "yes2", status="failed",
                        error="worker exploded; GitHub checkpoint failed: " + marker)
        # Empty diff_text without the marker is not a no-change run.
        _add_experiment(self.conn, "no1", status="failed", error="pipeline crash", diff_text=None)
        _add_experiment(self.conn, "no2", status="interrupted", error="restart", diff_text=None)
        _add_experiment(self.conn, "no3", status="cancelled", diff_text=None)
        _add_experiment(self.conn, "no4", status="needs_review",
                        error="no safe test command could be determined", diff_text="")
        _add_experiment(self.conn, "no5", status="succeeded", diff_text="")
        self.conn.row_factory = sqlite3.Row
        found = {row["id"] for row in ncc.find_no_change_runs(self.conn)}
        self.assertEqual(found, {"yes1", "yes2"})

    def test_pre_verification_failures_do_not_inflate_the_no_change_share(self):
        marker = "implementation produced no publishable source/test/documentation changes"
        _add_experiment(self.conn, "n1", status="needs_review", error=marker)
        _add_experiment(self.conn, "c1", status="failed",
                        error="unhandled exception in experiment pipeline: boom", diff_text=None)
        _add_experiment(self.conn, "i1", status="interrupted", error="restart", diff_text=None)
        _add_experiment(self.conn, "x1", status="cancelled", diff_text=None)
        _add_experiment(self.conn, "s1", status="succeeded", diff_text="diff --git a/x b/x")
        # 1 no-change run of 5 ended runs - the crash/interruption/cancellation
        # are ended runs but not no-change runs (#807 comparability).
        self.assertEqual(ncc.no_change_share(self.conn, "2026-11-01T00:00:00+00:00"),
                         (1, 5, 20.0))
        self.assertEqual(
            ncc.no_change_cause_counts(self.conn, "2026-11-01T00:00:00+00:00"),
            [("(unclassified)", 1)],
        )

    def test_limit_selects_the_most_recent_runs(self):
        for i in range(5):
            _add_experiment(self.conn, f"e{i}", status="needs_review",
                            error="GitHub checkpoint failed before verification: implementation "
                                  "produced no publishable changes",
                            created_at=f"2026-11-2{i}T10:00:00+00:00")
        counts = ncc.classify_recent_no_change_runs(self.db_path, limit=2, apply=False)
        self.assertEqual(counts, {ncc.CAUSE_NO_EDITS: 2})

    def test_the_cli_prints_the_counts(self):
        _add_experiment(self.conn, "e1", status="needs_review",
                        error="implementation produced no publishable source/test/documentation changes")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ncc.main([self.db_path, "--log-dir", str(self.log_dir), "--limit", "20"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("no-change runs classified: 1", text)
        self.assertIn(f"{ncc.CAUSE_NO_EDITS}: 1", text)


class BackfillRobustnessTests(unittest.TestCase):
    """Review of f0195c70, blocking problem 3: the startup backfill must be
    idempotent (restarts must not pile up duplicate cause events), must never
    recreate cleaned-up log files, and must survive broken rows/logs/databases
    without aborting the remaining runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        self.conn = init_db(self.db_path)
        self.addCleanup(self.conn.close)
        self.log_dir = Path(self.tmp.name) / "logs"
        self.log_dir.mkdir()

    def _marker_row(self, exp_id, *, cause=None, created_at=NOW):
        _add_experiment(
            self.conn, exp_id, status="needs_review", created_at=created_at,
            error="GitHub checkpoint failed before verification: implementation produced "
                  "no publishable source/test/documentation changes",
            cause=cause,
        )

    def _cause_events(self, exp_id):
        return [e for e in ncc._log_entries(self.log_dir, exp_id)
                if e.get("event") == ncc.EVENT]

    def test_running_the_backfill_twice_adds_no_duplicate_events(self):
        self._marker_row("a1")
        self._marker_row("a2")
        # Both experiments still have their (cleaned-up-but-present) log files.
        for exp_id in ("a1", "a2"):
            _write_log(self.log_dir, exp_id, [{"event": "worker_done", "message": "x"}])
        first = ncc.classify_recent_no_change_runs(self.db_path, log_dir=self.log_dir)
        second = ncc.classify_recent_no_change_runs(self.db_path, log_dir=self.log_dir)
        third = ncc.classify_recent_no_change_runs(self.db_path, log_dir=self.log_dir)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(first, {ncc.CAUSE_NO_EDITS: 2})
        for exp_id in ("a1", "a2"):
            self.assertEqual(len(self._cause_events(exp_id)), 1)
        self.conn.row_factory = None
        causes = dict(self.conn.execute(
            "SELECT id, no_changes_cause FROM experiments").fetchall())
        self.assertEqual(causes, {"a1": ncc.CAUSE_NO_EDITS, "a2": ncc.CAUSE_NO_EDITS})

    def test_the_backfill_never_creates_a_log_file_for_a_cleaned_up_experiment(self):
        self._marker_row("gone1")  # no log file exists for this experiment
        counts = ncc.classify_recent_no_change_runs(self.db_path, log_dir=self.log_dir)
        self.assertEqual(counts, {ncc.CAUSE_NO_EDITS: 1})
        self.assertFalse((self.log_dir / "gone1.jsonl").exists())
        # The cause is still durably recorded on the row.
        self.conn.row_factory = None
        row = self.conn.execute(
            "SELECT no_changes_cause FROM experiments WHERE id = 'gone1'").fetchone()
        self.assertEqual(row[0], ncc.CAUSE_NO_EDITS)

    def test_rows_that_already_carry_a_cause_are_counted_but_never_rewritten(self):
        self._marker_row("done1", cause=ncc.CAUSE_LIMIT_OR_TIMEOUT)
        _write_log(self.log_dir, "done1", [{"event": "unrelated", "message": "no cause event yet"}])
        counts = ncc.classify_recent_no_change_runs(self.db_path, log_dir=self.log_dir)
        self.assertEqual(counts, {ncc.CAUSE_LIMIT_OR_TIMEOUT: 1})
        self.assertEqual(self._cause_events("done1"), [])  # no event appended
        self.conn.row_factory = None
        row = self.conn.execute(
            "SELECT no_changes_cause FROM experiments WHERE id = 'done1'").fetchone()
        self.assertEqual(row[0], ncc.CAUSE_LIMIT_OR_TIMEOUT)

    def test_an_event_the_pipeline_already_wrote_is_not_duplicated(self):
        self._marker_row("pipe1")  # column NULL, but the log already has the event
        _write_log(self.log_dir, "pipe1", [
            {"event": ncc.EVENT, "message": "classified by the pipeline",
             "no_changes_cause": ncc.CAUSE_NO_EDITS},
        ])
        counts = ncc.classify_recent_no_change_runs(self.db_path, log_dir=self.log_dir)
        self.assertEqual(counts, {ncc.CAUSE_NO_EDITS: 1})
        self.assertEqual(len(self._cause_events("pipe1")), 1)

    def test_one_broken_row_does_not_abort_the_backfill(self):
        self._marker_row("good1")
        self._marker_row("bad1")
        _write_log(self.log_dir, "good1", [{"event": "worker_done", "message": "x"}])
        real_classify = ncc.classify_no_changes_cause

        def flaky(conn, experiment_id, log_dir=None, **kwargs):
            if experiment_id == "bad1":
                raise RuntimeError("simulated worker crash while classifying")
            return real_classify(conn, experiment_id, log_dir, **kwargs)

        with patch.object(ncc, "classify_no_changes_cause", side_effect=flaky):
            counts = ncc.classify_recent_no_change_runs(self.db_path, log_dir=self.log_dir)
        self.assertEqual(counts, {ncc.CAUSE_NO_EDITS: 1, ncc.CAUSE_OTHER: 1})
        self.conn.row_factory = None
        causes = dict(self.conn.execute(
            "SELECT id, no_changes_cause FROM experiments").fetchall())
        self.assertEqual(causes["good1"], ncc.CAUSE_NO_EDITS)
        self.assertEqual(causes["bad1"], ncc.CAUSE_OTHER)
        # The surviving row still got its log event.
        self.assertEqual(len(self._cause_events("good1")), 1)

    def test_a_malformed_log_line_does_not_break_classification_or_backfill(self):
        self._marker_row("messy1")
        path = self.log_dir / "messy1.jsonl"
        path.write_text("this is not json\n" + json.dumps({
            "timestamp": NOW, "level": "INFO", "logger": "t", "phase": "implementation",
            "event": "vibe_call_completed", "experiment_id": "messy1",
            "stdout": "already implemented on main, nothing to do",
        }) + "\n", encoding="utf-8")
        counts = ncc.classify_recent_no_change_runs(self.db_path, log_dir=self.log_dir)
        self.assertEqual(counts, {ncc.CAUSE_ALREADY_ON_MAIN: 1})
        self.assertEqual(len(self._cause_events("messy1")), 1)  # appended, file existed

    def test_a_pre_811_database_without_the_cause_column_is_tolerated(self):
        old_db = str(Path(self.tmp.name) / "old.db")
        old = sqlite3.connect(old_db)
        old.execute(
            "CREATE TABLE experiments (id TEXT PRIMARY KEY, goal TEXT, status TEXT, "
            "created_at TEXT, error TEXT, diff_text TEXT)"
        )
        old.execute(
            "INSERT INTO experiments VALUES ('old1', 'g', 'needs_review', ?, "
            "'implementation produced no publishable source/test/documentation changes', NULL)",
            (NOW,),
        )
        old.commit()
        old.close()
        # find_no_change_runs falls back to NULL for the missing column.
        conn = sqlite3.connect(old_db)
        conn.row_factory = sqlite3.Row
        self.assertEqual([r["id"] for r in ncc.find_no_change_runs(conn)], ["old1"])
        conn.close()
        # The full backfill neither crashes nor creates a log file.
        counts = ncc.classify_recent_no_change_runs(old_db, log_dir=self.log_dir)
        self.assertEqual(counts, {ncc.CAUSE_NO_EDITS: 1})
        self.assertFalse((self.log_dir / "old1.jsonl").exists())
        self.assertTrue(ncc.set_no_changes_cause(sqlite3.connect(old_db), "old1", "other"))

    def test_record_no_changes_cause_twice_logs_exactly_one_event(self):
        # A repaired or resumed experiment classifies again: the second call
        # must not append a second no_changes_cause event.
        self._marker_row("twice1")
        elog = ExperimentLogger("twice1", log_dir=self.log_dir)
        ncc.record_no_changes_cause(self.conn, "twice1", ncc.CAUSE_NO_EDITS, elog)
        ncc.record_no_changes_cause(self.conn, "twice1", ncc.CAUSE_LIMIT_OR_TIMEOUT, elog)
        events = self._cause_events("twice1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["no_changes_cause"], ncc.CAUSE_NO_EDITS)  # first wins
        self.conn.row_factory = None
        row = self.conn.execute(
            "SELECT no_changes_cause FROM experiments WHERE id = 'twice1'").fetchone()
        self.assertEqual(row[0], ncc.CAUSE_NO_EDITS)

    def test_set_no_changes_cause_reports_first_write_only(self):
        self._marker_row("fw1")
        self.assertTrue(ncc.set_no_changes_cause(self.conn, "fw1", ncc.CAUSE_OTHER))
        self.assertFalse(ncc.set_no_changes_cause(self.conn, "fw1", ncc.CAUSE_NO_EDITS))
        self.conn.row_factory = None
        row = self.conn.execute(
            "SELECT no_changes_cause FROM experiments WHERE id = 'fw1'").fetchone()
        self.assertEqual(row[0], ncc.CAUSE_OTHER)


class PipelineRecordingTests(unittest.TestCase):
    """End to end: a no-change experiment records its cause on the row, in the
    log and in the error text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo_root = root / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)
        self.worktrees_root = root / "worktrees"
        self.log_dir = root / "logs"
        self.db_path = str(root / "agent.db")
        self.worker = MagicMock()
        self.worker.provider = "mistral"
        self.worker.run.return_value = _result()

        def factory(worktree_path: str):
            return self.worker

        self.service = ExperimentService(
            worker_factory=factory,
            db_path=self.db_path,
            repo_root=str(self.repo_root),
            worktrees_root=str(self.worktrees_root),
            experiment_log_dir=str(self.log_dir),
        )
        self.addCleanup(self.service.close)

    def _github(self):
        github = MagicMock()
        github.experiment_base_sha.return_value = subprocess.run(
            ["git", "-C", str(self.repo_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        github.checkpoint_experiment.side_effect = GitHubWorkTrackingError(
            "implementation produced no publishable source/test/documentation changes"
        )
        return github

    def _read_log(self, experiment_id: str) -> list[dict]:
        path = self.log_dir / f"{experiment_id}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_an_answer_without_edits_records_answered_without_edits(self):
        experiment = self.service.create_experiment(goal="Explain the module.")
        result = self.service.run_experiment(experiment.id, github_work_tracking=self._github())

        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertIn("no publishable", result.error)
        self.assertIn(f"no_changes_cause={ncc.CAUSE_NO_EDITS}", result.error)
        self.assertEqual(result.no_changes_cause, ncc.CAUSE_NO_EDITS)
        self.assertEqual(
            self.service.get_experiment(experiment.id).no_changes_cause,
            ncc.CAUSE_NO_EDITS,
        )
        events = [e["event"] for e in self._read_log(experiment.id)]
        self.assertIn(ncc.EVENT, events)
        entry = [e for e in self._read_log(experiment.id) if e["event"] == ncc.EVENT][0]
        self.assertEqual(entry["no_changes_cause"], ncc.CAUSE_NO_EDITS)

    def test_a_timeout_records_limit_or_timeout(self):
        self.worker.run.return_value = _result(
            success=False, timed_out=True, error="Vibe reached the absolute cap of 10800s")
        experiment = self.service.create_experiment(goal="Change the module.")
        result = self.service.run_experiment(experiment.id, github_work_tracking=self._github())

        self.assertEqual(result.status, STATUS_TIMED_OUT)
        self.assertIn(f"no_changes_cause={ncc.CAUSE_LIMIT_OR_TIMEOUT}", result.error)
        self.assertEqual(result.no_changes_cause, ncc.CAUSE_LIMIT_OR_TIMEOUT)

    def test_writing_outside_the_worktree_records_wrote_outside_worktree(self):
        def write_outside(*args, **kwargs):
            # The agent edits the deployed checkout instead of its worktree.
            (self.repo_root / "module.py").write_text("changed by the agent\n")
            return _result()

        self.worker.run.side_effect = write_outside
        experiment = self.service.create_experiment(goal="Change the module.")
        result = self.service.run_experiment(experiment.id, github_work_tracking=self._github())

        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("wrote outside its worktree", result.error)
        self.assertIn(f"no_changes_cause={ncc.CAUSE_OUTSIDE_WORKTREE}", result.error)
        self.assertEqual(result.no_changes_cause, ncc.CAUSE_OUTSIDE_WORKTREE)
        # The guard stashed the outside write, so the deployed checkout is clean.
        status = subprocess.run(
            ["git", "-C", str(self.repo_root), "status", "--porcelain"],
            capture_output=True, text=True,
        ).stdout
        self.assertEqual(status.strip(), "")
        # The guard event is in the log and the prompt never contained the path.
        entries = self._read_log(experiment.id)
        self.assertIn(OUTSIDE_EVENT, [e["event"] for e in entries])
        guard_entry = [e for e in entries if e["event"] == OUTSIDE_EVENT][0]
        self.assertEqual(guard_entry["no_changes_cause"], ncc.CAUSE_OUTSIDE_WORKTREE)
        task = self.worker.run.call_args.kwargs["task"]
        self.assertNotIn(str(self.repo_root), task)


class WeeklyReviewPackTests(unittest.TestCase):
    def test_the_starter_pack_counts_the_causes_and_the_share(self):
        from datetime import datetime, timezone

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / "agent.db")
        conn = init_db(db_path)
        now = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
        recent = now.isoformat()
        _add_experiment(conn, "n1", status="needs_review", created_at=recent,
                        error="GitHub checkpoint failed before verification: implementation "
                              "produced no publishable source/test/documentation changes",
                        cause=ncc.CAUSE_NO_EDITS)
        _add_experiment(conn, "n2", status="timed_out", created_at=recent,
                        error="implementation phase timed out; GitHub checkpoint failed: "
                              "implementation produced no publishable source/test/documentation "
                              "changes", cause=ncc.CAUSE_LIMIT_OR_TIMEOUT)
        _add_experiment(conn, "n3", status="needs_review", created_at=recent,
                        error="GitHub checkpoint failed before verification: implementation "
                              "produced no publishable changes", cause=None)
        # A pre-verification crash: an ended run, but NOT a no-change run (an empty
        # diff_text alone must not count - review of f0195c70, blocking problem 2).
        _add_experiment(conn, "c1", status="failed", created_at=recent,
                        error="unhandled exception in experiment pipeline: boom", diff_text=None)
        _add_experiment(conn, "s1", status="succeeded", created_at=recent,
                        diff_text="diff --git a/x b/x")
        conn.close()

        pack = starter_pack(db_path, tmp.name, now)
        self.assertIn("by recorded cause (#811)", pack)
        self.assertIn(f"{ncc.CAUSE_NO_EDITS} | 1", pack)
        self.assertIn(f"{ncc.CAUSE_LIMIT_OR_TIMEOUT} | 1", pack)
        self.assertIn("(unclassified) | 1", pack)
        self.assertIn("no-change share of ended experiments (#811)", pack)
        # 3 of 5 ended experiments produced no changes; the crash is not counted.
        self.assertIn("no-change: 3 | 5 | 60.0%", pack)


if __name__ == "__main__":
    unittest.main()
