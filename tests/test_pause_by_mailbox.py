"""The owner can pause all new work by mailbox."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.mailbox.processing import _process_setting
from modules.run_store import init_db
from tests.test_nexus_initiative import InitiativeFixture


def _set(db_path: str, value: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS service_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        conn.execute("INSERT OR REPLACE INTO service_settings VALUES ('nexus.paused', ?, 'now')", (value,))


class PauseTests(unittest.TestCase):
    def test_paused_starts_nothing_not_even_an_adoption(self):
        fx = InitiativeFixture()
        fx.add_experiment("verified", "succeeded")
        _set(fx.db_path, "1")
        svc = fx.service()
        self.addCleanup(svc.close)
        self.assertEqual(svc.tick().decision, "paused")
        self.assertEqual(svc.resume_blocked_capability().decision, "paused")
        self.assertEqual(fx.adoption.started, [])

    def test_resumed_works_again(self):
        fx = InitiativeFixture()
        fx.add_experiment("verified", "succeeded")
        _set(fx.db_path, "0")
        svc = fx.service()
        self.addCleanup(svc.close)
        self.assertTrue(svc.tick().acted)
        self.assertEqual(fx.adoption.started, ["verified"])

    def test_mailbox_setting_accepts_only_0_and_1(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = init_db(str(Path(tmp.name) / "agent.db"))
        self.addCleanup(conn.close)

        def msg(value):
            return SimpleNamespace(meta={"key": "paused", "value": value}, body="", file_path=Path("inbox/x.md"))

        self.assertIn("now 1", _process_setting(msg("1"), Path("."), conn, 1)[0])
        self.assertIn("between", _process_setting(msg("2"), Path("."), conn, 1)[0])


if __name__ == "__main__":
    unittest.main()
