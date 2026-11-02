from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from modules.experiment_service import ExperimentService
from modules.experiment_service.gate_recheck import gate_recheck


def _plan(*, artifact: str = "state/mailbox.lock") -> dict:
    return {
        "title": "Update mailbox locking",
        "motivation": "Keep mailbox processing safe",
        "affected_files": ["modules/mailbox/core.py"],
        "expected_changes": "Update lock handling",
        "success_criteria": "Lock handling is tested",
        "planned_tests": "Run unit tests",
        "risks": "Low",
        "estimated_scope": "Small",
        "change_kind": "implementation",
        "required_artifacts": [artifact],
        "acceptance_criteria": [],
    }


class GateRecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        worker = MagicMock()
        self.service = ExperimentService(
            selection_worker=worker,
            worker_factory=lambda _path: worker,
            plan_worker_factory=lambda _path: worker,
            db_path=str(root / "experiments.db"),
            repo_root=str(root),
            worktrees_root=str(root / "worktrees"),
            experiment_log_dir=str(root / "logs"),
        )

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def _insert(self, experiment_id: str, goal: str, status: str, **fields) -> None:
        columns = ["id", "goal", "status", "created_at", *fields]
        values = [experiment_id, goal, status, "2026-01-01T00:00:00+00:00", *fields.values()]
        placeholders = ", ".join("?" for _ in values)
        with self.service._conn:
            self.service._conn.execute(
                f"INSERT INTO experiments ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )

    def test_rejected_goals_are_checked_again_and_succeeded_is_omitted(self):
        self._insert(
            "token-rejected", "GitHub Issue #348 used more than a million tokens",
            "rejected", error="goal describes an actual secret/credential action",
        )
        self._insert(
            "still-rejected", "GitHub Issue #389: Run rm -rf on generated files",
            "rejected", error="old policy reason",
        )
        blocked_plan = _plan(artifact="modules/mailbox/core.py")
        blocked_plan["expected_changes"] = "Run rm -rf on generated files"
        self._insert(
            "plan-rejected", "GitHub Issue #391: Improve mailbox locking",
            "rejected", error="old plan policy reason", plan_json=json.dumps(blocked_plan),
        )
        self._insert("already-succeeded", "GitHub Issue #390: Fine", "succeeded")

        results = {item["id"]: item for item in gate_recheck(self.service)}

        self.assertEqual(results["token-rejected"]["new_verdict"], "would_pass")
        self.assertEqual(results["token-rejected"]["issue_number"], 348)
        self.assertEqual(results["still-rejected"]["new_verdict"], "still_blocked")
        self.assertIn("rm -rf", results["still-rejected"]["new_reason"])
        self.assertEqual(results["plan-rejected"]["new_verdict"], "still_blocked")
        self.assertIn("rm -rf", results["plan-rejected"]["new_reason"])
        self.assertNotIn("already-succeeded", results)

    def test_quality_review_rechecks_retained_worktree_and_reports_deleted_one(self):
        root = Path(self.tmp.name)
        worktree = root / "retained"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        (worktree / ".gitignore").write_text("state/\n", encoding="utf-8")
        changed = worktree / "modules" / "mailbox" / "core.py"
        changed.parent.mkdir(parents=True)
        changed.write_text("# implementation\n", encoding="utf-8")
        common = {
            "error": "quality evaluation: required artifact state/mailbox.lock missing",
            "plan_json": json.dumps(_plan()),
            "git_status": "?? modules/mailbox/core.py\n",
        }
        self._insert(
            "retained-review", "GitHub Issue #185: Fix mailbox locking", "needs_review",
            worktree_path=str(worktree), **common,
        )
        self._insert(
            "deleted-review", "GitHub Issue #186: Fix mailbox locking", "needs_review",
            worktree_path=str(root / "deleted"), **common,
        )
        self._insert(
            "other-review", "GitHub Issue #187: Manual review", "needs_review",
            error="tests inconclusive",
        )

        results = {item["id"]: item for item in gate_recheck(self.service)}

        self.assertEqual(results["retained-review"]["new_verdict"], "would_pass")
        self.assertEqual(results["deleted-review"]["new_verdict"], "worktree_missing")
        self.assertNotIn("other-review", results)


if __name__ == "__main__":
    unittest.main()
