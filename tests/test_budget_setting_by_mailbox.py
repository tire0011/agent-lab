"""The daily budget is set by a mailbox `setting` message, not by editing code."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.mailbox.processing import _process_setting
from modules.run_store import init_db
from tests.test_nexus_initiative import InitiativeFixture


def _msg(**meta):
    return SimpleNamespace(meta=meta, file_path=Path("inbox/x.md"), body="")


class BudgetSettingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "agent.db")
        self.conn = init_db(self.db)
        self.addCleanup(self.conn.close)

    def test_setting_message_stores_the_budget(self):
        reply, _ = _process_setting(_msg(key="daily_budget", value="5"), Path("."), self.conn, 1)
        self.assertIn("now 5", reply)
        row = self.conn.execute("SELECT value FROM service_settings WHERE key='nexus.daily_budget'").fetchone()
        self.assertEqual(row[0], "5")

    def test_unknown_keys_and_bad_values_are_refused(self):
        self.assertIn("Unknown setting", _process_setting(_msg(key="shell", value="1"), Path("."), self.conn, 1)[0])
        self.assertIn("integer", _process_setting(_msg(key="daily_budget", value="x"), Path("."), self.conn, 1)[0])
        self.assertIn("between", _process_setting(_msg(key="daily_budget", value="999"), Path("."), self.conn, 1)[0])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM service_settings WHERE key='nexus.daily_budget'").fetchone())

    def test_initiative_uses_the_stored_budget_over_the_default(self):
        fx = InitiativeFixture()
        with sqlite3.connect(fx.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS service_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO service_settings VALUES ('nexus.daily_budget', '3', 'now')")
        svc = fx.service(daily_budget=15)
        self.addCleanup(svc.close)
        self.assertEqual(svc.daily_budget, 3)

    def test_without_a_setting_the_default_holds(self):
        fx = InitiativeFixture()
        svc = fx.service(daily_budget=15)
        self.addCleanup(svc.close)
        self.assertEqual(svc.daily_budget, 15)


if __name__ == "__main__":
    unittest.main()
