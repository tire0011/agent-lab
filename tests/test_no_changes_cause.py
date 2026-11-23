"""#811: every experiment that produces no publishable changes gets a recorded,
countable cause (column + log event + error token), and the most frequent cause
- the agent writing outside its worktree - is prevented by scrubbing the
deployed checkout's path from the prompt (see tests/test_checkout_guard.py)."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules import no_changes_cause as ncc
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
        rows = [
            ("a1", "needs_review", "GitHub checkpoint failed before verification: implementation "
                                   "produced no publishable source/test/documentation changes", None),
            ("a2", "needs_review", "GitHub checkpoint failed before verification: implementation "
                                   "produced no publishable source/test/documentation changes", None),
            ("t1", "timed_out", "implementation phase timed out", None),
            ("o1", "failed", "coding agent mistral wrote outside its worktree into /agent: x.py", None),
            ("r1", "rejected", "goal rejected by policy: ...", None),  # policy rejection: not a no-change run
            ("s1", "succeeded", None, "diff --git a/x b/x"),
        ]
        for exp_id, status, error, diff in rows:
            _add_experiment(self.conn, exp_id, status=status, error=error, diff_text=diff)
        _write_log(self.log_dir, "o1", [{"event": OUTSIDE_EVENT, "message": "wrote outside"}])

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
        self.assertIsNone(causes["r1"])   # policy rejections are never classified
        self.assertIsNone(causes["s1"])   # succeeded runs have changes
        # The cause is also an event in the experiment log.
        entries = ncc._log_entries(self.log_dir, "a1")
        self.assertTrue(any(e.get("event") == ncc.EVENT for e in entries))
        self.assertEqual(
            [e for e in entries if e.get("event") == ncc.EVENT][0]["no_changes_cause"],
            ncc.CAUSE_NO_EDITS,
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
                        error="implementation phase timed out", cause=ncc.CAUSE_LIMIT_OR_TIMEOUT)
        _add_experiment(conn, "n3", status="needs_review", created_at=recent,
                        error="GitHub checkpoint failed before verification: implementation "
                              "produced no publishable changes", cause=None)
        _add_experiment(conn, "s1", status="succeeded", created_at=recent,
                        diff_text="diff --git a/x b/x")
        conn.close()

        pack = starter_pack(db_path, tmp.name, now)
        self.assertIn("by recorded cause (#811)", pack)
        self.assertIn(f"{ncc.CAUSE_NO_EDITS} | 1", pack)
        self.assertIn(f"{ncc.CAUSE_LIMIT_OR_TIMEOUT} | 1", pack)
        self.assertIn("(unclassified) | 1", pack)
        self.assertIn("no-change share of ended experiments (#811)", pack)
        # 3 of 4 ended experiments produced no changes.
        self.assertIn("no-change: 3 | 4 | 75.0%", pack)


if __name__ == "__main__":
    unittest.main()
