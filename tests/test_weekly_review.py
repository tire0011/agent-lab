"""Weekly open review of Nexus by one of its AIs (owner rule 2026-11-22)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from modules import weekly_review

NOW = datetime(2026, 11, 29, 10, tzinfo=timezone.utc)


class WeeklyReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "agent.db")
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE experiments (id TEXT, status TEXT, implementation_provider TEXT, "
                         "implementation_runtime_ms INTEGER, error TEXT, created_at TEXT)")
            conn.execute("INSERT INTO experiments VALUES ('e1', 'succeeded', 'codex', 60000, NULL, ?)",
                         ((NOW - timedelta(days=1)).isoformat(),))
        self.repo = tmp.name
        self.issues = []
        env = mock.patch.dict(os.environ, {"NEXUS_WEEKLY_REVIEW": "1"})
        env.start()
        self.addCleanup(env.stop)

    def open_issue(self, title, body):
        self.issues.append((title, body))

    def run_review(self, runners, now=NOW):
        return weekly_review.run(self.db, self.repo, self.open_issue, now=now, runners=runners)

    def test_an_open_review_becomes_an_issue_once_a_week_and_models_take_turns(self):
        prompts = {}

        def runner(name):
            def run(prompt, root):
                prompts[name] = prompt
                return f"Bericht von {name}"
            return run

        runners = {n: runner(n) for n in ("codex", "claude", "azure")}
        self.assertIn("by codex", self.run_review(runners))
        self.assertEqual(self.issues[0][0], "Wochenreview Nexus 2026-11-29")
        self.assertIn("Geprueft von: codex", self.issues[0][1])
        self.assertIn("nobody can tell you in advance where something is wrong", prompts["codex"])
        self.assertIn("succeeded | codex | 1", prompts["codex"])
        self.assertIn("run read-only shell commands", prompts["codex"])
        self.assertIsNone(self.run_review(runners, NOW + timedelta(days=6)))
        self.assertIn("by claude", self.run_review(runners, NOW + timedelta(days=7)))
        self.assertIn("LAST WEEK'S REPORT:\nBericht von codex", prompts["claude"])
        self.assertIn("read files", prompts["claude"])

    def test_azure_stands_in_and_a_total_failure_is_reported_once(self):
        def down(prompt, root):
            raise RuntimeError("limit")
        line = self.run_review({"codex": down, "claude": down, "azure": lambda p, r: "Nur Startpaket"})
        self.assertIn("by azure", line)
        failed = self.run_review({"codex": down}, NOW + timedelta(days=7))
        self.assertIn("Weekly review failed: codex: limit", failed)
        self.assertIsNone(self.run_review({"codex": down}, NOW + timedelta(days=7, hours=1)))

    def test_switched_off_does_nothing(self):
        with mock.patch.dict(os.environ, {"NEXUS_WEEKLY_REVIEW": "0"}):
            self.assertIsNone(self.run_review({"codex": lambda p, r: "x"}))
        self.assertEqual(self.issues, [])


if __name__ == "__main__":
    unittest.main()
