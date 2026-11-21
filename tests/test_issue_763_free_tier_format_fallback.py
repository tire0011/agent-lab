"""#763: a free-tier coder that returns no usable edit hands the same run on.

Fake workers only; no model, CLI or network is called.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules import free_tier_guard
from modules.central_logging import read_log_entries
from modules.experiment_service import STATUS_CANCELLED, STATUS_FAILED, ExperimentService
from modules.nexus_status_issue import render
from modules.run_store import init_db
from tests.test_experiment_service import (
    _init_git_repo,
    _make_result,
    _patch_service,
    _write_affected_files_side_effect,
)

FREE_ANSWER = "I would change the helper like this, but here is prose without blocks. " * 20
FORMAT_ERROR = (
    "patch rejected (EditFormatError): local model did not return any SEARCH/REPLACE edit blocks"
)


def _format_failure(**overrides):
    values = dict(
        success=False, exit_code=1, sandbox="workspace-write", stdout=FREE_ANSWER,
        final_message=FREE_ANSWER, error=FORMAT_ERROR,
    )
    values.update(overrides)
    return _make_result(**values)


class _ServiceFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo_root = root / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)
        self.db_path = str(root / "agent.db")
        env = patch.dict(os.environ, {free_tier_guard.ENV_GUARD: "1"})
        env.start()
        self.addCleanup(env.stop)

        self.free_worker = MagicMock()
        self.free_worker.provider = "mistral"
        self.free_worker.run.return_value = _format_failure()
        self.service = ExperimentService(
            selection_worker=MagicMock(),
            plan_worker_factory=lambda _scratch: MagicMock(),
            worker_factory=lambda _path: self.free_worker,
            db_path=self.db_path,
            repo_root=str(self.repo_root),
            worktrees_root=str(root / "worktrees"),
        )
        self.addCleanup(self.service.close)

        self.paid_worker = MagicMock()
        self.paid_worker.provider = "codex"
        self.paid_worker.run.side_effect = _write_affected_files_side_effect()

    def _run(self, *, handover: str | None = "codex", complexity: str = "medium"):
        with _patch_service("other_provider", return_value=handover), _patch_service(
            "build_worker", return_value=self.paid_worker
        ) as build:
            experiment = self.service.create_experiment(goal="Fix the helper.")
            with self.service._lock, self.service._conn:
                self.service._conn.execute(
                    "UPDATE experiments SET complexity = ? WHERE id = ?", (complexity, experiment.id)
                )
            result = self.service.run_experiment(experiment.id)
        return result, build

    def _fallback_logs(self, experiment_id: str) -> list[dict]:
        entries = read_log_entries(self.service.get_experiment_logger(experiment_id).path)
        return [e for e in entries if e.get("event") == "implementation_provider_fallback"]


class FreeTierFormatFallbackTests(_ServiceFixture):
    def test_untouched_worktree_hands_the_same_run_to_the_next_provider(self):
        result, build = self._run()

        build.assert_called_once()
        self.assertEqual(build.call_args.args[0], "codex")
        self.paid_worker.run.assert_called_once()
        self.assertNotEqual(result.status, STATUS_FAILED, result.error)
        self.assertEqual(result.implementation_provider, "codex")
        # One note, never the free coder's text.
        task = self.paid_worker.run.call_args.kwargs["task"]
        self.assertIn(free_tier_guard.FORMAT_FALLBACK_NOTE, task)
        self.assertNotIn("prose without blocks", task)
        # Counted on the experiment row.
        self.assertEqual(result.free_tier_provider, "mistral")
        self.assertEqual(result.free_tier_format_failures, 1)
        logs = self._fallback_logs(result.id)
        self.assertEqual(len(logs), 1, logs)
        self.assertEqual(logs[0]["reason"], "free_tier_format_failure")
        self.assertIs(logs[0]["paid_fallback_used"], True)
        self.assertEqual(logs[0]["free_answer"], FREE_ANSWER[:300])

    def test_partial_edits_are_never_thrown_away(self):
        def dirty_run(**kwargs):
            (Path(kwargs["working_dir"]) / "partial.py").write_text("x = 1\n")
            return _format_failure()

        self.free_worker.run.side_effect = dirty_run
        result, build = self._run()

        build.assert_not_called()
        self.paid_worker.run.assert_not_called()
        self.assertEqual(result.free_tier_format_failures, 1)
        self.assertEqual(self._fallback_logs(result.id), [])

    def test_a_paid_provider_with_the_same_error_does_not_fall_back(self):
        self.free_worker.provider = "claude"
        result, build = self._run()

        build.assert_not_called()
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("EditFormatError", result.error)
        self.assertIsNone(result.free_tier_provider)

    def test_no_next_provider_keeps_todays_result(self):
        result, build = self._run(handover=None)

        build.assert_not_called()
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertTrue(result.error.startswith(FORMAT_ERROR), result.error)

    def test_a_cancelled_experiment_does_not_fall_back(self):
        def cancelled_run(**_kwargs):
            experiment_id = self.service.list_experiments()[0].id
            self.service.cancel_experiment(experiment_id, cancelled_by="test")
            return _format_failure()

        self.free_worker.run.side_effect = cancelled_run
        result, build = self._run()

        build.assert_not_called()
        self.paid_worker.run.assert_not_called()
        self.assertEqual(result.status, STATUS_CANCELLED)

    def test_other_free_tier_errors_do_not_fall_back(self):
        self.free_worker.run.return_value = _format_failure(error="mistral call failed")
        result, build = self._run()

        build.assert_not_called()
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.free_tier_format_failures, 0)


def _seed(db_path: str, outcomes: list[int], provider: str = "mistral") -> None:
    conn = init_db(db_path)
    now = datetime.now(timezone.utc)
    try:
        for i, failed in enumerate(outcomes):
            ts = (now - timedelta(minutes=i)).isoformat()
            conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at, implementation_started_at, "
                "free_tier_provider, free_tier_format_failures) VALUES (?,?,?,?,?,?,?)",
                (f"seed{i}", "g", "failed" if failed else "succeeded", ts, ts, provider, failed),
            )
        conn.commit()
    finally:
        conn.close()


class FreeTierGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "agent.db")
        env = patch.dict(os.environ, {free_tier_guard.ENV_GUARD: "1"})
        env.start()
        self.addCleanup(env.stop)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def test_more_than_half_of_ten_runs_failing_blocks_non_small_work(self):
        _seed(self.db_path, [1] * 6 + [0] * 4)
        conn = self._conn()
        self.assertTrue(free_tier_guard.blocks_free_tier(conn, "medium"))
        self.assertTrue(free_tier_guard.blocks_free_tier(conn, None))
        self.assertFalse(free_tier_guard.blocks_free_tier(conn, "small"))

    def test_half_or_too_few_runs_do_not_block(self):
        _seed(self.db_path, [1] * 5 + [0] * 5)
        self.assertFalse(free_tier_guard.blocks_free_tier(self._conn(), "medium"))

    def test_fewer_than_ten_runs_do_not_block(self):
        _seed(self.db_path, [1] * 9)
        self.assertFalse(free_tier_guard.blocks_free_tier(self._conn(), "medium"))

    def test_the_guard_can_be_turned_off(self):
        _seed(self.db_path, [1] * 10)
        with patch.dict(os.environ, {free_tier_guard.ENV_GUARD: "0"}):
            self.assertFalse(free_tier_guard.blocks_free_tier(self._conn(), "medium"))
            self.assertIn("off", free_tier_guard.status_line(self._conn()))

    def test_daily_stats_show_the_failure_share(self):
        _seed(self.db_path, [1, 0, 1, 1])
        stats = free_tier_guard.daily_stats(self._conn())
        self.assertEqual(sum(day["runs"] for day in stats), 4)
        self.assertEqual(sum(day["free_tier_format_failures"] for day in stats), 3)

    def test_status_output_shows_the_setting(self):
        _seed(self.db_path, [1] * 7 + [0] * 3)
        body = render(self._conn(), now=datetime.now(timezone.utc), budget=5, spent=1,
                      paused=False, busy=None)
        self.assertIn("**Free-tier guard:** active", body)
        self.assertIn("7 of the last 10", body)
        self.assertIn("more than 50% over 10 runs", body)


class FreeTierGuardInServiceTests(_ServiceFixture):
    def test_a_tripped_guard_starts_non_small_work_on_the_next_provider(self):
        _seed(self.db_path, [1] * 10)
        result, build = self._run()

        self.free_worker.run.assert_not_called()
        self.assertEqual(build.call_args.args[0], "codex")
        self.paid_worker.run.assert_called_once()
        self.assertIsNone(result.free_tier_provider)


if __name__ == "__main__":
    unittest.main()
