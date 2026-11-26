"""Old logs are removed by the tick (owner 2026-11-26)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from modules import log_retention

OLD = time.time() - 20 * 86400


class LogRetentionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.logs = self.root / "experiment-logs"
        self.logs.mkdir()
        self.db = str(self.root / "agent.db")
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE experiments (id TEXT, status TEXT)")
            conn.executemany("INSERT INTO experiments VALUES (?, ?)",
                             [("a" * 32, "succeeded"), ("b" * 32, "needs_review"), ("c" * 32, "implementing")])

    def log(self, name, old=True):
        path = self.logs / name
        path.write_text("{}\n")
        if old:
            os.utime(path, (OLD, OLD))
        return path

    def test_old_logs_go_but_running_and_parked_experiments_keep_theirs(self):
        done, lock = self.log("a" * 32 + ".jsonl"), self.log("." + "a" * 32 + ".lock")
        parked, running = self.log("b" * 32 + ".jsonl"), self.log("c" * 32 + ".jsonl")
        task, fresh = self.log("task-123.jsonl"), self.log("d" * 32 + ".jsonl", old=False)
        (self.logs / "test-runs").mkdir()
        self.assertEqual(log_retention.prune_experiment_logs(self.logs, self.db), 3)
        self.assertFalse(done.exists() or lock.exists() or task.exists())
        self.assertTrue(parked.exists() and running.exists() and fresh.exists())
        self.assertTrue((self.logs / "test-runs").is_dir())

    def test_without_a_database_no_experiment_log_is_removed_blind(self):
        done = self.log("a" * 32 + ".jsonl")
        self.assertEqual(log_retention.prune_experiment_logs(self.logs, str(self.root / "none.db")), 0)
        self.assertTrue(done.exists())

    def test_vibe_sessions_older_than_a_week_go(self):
        unified = self.root / ".vibe" / "logs" / "session" / "unified"
        old, new = unified / "old-session", unified / "new-session"
        for d in (old, new):
            (d / "chunks").mkdir(parents=True)
        os.utime(old, (OLD, OLD))
        self.assertEqual(log_retention.prune_vibe_logs(self.root), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_switched_off_in_test_runs(self):
        self.assertIsNone(log_retention.prune_all(str(self.root), self.db))


if __name__ == "__main__":
    unittest.main()
