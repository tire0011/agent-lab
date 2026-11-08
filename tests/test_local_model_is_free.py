"""Work done only by the local model stays out of the daily budget (#541)."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from modules.mailbox.processing import _process_setting
from modules.nexus_planner import DEFAULT_MISSION, planning_prompt
from modules.run_store import init_db
from tests.test_nexus_initiative import InitiativeFixture

ISSUES = [
    {"number": n, "author": {"login": "tire0011"}, "title": f"Issue {n}", "body": ""} for n in (1, 2, 3)
]


class LocalModelIsFreeTests(unittest.TestCase):
    def _service(self, *, azure: bool, experiment_during_dispatch: bool = False):
        fx = InitiativeFixture()
        controller = fx.controller
        controller.orchestrator = SimpleNamespace(_azure_drives_controller=lambda: azure)
        handle = controller.handle_issue

        def handle_issue(number, **kwargs):
            handle(number, **kwargs)
            with sqlite3.connect(fx.db_path) as conn:
                conn.execute(
                    "INSERT INTO github_experiment_links (experiment_id, issue_number) VALUES (?, ?)",
                    (f"e{number}", number),
                )
                if experiment_during_dispatch:
                    conn.execute(
                        "INSERT INTO experiments (id, goal, status, created_at) VALUES (?, 'g', 'running', ?)",
                        (f"e{number}", datetime.now(timezone.utc).isoformat()),
                    )

        controller.handle_issue = handle_issue
        svc = fx.service(daily_budget=1, issue_lister=lambda: ISSUES)
        self.addCleanup(svc.close)
        return fx, svc

    def test_a_local_only_dispatch_is_free(self):
        fx, svc = self._service(azure=False)
        for _ in range(3):
            self.assertTrue(svc.tick().acted)
        self.assertEqual(fx.controller.issues, [1, 2, 3])
        self.assertEqual(svc._spent_today(), 0)

    def test_an_azure_controller_is_paid(self):
        _fx, svc = self._service(azure=True)
        self.assertTrue(svc.tick().acted)
        self.assertEqual(svc.tick().decision, "budget_exhausted")

    def test_a_coding_experiment_is_paid(self):
        _fx, svc = self._service(azure=False, experiment_during_dispatch=True)
        self.assertTrue(svc.tick().acted)
        self.assertEqual(svc.tick().decision, "budget_exhausted")

    def test_a_free_registry_delegation_is_free_but_a_paid_one_is_not(self):
        import json
        fx, svc = self._service(azure=False)
        since = "2026-01-01T00:00:00"
        with sqlite3.connect(fx.db_path) as conn:
            conn.execute("CREATE TABLE nexus_task_events (kind TEXT, content TEXT, created_at TEXT)")
            free = {"tool": "delegate", "provider_used": "free_tier", "result": {"metadata": {"cost": "free"}}}
            conn.execute("INSERT INTO nexus_task_events VALUES ('delegate', ?, '2026-06-01')", (json.dumps(free),))
        self.assertFalse(svc._dispatch_was_paid(since))
        with sqlite3.connect(fx.db_path) as conn:
            paid = {"tool": "delegate", "provider_used": "azure_openai", "result": {"metadata": {}}}
            conn.execute("INSERT INTO nexus_task_events VALUES ('delegate', ?, '2026-06-01')", (json.dumps(paid),))
        self.assertTrue(svc._dispatch_was_paid(since))

    def test_a_controller_of_unknown_kind_is_paid(self):
        fx = InitiativeFixture()
        svc = fx.service(daily_budget=1, issue_lister=lambda: ISSUES)
        self.addCleanup(svc.close)
        self.assertTrue(svc.tick().acted)
        self.assertEqual(svc._spent_today(), 1)


class PlannerMissionTests(unittest.TestCase):
    def test_default_mission_names_the_free_local_model_and_no_size_limit(self):
        prompt = planning_prompt(ISSUES, budget_left=3)
        self.assertIn(DEFAULT_MISSION, prompt)
        self.assertIn("local model is free", prompt)
        self.assertNotIn("small enough", prompt)
        self.assertIn("Paid starts left in your daily budget: 3", prompt)

    def test_the_owner_mission_replaces_the_default(self):
        prompt = planning_prompt(ISSUES, mission="Focus on the local model.")
        self.assertTrue(prompt.startswith("Focus on the local model."))
        self.assertNotIn(DEFAULT_MISSION, prompt)
        self.assertIn('"issue": <number>', prompt)  # the answer format stays

    def test_mailbox_sets_and_resets_the_mission(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = init_db(str(Path(tmp.name) / "agent.db"))
        self.addCleanup(conn.close)

        def msg(body):
            return SimpleNamespace(meta={"key": "planner_mission"}, body=body, file_path=Path("inbox/x.md"))

        self.assertIn("now set", _process_setting(msg("Do more locally."), Path("."), conn, 1)[0])
        row = conn.execute("SELECT value FROM service_settings WHERE key='nexus.planner_mission'").fetchone()
        self.assertEqual(row[0], "Do more locally.")
        self.assertIn("default", _process_setting(msg("default"), Path("."), conn, 1)[0])
        self.assertIsNone(conn.execute("SELECT 1 FROM service_settings WHERE key='nexus.planner_mission'").fetchone())


if __name__ == "__main__":
    unittest.main()
