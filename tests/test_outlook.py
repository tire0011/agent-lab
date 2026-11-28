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

    def test_each_proposal_becomes_its_own_issue(self):
        report = ("Gefunden: Qwen3-Coder 4B.\n\n```json\n"
                  '{"proposals": [{"title": "Try a 4B coder model", "body": "Link, gain, measure."},'
                  ' {"title": "", "body": "no title"}]}\n```')
        line = self.run_outlook(lambda p, root: report)
        self.assertIn("1 proposal issue(s)", line)
        self.assertEqual(self.issues[0], ("Ausblick Nexus 2026-11-28", "Gefunden: Qwen3-Coder 4B."))
        self.assertEqual(self.issues[1][0], "Outlook: Try a 4B coder model")
        self.assertIn('From the outlook "Ausblick Nexus 2026-11-28"', self.issues[1][1])
        self.assertIn("```json", outlook.outlook_prompt("setup", None))

    def test_a_broken_json_block_opens_only_the_report(self):
        text, proposals = outlook.parse_proposals("Bericht\n```json\n{broken\n```")
        self.assertEqual((text, proposals), ("Bericht", []))
        self.assertEqual(outlook.parse_proposals("Nur Text"), ("Nur Text", []))

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
