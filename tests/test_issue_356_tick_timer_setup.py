"""#356: the refresh installs and enables the initiative tick timer.

The units existed since #353, but nothing installed them on the VM.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class TickTimerSetupTests(unittest.TestCase):
    def test_refresh_installs_and_enables_the_timer(self):
        refresh = (REPO_ROOT / "scripts" / "nexus-refresh.ps1").read_text(encoding="utf-8")
        self.assertIn("setup-nexus-tick.sh", refresh)
        self.assertIn("'nexus-tick.timer'", refresh)
        setup = (REPO_ROOT / "scripts" / "setup-nexus-tick.sh").read_text(encoding="utf-8")
        self.assertIn("systemctl enable --now nexus-tick.timer", setup)
        for unit in ("nexus-tick.service", "nexus-tick.timer"):
            self.assertIn(unit, setup)
            self.assertTrue((REPO_ROOT / "deploy" / unit).is_file())


if __name__ == "__main__":
    unittest.main()
