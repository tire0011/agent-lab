"""#614: changed quality gates create one durably linked, free verification run."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules.experiment_service import ExperimentService
from modules.nexus_initiative import NexusInitiativeService


class CreatingController:
    def __init__(self, service: ExperimentService, *, fail_after_create: bool = False) -> None:
        self.service = service
        self.created: list[str] = []
        self.fail_after_create = fail_after_create

    def handle_issue(self, number: int, *, title: str, body: str, rechecked_experiment_id=None):
        experiment = self.service.create_experiment(
            f"GitHub Issue #{number}: {title}\n\n{body}",
            source_type="issue", source_id=str(number),
            rechecked_experiment_id=rechecked_experiment_id,
        )
        self.created.append(experiment.id)
        with self.service._lock, self.service._conn:
            self.service._conn.execute(
                "INSERT OR REPLACE INTO github_experiment_links "
                "(experiment_id, issue_number) VALUES (?, ?)",
                (experiment.id, number),
            )
        if self.fail_after_create:
            raise RuntimeError("binding failed")


class GateRequeueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "state.db")
        self.service = ExperimentService(db_path=self.db, repo_root=self.tmp.name)
        self.addCleanup(self.service.close)
        with self.service._lock, self.service._conn:
            self.service._conn.execute(
                "CREATE TABLE github_experiment_links "
                "(experiment_id TEXT PRIMARY KEY, issue_number INTEGER)"
            )
            for exp_id, reason in (
                ("quality", "quality evaluation: tests only"),
                ("blocked", "quality evaluation: still bad"),
                ("review", "review blocked this change"),
            ):
                self.service._conn.execute(
                    "INSERT INTO experiments (id, goal, status, created_at, ended_at, error) "
                    "VALUES (?, ?, 'needs_review', datetime('now'), datetime('now'), ?)",
                    (exp_id, f"GitHub Issue #{1 if exp_id == 'quality' else 2}: test", reason),
                )
            self.service._conn.execute(
                "INSERT INTO github_experiment_links (experiment_id, issue_number) "
                "VALUES ('quality', 1)"
            )

    def initiative(self, controller):
        svc = NexusInitiativeService(
            db_path=self.db, experiment_service=self.service, controller=controller,
            issue_lister=lambda: [
                {"number": 1, "title": "tests", "body": "", "state": "open"},
                {"number": 2, "title": "other", "body": "", "state": "open"},
            ], idea_lister=lambda: [], adoption_service=None, daily_budget=0,
        )
        self.addCleanup(svc.close)
        return svc

    @staticmethod
    def verdicts(_store):
        return [
            {"id": "quality", "issue_number": 1, "old_reason": "quality evaluation: tests only", "new_verdict": "would_pass"},
            {"id": "blocked", "issue_number": 2, "old_reason": "quality evaluation: still bad", "new_verdict": "still_blocked"},
            {"id": "review", "issue_number": 2, "old_reason": "review blocked this change", "new_verdict": "would_pass"},
        ]

    def test_only_eligible_source_is_queued_once_and_is_not_an_attempt(self):
        controller = CreatingController(self.service)
        initiative = self.initiative(controller)
        with patch("modules.nexus_initiative.gate_recheck", self.verdicts), patch.object(
            initiative, "_execution_allowed", return_value=SimpleNamespace(allowed=True, reason="")
        ):
            self.assertEqual(initiative.tick().decision, "dispatched")
            self.assertEqual(len(controller.created), 1)
            created = self.service.get_experiment(controller.created[0])
            self.assertEqual(created.rechecked_experiment_id, "quality")
            self.assertEqual(initiative.tick().decision, "busy")
            self.assertEqual(len(controller.created), 1)
            self.service._force_status(
                created.id, "needs_review", ended_at="2026-01-01T00:00:00+00:00",
                error="quality evaluation: failed again",
            )
            _blocked, exhausted = initiative._issue_attempts()
            self.assertNotIn(1, exhausted)

    def test_running_experiment_suppresses_requeue(self):
        with self.service._lock, self.service._conn:
            self.service._conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at) VALUES "
                "('running', 'GitHub Issue #1: live', 'implementing', datetime('now'))"
            )
        controller = CreatingController(self.service)
        initiative = self.initiative(controller)
        self.assertEqual(initiative.tick().decision, "busy")
        self.assertEqual(controller.created, [])

    def test_failure_before_creation_does_not_consume_source(self):
        class FailingController:
            def handle_issue(self, *args, **kwargs):
                raise RuntimeError("orchestration failed before create")

        with self.assertRaises(RuntimeError):
            FailingController().handle_issue(
                1, title="tests", body="", rechecked_experiment_id="quality"
            )
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertIsNone(conn.execute(
            "SELECT id FROM experiments WHERE rechecked_experiment_id='quality'"
        ).fetchone())

    def test_failure_after_creation_keeps_link_and_free_attempt_marker(self):
        controller = CreatingController(self.service, fail_after_create=True)
        with self.assertRaises(RuntimeError):
            controller.handle_issue(1, title="tests", body="", rechecked_experiment_id="quality")
        created = self.service.get_experiment(controller.created[0])
        self.assertEqual(created.rechecked_experiment_id, "quality")
        activity = self.service.list_activity(experiment_id=created.id)
        self.assertIn("rechecked_experiment_id=quality", activity[0]["message"])


if __name__ == "__main__":
    unittest.main()
