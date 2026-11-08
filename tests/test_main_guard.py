"""A Nexus merge that turned main red is reverted."""

from __future__ import annotations

import json
import subprocess
import unittest

from modules.main_guard import guard_main


class FakeRunner:
    def __init__(self, message, conclusion="failure", fail_on=None):
        self.message, self.conclusion, self.fail_on = message, conclusion, fail_on
        self.calls = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        out, code = "", 0
        if argv[:2] == ["gh", "api"] and argv[2].endswith("commits/main"):
            out = json.dumps({"sha": "abc123", "commit": {"message": self.message}})
        elif argv[:2] == ["gh", "api"]:
            out = json.dumps({"workflow_runs": [
                {"head_sha": "abc123", "status": "completed", "conclusion": self.conclusion}
            ]})
        if self.fail_on and argv[:2] == self.fail_on:
            code = 1
        return subprocess.CompletedProcess(argv, code, out, "")

    def ran(self, *prefix):
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


NEXUS = "Do something (#410)\n\nCloses #410\n\nNexus-Experiment: 58cb72de"


class MainGuardTests(unittest.TestCase):
    def test_red_nexus_merge_is_reverted_and_its_issue_reopened(self):
        runner = FakeRunner(NEXUS)
        message = guard_main("/repo", runner=runner)
        self.assertIn("Reverted Nexus merge abc123", message)
        self.assertTrue(runner.ran("git", "revert", "--no-edit", "abc123"))
        self.assertTrue(runner.ran("git", "push", "origin", "HEAD:main"))
        self.assertTrue(runner.ran("gh", "issue", "reopen", "410"))

    def test_green_main_is_left_alone(self):
        runner = FakeRunner(NEXUS, conclusion="success")
        self.assertIsNone(guard_main("/repo", runner=runner))
        self.assertFalse(runner.ran("git", "revert"))

    def test_a_human_commit_is_never_reverted(self):
        runner = FakeRunner("Human fix (#466)")
        self.assertIsNone(guard_main("/repo", runner=runner))
        self.assertFalse(runner.ran("git", "revert"))

    def test_a_failed_revert_is_reported(self):
        runner = FakeRunner(NEXUS, fail_on=["git", "push"])
        self.assertIn("automatic revert failed", guard_main("/repo", runner=runner))
        self.assertFalse(runner.ran("gh", "issue", "reopen"))


if __name__ == "__main__":
    unittest.main()
