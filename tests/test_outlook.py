"""Every two weeks Nexus looks outward (owner rule 2026-11-28)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from modules import outlook
from modules.claude_worker import ClaudeWorker

NOW = datetime(2026, 11, 28, 10, tzinfo=timezone.utc)


class OutlookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "agent.db")
        self.issues = []
        env = mock.patch.dict(os.environ, {"NEXUS_OUTLOOK": "1"})
        env.start()
        self.addCleanup(env.stop)

    def run_outlook(self, runner, now=NOW):
        return outlook.run(self.db, ".", lambda t, b: self.issues.append((t, b)), now=now, runner=runner)

    def test_an_outlook_every_two_weeks_becomes_an_issue(self):
        prompts = []
        runner = lambda p, root: prompts.append(p) or "Neu: ein kleineres Modell"
        self.assertIn("Ausblick Nexus 2026-11-28", self.run_outlook(runner))
        self.assertEqual(self.issues[0][1], "Neu: ein kleineres Modell")
        self.assertIn("Search the web", prompts[0])
        self.assertIn("RTX 2000 Ada", prompts[0])
        self.assertIsNone(self.run_outlook(runner, NOW + timedelta(days=13)))
        self.run_outlook(runner, NOW + timedelta(days=14))
        self.assertIn("LAST OUTLOOK:\nNeu: ein kleineres Modell", prompts[1])

    def test_a_failure_is_reported_once_and_switched_off_does_nothing(self):
        def down(p, root):
            raise RuntimeError("limit")
        self.assertIn("Outlook failed: limit", self.run_outlook(down))
        self.assertIsNone(self.run_outlook(down, NOW + timedelta(days=1)))
        with mock.patch.dict(os.environ, {"NEXUS_OUTLOOK": "0"}):
            self.assertIsNone(self.run_outlook(down, NOW + timedelta(days=30)))

    def test_the_web_mode_adds_only_search_and_fetch(self):
        cmd = ClaudeWorker(claude_bin="claude", web=True)._build_command("read-only")
        self.assertIn("Read,Glob,Grep,WebSearch,WebFetch", cmd)
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "WebSearch,WebFetch")
        plain = ClaudeWorker(claude_bin="claude")._build_command("read-only")
        self.assertNotIn("--allowedTools", plain)
        self.assertNotIn("WebSearch", " ".join(plain))


if __name__ == "__main__":
    unittest.main()
