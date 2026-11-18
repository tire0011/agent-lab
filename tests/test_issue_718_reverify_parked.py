"""#718: parked timing-check work is retried without another coding start."""

from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from unittest.mock import MagicMock

from modules.experiment_service.reverify import (
    list_reverifications, parked_candidates, reverify_parked,
)
from modules.nexus_initiative import KIND_INTEGRATION, NexusInitiativeService
from modules.nexus_planner import _history_line
from modules.run_store import init_db


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)
    return proc.stdout.strip()


class FakeService:
    def __init__(self, repo: Path, db: Path) -> None:
        self.repo_root = str(repo)
        self._conn = init_db(str(db))
        self._conn.row_factory = __import__("sqlite3").Row
        self._lock = threading.Lock()
        self.verify_calls = 0
        self.verify_error = None
        self.verify_kwargs: list[dict] = []

    def add(self, branch: str) -> None:
        self._conn.execute(
            """INSERT INTO experiments
               (id, goal, status, created_at, branch_name, error)
               VALUES ('exp', 'GitHub Issue #718', 'needs_review',
                       '2026-11-14T00:00:00+00:00', ?,
                       'timing check failed closed: host failure')""",
            (branch,),
        )
        self._conn.commit()

    def get_experiment(self, experiment_id: str):
        row = self._conn.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        return SimpleNamespace(**dict(row))

    def _force_status(self, experiment_id: str, status: str, **fields):
        assignments = ["status=?", *(f"{key}=?" for key in fields)]
        self._conn.execute(
            f"UPDATE experiments SET {', '.join(assignments)} WHERE id=?",
            (status, *fields.values(), experiment_id),
        )
        self._conn.commit()

    def _verify(self, experiment_id: str, worktree: str, **kwargs):
        self.verify_calls += 1
        self.verify_kwargs.append(kwargs)
        # Like the real verifier: the run's evidence is rewritten.
        self._force_status(
            experiment_id, "verifying", test_output="re-verification output",
            diff_text="rebased diff", base_commit_sha="rebased-base",
        )
        if self.verify_error:
            self._force_status(
                experiment_id, "failed", ended_at="now", error=self.verify_error,
            )
            return self.get_experiment(experiment_id)
        self._force_status(experiment_id, "succeeded", ended_at="now", error=None)
        return self.get_experiment(experiment_id)


class ReverifyParkedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "nexus@example.invalid")
        git(self.repo, "config", "user.name", "Nexus")
        (self.repo / "value.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "value.txt")
        git(self.repo, "commit", "-m", "base")
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-u", "origin", "main")
        self.service = FakeService(self.repo, root / "runs.db")

    def tearDown(self) -> None:
        self.service._conn.close()
        self.tmp.cleanup()

    def test_missing_branch_is_stored_and_same_main_is_skipped(self) -> None:
        self.service.add("missing")
        first = reverify_parked("exp", service=self.service, issue_is_open=lambda _n: True)
        self.assertEqual(first["verdict"], "branch_missing")
        self.assertIsNone(reverify_parked("exp", service=self.service, issue_is_open=lambda _n: True))
        self.assertEqual(list_reverifications(self.service)[0]["verdict"], "branch_missing")
        self.assertEqual(self.service.verify_calls, 0)

    def test_conflict_leaves_branch_untouched(self) -> None:
        git(self.repo, "checkout", "-b", "parked")
        (self.repo / "value.txt").write_text("parked\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "parked")
        parked_sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "main")
        (self.repo / "value.txt").write_text("main\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "main")
        git(self.repo, "push", "origin", "main")
        self.service.add("parked")

        result = reverify_parked("exp", service=self.service, issue_is_open=lambda _n: True)

        self.assertEqual(result["verdict"], "rebase_conflict")
        self.assertEqual(git(self.repo, "rev-parse", "parked"), parked_sha)
        self.assertEqual(self.service.verify_calls, 0)

    def test_closed_issue_is_not_run_but_recorded_as_skipped(self) -> None:
        self.service.add("missing")
        result = reverify_parked("exp", service=self.service, issue_is_open=lambda _n: False)
        self.assertEqual(result["verdict"], "skipped:issue_closed")
        self.assertEqual(self.service.verify_calls, 0)
        # It no longer occupies a candidate slot on this main commit.
        main = git(self.repo, "rev-parse", "origin/main")
        self.assertEqual(parked_candidates(self.service, main), [])

    def test_missing_issue_lookup_fails_closed(self) -> None:
        self.service.add("missing")
        self.assertIsNone(reverify_parked("exp", service=self.service))
        self.assertEqual(list_reverifications(self.service), [])

    def test_disabled_flag_does_not_touch_the_experiment_service(self) -> None:
        initiative = object.__new__(NexusInitiativeService)
        with patch.dict("os.environ", {"NEXUS_REVERIFY_PARKED": "off"}):
            # No attributes are initialized: reaching beyond the flag guard
            # would fail, which makes this a strict no-work assertion.
            initiative._reverify_parked()

    def test_since_fixed_failure_passes_and_enters_adoption_path(self) -> None:
        git(self.repo, "checkout", "-b", "parked")
        (self.repo / "feature.txt").write_text("done\n", encoding="utf-8")
        git(self.repo, "add", "feature.txt")
        git(self.repo, "commit", "-m", "feature")
        git(self.repo, "push", "-u", "origin", "parked")
        git(self.repo, "checkout", "main")
        (self.repo / "host.txt").write_text("fixed\n", encoding="utf-8")
        git(self.repo, "add", "host.txt")
        git(self.repo, "commit", "-m", "host fix")
        self.service.add("parked")

        result = reverify_parked("exp", service=self.service, issue_is_open=lambda _n: True)

        self.assertEqual(result["verdict"], "passes_now")
        experiment = self.service.get_experiment("exp")
        self.assertEqual(experiment.status, "succeeded")
        # The normal adoption path requires the exact verified worktree; a pass
        # must not hand it a path that reverify_parked already deleted.
        self.assertTrue(Path(experiment.worktree_path).is_dir())
        self.assertEqual(self.service.verify_calls, 1)  # verifier only; no coding client exists

    def test_real_failure_stays_blocked_with_reason(self) -> None:
        git(self.repo, "checkout", "-b", "parked")
        (self.repo / "feature.txt").write_text("broken\n", encoding="utf-8")
        git(self.repo, "add", "feature.txt")
        git(self.repo, "commit", "-m", "broken feature")
        self.service.add("parked")
        self.service.verify_error = "tests failed: test_real_failure"

        result = reverify_parked("exp", service=self.service, issue_is_open=lambda _n: True)

        self.assertTrue(result["verdict"].startswith("still_blocked:"))
        self.assertIn("test_real_failure", result["verdict"])
        experiment = self.service.get_experiment("exp")
        self.assertEqual(experiment.status, "needs_review")
        # The parking evidence is restored, so the experiment stays eligible
        # once main moves on.
        self.assertEqual(experiment.error, "timing check failed closed: host failure")
        self.assertIsNone(experiment.test_output)
        self.assertIsNone(experiment.diff_text)
        self.assertIsNone(experiment.worktree_path)
        git(self.repo, "checkout", "main")
        (self.repo / "other.txt").write_text("x\n", encoding="utf-8")
        git(self.repo, "add", "other.txt")
        git(self.repo, "commit", "-m", "main moves")
        git(self.repo, "push", "origin", "main")
        self.assertEqual(parked_candidates(self.service, git(self.repo, "rev-parse", "main")), ["exp"])

    def test_verifier_routes_soft_findings_to_the_reviewer(self) -> None:
        self._parked_feature_branch()
        reverify_parked("exp", service=self.service, issue_is_open=lambda _n: True)
        self.assertEqual(self.service.verify_kwargs, [{"soft_findings_to_reviewer": True}])

    def test_rebases_onto_fetched_origin_main_not_stale_local_main(self) -> None:
        self._parked_feature_branch()
        # A merge lands on GitHub; the local main does not know it yet.
        other = Path(self.tmp.name) / "other"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.remote), str(other)], check=True)
        git(other, "config", "user.email", "o@example.invalid")
        git(other, "config", "user.name", "Other")
        (other / "merged.txt").write_text("merged\n", encoding="utf-8")
        git(other, "add", "merged.txt")
        git(other, "commit", "-m", "merged on GitHub")
        git(other, "push", "origin", "main")
        remote_main = git(other, "rev-parse", "HEAD")
        self.assertNotEqual(git(self.repo, "rev-parse", "main"), remote_main)

        result = reverify_parked("exp", service=self.service, issue_is_open=lambda _n: True)

        self.assertEqual(result["verdict"], "passes_now")
        self.assertEqual(result["main_commit_sha"], remote_main)
        pushed = git(self.repo, "ls-remote", "origin", "refs/heads/parked").split()[0]
        self.assertEqual(git(self.repo, "rev-parse", f"{pushed}~1"), remote_main)

    def test_pass_reaches_the_adoption_queue_and_review_is_not_skipped(self) -> None:
        self._parked_feature_branch()
        reverify_parked("exp", service=self.service, issue_is_open=lambda _n: True)
        paid_client = MagicMock()
        initiative = NexusInitiativeService(
            db_path=str(Path(self.tmp.name) / "runs.db"),
            experiment_service=self.service,
            controller=SimpleNamespace(handle_issue=paid_client),
            daily_budget=5,
            reflection_sweep=lambda **kwargs: None,
        )
        try:
            candidates = initiative._integration_candidates()
        finally:
            initiative.close()
        # The pass is a normal adoption candidate: adoption (with its paid
        # review) has not started, and nothing was integrated or reviewed here.
        self.assertEqual([(c.kind, c.ref) for c in candidates], [(KIND_INTEGRATION, "exp")])
        self.assertEqual(candidates[0].payload["adoption_status"], "not_started")
        paid_client.assert_not_called()

    def test_planner_shows_ready_only_while_adoption_is_pending(self) -> None:
        row = {"title": "T", "status": "succeeded", "error": "",
               "reverification_verdict": "passes_now", "adoption_reason": ""}
        for status in ("", "not_started", "queued", "adopting"):
            self.assertIn("ready, no paid start needed", _history_line({**row, "adoption_status": status}))
        failed = _history_line({**row, "adoption_status": "failed", "adoption_reason": "Review by codex blocked"})
        self.assertNotIn("ready, no paid start needed", failed)
        self.assertIn("blocked by review", failed)

    def test_tick_with_flag_off_never_reverifies(self) -> None:
        initiative = NexusInitiativeService(
            db_path=str(Path(self.tmp.name) / "initiative.db"),
            experiment_service=self.service,
            controller=SimpleNamespace(handle_issue=MagicMock()),
            daily_budget=5,
            reflection_sweep=lambda **kwargs: None,
        )
        try:
            with patch.dict("os.environ", {"NEXUS_REVERIFY_PARKED": "off"}), \
                    patch("modules.experiment_service.reverify.reverify_parked") as run, \
                    patch.object(initiative, "_paused", return_value=False), \
                    patch.object(initiative, "_sweep_stalled_experiments"), \
                    patch.object(initiative, "enforce_issue_approvals"), \
                    patch.object(initiative, "busy_reason", return_value=None), \
                    patch.object(initiative, "_stale_service_revisions", return_value=None), \
                    patch.object(initiative, "candidates", return_value=[]):
                self.assertEqual(initiative.tick().decision, "idle")
            run.assert_not_called()
        finally:
            initiative.close()

    def _parked_feature_branch(self) -> None:
        git(self.repo, "checkout", "-b", "parked")
        (self.repo / "feature.txt").write_text("done\n", encoding="utf-8")
        git(self.repo, "add", "feature.txt")
        git(self.repo, "commit", "-m", "feature")
        git(self.repo, "push", "-u", "origin", "parked")
        git(self.repo, "checkout", "main")
        self.service.add("parked")


if __name__ == "__main__":
    unittest.main()
