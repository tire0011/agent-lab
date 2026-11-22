"""Weekly update of the coding-agent CLIs (owner rule 2026-11-22)."""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from modules import cli_updates

FAKE = textwrap.dedent("""\
    #!{python}
    import os, sys
    state = os.path.join(os.path.dirname(sys.argv[0]), "{name}.version")
    v = open(state).read().strip() if os.path.exists(state) else "1.0"
    if sys.argv[1:] == ["--version"]:
        print("{name} " + v)
    elif "{fail}" == "yes":
        print("network down", file=sys.stderr); sys.exit(1)
    else:
        open(state, "w").write("{new}")
""")


class CliUpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name)
        self.db = str(self.bin / "agent.db")
        env = mock.patch.dict(os.environ, {"PATH": str(self.bin), "NEXUS_CLI_UPDATE": "1"})
        env.start()
        self.addCleanup(env.stop)
        local = mock.patch.object(cli_updates, "_LOCAL_BIN", self.bin / "none")
        local.start()
        self.addCleanup(local.stop)

    def fake(self, name, *, new="2.0", fail="no", updater=None):
        for tool in {name, updater or name}:
            path = self.bin / tool
            path.write_text(FAKE.format(python=sys.executable, name=name, new=new, fail=fail))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def test_updates_installed_clis_once_a_week_and_reports_versions(self):
        self.fake("claude")
        self.fake("codex", new="1.0")
        now = datetime(2026, 11, 22, tzinfo=timezone.utc)
        summary = cli_updates.update_all(self.db, now)
        self.assertIn("claude claude 1.0 -> claude 2.0", summary)
        self.assertIn("codex unchanged", summary)
        self.assertNotIn("vibe", summary)  # not installed: skipped
        self.assertIsNone(cli_updates.update_all(self.db, now + timedelta(days=6)))
        self.assertIsNotNone(cli_updates.update_all(self.db, now + timedelta(days=7)))

    def test_a_failed_update_is_reported_and_still_counts_as_this_weeks_run(self):
        self.fake("claude", fail="yes")
        now = datetime(2026, 11, 22, tzinfo=timezone.utc)
        summary = cli_updates.update_all(self.db, now)
        self.assertIn("claude update failed (network down), still claude 1.0", summary)
        self.assertIsNone(cli_updates.update_all(self.db, now + timedelta(hours=1)))

    def test_switched_off_does_nothing(self):
        self.fake("claude")
        with mock.patch.dict(os.environ, {"NEXUS_CLI_UPDATE": "0"}):
            self.assertIsNone(cli_updates.update_all(self.db))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
