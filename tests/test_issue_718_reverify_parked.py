"""#718: parked timing-check work is retried without another coding start."""

from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.experiment_service.reverify import list_reverifications, reverify_parked
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

    def _verify(self, experiment_id: str, worktree: str):
        self.verify_calls += 1
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
        self.service.add("parked")

        result = reverify_parked("exp", service=self.service, issue_is_open=lambda _n: True)

        self.assertEqual(result["verdict"], "rebase_conflict")
        self.assertEqual(git(self.repo, "rev-parse", "parked"), parked_sha)
        self.assertEqual(self.service.verify_calls, 0)

    def test_closed_issue_is_not_run_or_recorded(self) -> None:
        self.service.add("missing")
        self.assertIsNone(
            reverify_parked("exp", service=self.service, issue_is_open=lambda _n: False)
        )
        self.assertEqual(list_reverifications(self.service), [])

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
        self.assertEqual(self.service.get_experiment("exp").status, "succeeded")
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


if __name__ == "__main__":
    unittest.main()
