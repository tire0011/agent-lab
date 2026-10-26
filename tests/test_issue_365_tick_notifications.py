"""#365: the tick comments on a watched PR when something needs attention.

Claude Code is only woken by activity on a pull request it watches.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import nexus_tick


class _Service:
    controller = None

    def __init__(self, db_path, *, decision="dispatched", on_tick=None):
        self.db_path = db_path
        self._decision = decision
        self._on_tick = on_tick

    def abandoned_work(self):
        return []

    def tick(self):
        if self._on_tick:
            self._on_tick(self.db_path)
        return SimpleNamespace(acted=False, candidate=None, decision=self._decision, detail="boom")

    def close(self):
        pass


def _db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE experiments (id TEXT, status TEXT, goal TEXT, error TEXT, ended_at TEXT)")
    db.execute("CREATE TABLE experiment_adoptions (experiment_id TEXT, status TEXT, reason TEXT, updated_at TEXT)")
    db.execute("INSERT INTO experiments VALUES ('old0000000', 'failed', 'Old', 'x', '2000-01-01T00:00:00+00:00')")
    db.commit()
    db.close()


def _ended(status, adoption=None):
    def write(path):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        db = sqlite3.connect(path)
        db.execute("INSERT INTO experiments VALUES ('abcdef1234', ?, 'GitHub Issue #9: Fix it', 'tests failed', ?)", (status, now))
        if adoption:
            db.execute("INSERT INTO experiment_adoptions VALUES ('abcdef1234', ?, 'CI red', ?)", (adoption, now))
        db.commit()
        db.close()
    return write


class TickNotificationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "agent.db")
        _db(self.db_path)
        self.calls = []

    def _run(self, service, env={"NEXUS_NOTIFY_PR": "tire0011/agent-lab-mailbox#6"}):
        with patch.dict(os.environ, env, clear=False), \
                patch.object(nexus_tick, "_build_service", return_value=service), \
                patch.object(nexus_tick, "_reconcile_stale_claims"), \
                patch.object(nexus_tick, "_gh", side_effect=lambda root, argv: self.calls.append(argv)):
            nexus_tick.main(["--db-path", self.db_path])

    def test_a_failed_experiment_is_reported_once(self):
        self._run(_Service(self.db_path, on_tick=_ended("failed")))
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]
        self.assertEqual(argv[:5], ["pr", "comment", "6", "--repo", "tire0011/agent-lab-mailbox"])
        body = argv[-1]
        self.assertIn("abcdef12", body)
        self.assertIn("GitHub Issue #9", body)
        self.assertNotIn("old00000", body)

    def test_a_failed_adoption_and_a_tick_error_are_reported(self):
        self._run(_Service(self.db_path, decision="error", on_tick=_ended("succeeded", adoption="needs_review")))
        body = self.calls[0][-1]
        self.assertIn("Adoption of experiment abcdef12 ended needs_review", body)
        self.assertIn("Tick error: boom", body)
        self.assertNotIn("Experiment abcdef12", body)

    def test_a_new_proposal_is_reported(self):
        def propose(_path):
            nexus_tick._open_proposal("/agent", "Add X", "Change Y")

        self._run(_Service(self.db_path, on_tick=propose))
        self.assertIn("New proposal issue: Add X", self.calls[-1][-1])
        # The VM's gh has no `label` command; the proposal is one issue create.
        self.assertEqual(self.calls[0][:2], ["issue", "create"])
        self.assertNotIn("label", [argv[0] for argv in self.calls])

    def test_nothing_to_report_sends_nothing(self):
        self._run(_Service(self.db_path, on_tick=_ended("succeeded")))
        self.assertEqual(self.calls, [])

    def test_no_target_sends_nothing(self):
        self._run(_Service(self.db_path, on_tick=_ended("failed")), env={"NEXUS_NOTIFY_PR": ""})
        self.assertEqual(self.calls, [])

    def test_a_failed_comment_does_not_fail_the_tick(self):
        with patch.dict(os.environ, {"NEXUS_NOTIFY_PR": "o/r#6"}), \
                patch.object(nexus_tick, "_build_service", return_value=_Service(self.db_path, on_tick=_ended("failed"))), \
                patch.object(nexus_tick, "_reconcile_stale_claims"), \
                patch.object(nexus_tick, "_gh", side_effect=RuntimeError("gh down")):
            self.assertEqual(nexus_tick.main(["--db-path", self.db_path]), 0)

    def test_the_service_points_at_the_watched_pr(self):
        unit = Path("deploy/nexus-tick.service").read_text(encoding="utf-8")
        self.assertIn("Environment=NEXUS_NOTIFY_PR=tire0011/agent-lab-mailbox#6", unit)


if __name__ == "__main__":
    unittest.main()
