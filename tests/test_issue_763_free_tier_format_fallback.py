"""#763: a free-tier coder that returns no usable edit hands the same run on.

Fake workers only; no model, CLI or network is called.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules import free_tier_guard
from modules.central_logging import read_log_entries
from modules.coding_provider import NO_ELIGIBLE_CODER
from modules.experiment_service import (
    ExperimentNotRunnableError, ExperimentService,
    STATUS_CANCELLED, STATUS_FAILED, STATUS_NEEDS_REVIEW, STATUS_SUCCEEDED,
)
from modules.nexus_initiative import _is_provider_overload
from modules.experiment_service.git_utils import _run_git
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
        env = patch.dict(os.environ, {
            free_tier_guard.ENV_GUARD: "1",
            free_tier_guard.ENV_WINDOW: "10",
            free_tier_guard.ENV_MAX_SHARE: "0.5",
        })
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
            experiment_log_dir=str(root / "logs"),
        )
        self.addCleanup(self.service.close)

        self.paid_worker = MagicMock()
        self.paid_worker.provider = "codex"
        self.paid_worker.run.side_effect = _write_affected_files_side_effect()

    def _run(
        self, *, handover: str | None = "codex", complexity: str = "medium",
        selection=None, construction=None,
    ):
        with _patch_service("other_provider", return_value=handover, side_effect=selection), _patch_service(
            "build_worker", return_value=self.paid_worker, side_effect=construction,
        ) as build:
            experiment = self.service.create_experiment(goal="Fix the helper.")
            self.experiment_id = experiment.id
            with self.service._lock, self.service._conn:
                self.service._conn.execute(
                    "UPDATE experiments SET complexity = ? WHERE id = ?", (complexity, experiment.id)
                )
            result = self.service.run_experiment(experiment.id)
        return result, build

    def _cancel(self):
        self.service.cancel_experiment(self.experiment_id, cancelled_by="test")

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
            subprocess.run(
                ["git", "config", "status.showUntrackedFiles", "no"],
                cwd=kwargs["working_dir"], check=True,
            )
            (Path(kwargs["working_dir"]) / "partial.py").write_text("x = 1\n")
            return _format_failure(error=f"{FORMAT_ERROR} (429 overloaded)")

        self.free_worker.run.side_effect = dirty_run
        result, build = self._run()

        build.assert_not_called()
        self.paid_worker.run.assert_not_called()
        self.assertEqual(result.free_tier_format_failures, 1)
        self.assertEqual(self._fallback_logs(result.id), [])
        self.assertTrue((Path(result.worktree_path) / "partial.py").exists())

    def test_a_paid_provider_with_the_same_error_does_not_fall_back(self):
        self.free_worker.provider = "claude"
        self.free_worker.run.return_value = _format_failure(error=f"{FORMAT_ERROR} (429 overloaded)")
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


    def test_cancellation_during_provider_selection_prevents_construction(self):
        def select(*args, **kwargs):
            self._cancel()
            return "codex"

        result, build = self._run(selection=select)
        build.assert_not_called()
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertEqual(result.free_tier_format_failures, 1)
        self.assertEqual(self._fallback_logs(result.id), [])

    def test_cancellation_during_construction_prevents_launch(self):
        def build(*args, **kwargs):
            self._cancel()
            return self.paid_worker

        result, _ = self._run(construction=build)
        self.paid_worker.run.assert_not_called()
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertEqual(self._fallback_logs(result.id), [])

    def test_token_budget_stop_during_construction_prevents_launch(self):
        def build(*args, **kwargs):
            self.service._stop_for_token_budget(self.experiment_id, 123, "test")
            return self.paid_worker

        result, _ = self._run(construction=build)
        self.paid_worker.run.assert_not_called()
        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertEqual(result.free_tier_format_failures, 1)
        self.assertEqual(self._fallback_logs(result.id), [])

    def test_token_budget_stop_during_construction_survives_setup_error(self):
        def build(*args, **kwargs):
            self.service._stop_for_token_budget(self.experiment_id, 123, "test")
            raise RuntimeError("construction interrupted")

        result, _ = self._run(construction=build)
        self.paid_worker.run.assert_not_called()
        self.assertEqual(result.status, STATUS_NEEDS_REVIEW)
        self.assertIn("experiment token budget exceeded", result.error)
        self.assertEqual(result.free_tier_format_failures, 1)

    def test_unreadable_worktree_status_keeps_format_failure(self):
        with patch(
            "modules.experiment_service.service_parts.implementation._run_git",
        ) as run_git:

            def fail_status(args, **kwargs):
                if args[0] == "status":
                    raise subprocess.TimeoutExpired("git status", 30)
                return _run_git(args, **kwargs)

            run_git.side_effect = fail_status
            result, build = self._run()
        build.assert_not_called()
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.free_tier_format_failures, 1)

    def test_paid_fallback_overload_never_returns_to_tried_free_worker(self):
        self.paid_worker.run.side_effect = None
        self.paid_worker.run.return_value = _format_failure(error="429 overloaded")

        def select(provider, *, db_path, exclude):
            if provider == "mistral":
                return "codex"
            self.assertEqual(exclude, {"mistral", "codex"})
            return "mistral"  # Even a stale selection is rejected.

        result, build = self._run(selection=select)
        self.free_worker.run.assert_called_once()
        self.paid_worker.run.assert_called_once()
        build.assert_called_once()
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.error, "429 overloaded")

    def test_crash_leaves_unknown_outcome_and_second_run_is_rejected(self):
        self.free_worker.run.side_effect = RuntimeError("worker crashed")
        result, build = self._run()
        build.assert_not_called()
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.free_tier_provider, "mistral")
        self.assertIsNone(result.free_tier_format_failures)
        with self.assertRaises(ExperimentNotRunnableError):
            self.service.run_experiment(result.id)
        self.free_worker.run.assert_called_once()

    def test_fallback_crash_preserves_outcome_and_cannot_relaunch(self):
        def crash(**kwargs):
            with self.assertRaises(ExperimentNotRunnableError):
                self.service.run_experiment(self.experiment_id)
            (Path(kwargs["working_dir"]) / "partial.py").write_text("x = 1\n")
            raise RuntimeError("paid worker crashed")

        self.paid_worker.run.side_effect = crash
        result, _ = self._run()
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("paid worker crashed", result.error)
        self.assertEqual(result.free_tier_format_failures, 1)
        self.assertTrue((Path(result.worktree_path) / "partial.py").exists())
        self.assertEqual(len(self._fallback_logs(result.id)), 1)
        self.paid_worker.run.assert_called_once()

    def test_repeated_outcome_write_preserves_first_result(self):
        result, _ = self._run()
        self.service._note_free_tier_outcome(
            result.id, "mistral", _format_failure(error=None),
            self.service.get_experiment_logger(result.id),
        )
        current = self.service.get_experiment(result.id)
        self.assertEqual(current.status, result.status)
        self.assertEqual(current.free_tier_format_failures, 1)

    def test_outcome_write_failure_keeps_the_handoff_available(self):
        with self.service._lock, self.service._conn:
            self.service._conn.execute(
                "CREATE TRIGGER fail_outcome_write BEFORE UPDATE OF free_tier_format_failures "
                "ON experiments BEGIN SELECT RAISE(ABORT, 'outcome write failed'); END"
            )
        result, _ = self._run()
        self.paid_worker.run.assert_called_once()
        self.assertNotEqual(result.status, STATUS_FAILED, result.error)
        self.assertIsNone(result.free_tier_format_failures)
        entries = read_log_entries(self.service.get_experiment_logger(result.id).path)
        self.assertIn("free_tier_outcome_write_failed", [entry["event"] for entry in entries])

    def test_repair_preserves_handoff_worker(self):
        def paid_call(**kwargs):
            worktree = Path(kwargs["working_dir"])
            (worktree / "implementation.py").write_text("def helper(): return 1\n")
            assertion = (
                "self.assertTrue(True)" if kwargs["phase"] == "implementation_repair"
                else "self.assertEqual(1, 2)"
            )
            (worktree / "tests" / "test_added.py").write_text(
                "import unittest\nclass TestHelper(unittest.TestCase):\n"
                f"    def test_helper(self): {assertion}\n"
            )
            return _make_result(sandbox="workspace-write")

        self.service.test_python_executable = sys.executable
        self.paid_worker.run.side_effect = paid_call
        result, build = self._run()
        self.assertEqual(result.status, STATUS_SUCCEEDED, result.error)
        build.assert_called_once()
        self.free_worker.run.assert_called_once()
        self.assertEqual(self.paid_worker.run.call_count, 2)
        self.assertEqual(self.paid_worker.run.call_args.kwargs["phase"], "implementation_repair")


def _seed(db_path: str, outcomes: list[int | None], provider: str = "mistral") -> None:
    conn = init_db(db_path)
    now = datetime.now(timezone.utc)
    try:
        for i, failed in enumerate(outcomes):
            ts = (now - timedelta(minutes=i)).isoformat()
            conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at, implementation_started_at, "
                "free_tier_provider, free_tier_format_failures) VALUES (?,?,?,?,?,?,?)",
                (f"seed{i}", "g", ("implementing" if failed is None else "failed" if failed else "succeeded"),
                 ts, ts, provider, failed),
            )
        conn.commit()
    finally:
        conn.close()


class FreeTierGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "agent.db")
        env = patch.dict(os.environ, {
            free_tier_guard.ENV_GUARD: "1",
            free_tier_guard.ENV_WINDOW: "10",
            free_tier_guard.ENV_MAX_SHARE: "0.5",
        })
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

    def test_pending_calls_do_not_displace_completed_failures(self):
        _seed(self.db_path, [None] * 12 + [1] * 6 + [0] * 4)
        conn = self._conn()
        state = free_tier_guard.guard_state(conn)
        self.assertTrue(state.tripped)
        self.assertEqual((state.runs, state.failures), (10, 6))
        self.assertEqual(sum(day["runs"] for day in free_tier_guard.daily_stats(conn)), 10)

    def test_status_output_shows_the_setting(self):
        _seed(self.db_path, [1] * 7 + [0] * 3)
        body = render(self._conn(), now=datetime.now(timezone.utc), budget=5, spent=1,
                      paused=False, busy=None)
        self.assertIn("**Free-tier guard:** active", body)
        self.assertIn("7 of the last 10", body)
        self.assertIn("more than 50% over 10 runs", body)
        self.assertIn("mistral format failures 7/10 (70%)", body)


class FreeTierGuardInServiceTests(_ServiceFixture):
    def test_a_tripped_guard_starts_non_small_work_on_the_next_provider(self):
        _seed(self.db_path, [1] * 10)
        result, build = self._run()

        self.free_worker.run.assert_not_called()
        self.assertEqual(build.call_args.args[0], "codex")
        self.paid_worker.run.assert_called_once()
        self.assertIsNone(result.free_tier_provider)

    def test_guard_defers_non_small_work_without_an_eligible_provider(self):
        _seed(self.db_path, [1] * 10)
        result, build = self._run(handover=None)
        self.free_worker.run.assert_not_called()
        build.assert_not_called()
        self.assertEqual(result.error, NO_ELIGIBLE_CODER)
        self.assertTrue(_is_provider_overload(result.status, result.error))

    def test_overload_cannot_handoff_to_guarded_free_tier(self):
        _seed(self.db_path, [1] * 10)
        self.free_worker.provider = "claude"
        self.free_worker.run.return_value = _format_failure(error="429 overloaded")

        def select(provider, *, db_path, exclude):
            self.assertIn("mistral", exclude)
            return "mistral"

        result, build = self._run(selection=select)
        self.free_worker.run.assert_called_once()
        build.assert_not_called()
        self.assertEqual(result.error, "429 overloaded")

    def test_guard_reevaluates_before_repair(self):
        self.service.test_python_executable = sys.executable

        def free_call(**kwargs):
            _seed(self.db_path, [1] * 10)
            worktree = Path(kwargs["working_dir"])
            (worktree / "implementation.py").write_text("def helper(): return 1\n")
            (worktree / "tests" / "test_added.py").write_text(
                "import unittest\nclass TestHelper(unittest.TestCase):\n"
                "    def test_helper(self): self.assertTrue(False)\n"
            )
            return _make_result(sandbox="workspace-write")

        self.free_worker.run.side_effect = free_call
        result, build = self._run(handover=None)
        self.free_worker.run.assert_called_once()
        build.assert_not_called()
        self.assertTrue(result.error.startswith(NO_ELIGIBLE_CODER), result.error)


if __name__ == "__main__":
    unittest.main()
