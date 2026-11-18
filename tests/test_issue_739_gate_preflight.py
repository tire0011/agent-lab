import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.gate_preflight import PreflightResult, preflight
from modules.nexus_initiative import DECISION_PREFLIGHT_BLOCKED, InitiativeDecision
from modules.verification_env import VERIFICATION_TEST_API_KEY
from test_nexus_initiative import InitiativeFixture


class GatePreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "nexus@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Nexus test"], cwd=self.repo, check=True)

    def _commit_test(self, body: str, name: str = "test_named.py") -> str:
        tests = self.repo / "tests"
        tests.mkdir(exist_ok=True)
        path = tests / name
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=self.repo, check=True,
                       stdout=subprocess.DEVNULL)
        return f"tests/{name}"

    def test_named_failure_blocks_with_id(self):
        test_file = self._commit_test("""
            def test_bad():
                assert False, "already red on main"
        """)
        result = preflight(f"Tests: `{test_file}`", self.repo, budget_s=30)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.failing_ids, (f"{test_file}::test_bad",))
        self.assertIn("already red", result.tails[result.failing_ids[0]])

    def test_green_main_goes_ahead_unchanged(self):
        test_file = self._commit_test("def test_good():\n    assert True\n")
        result = preflight(f"run {test_file}", self.repo, budget_s=30)
        launcher = unittest.mock.Mock()
        if not result.blocked:
            launcher("unchanged")
        self.assertEqual(result.status, "green")
        launcher.assert_called_once_with("unchanged")

    def test_child_has_verification_key_and_no_github_token(self):
        test_file = self._commit_test("""
            import os
            def test_env():
                assert "GITHUB_TOKEN" not in os.environ
                assert os.environ["AGENT_API_KEY"] == "test-key"
                assert os.environ["NEXUS_TEST_CLOCK_DRIFT_MS"] == "0"
        """)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "never-pass", "AGENT_API_KEY": "production"}):
            result = preflight(test_file, self.repo, budget_s=30)
        self.assertEqual(VERIFICATION_TEST_API_KEY, "test-key")
        self.assertEqual(result.status, "green")

    def test_over_budget_is_skipped_and_does_not_block(self):
        test_file = self._commit_test("def test_bad():\n    assert False\n")
        result = preflight(test_file, self.repo, budget_s=0)
        self.assertEqual(result.status, "skipped")
        self.assertFalse(result.blocked)


class GatePreflightInitiativeTests(unittest.TestCase):
    def test_blocked_result_prevents_launcher(self):
        fixture = InitiativeFixture()
        self.addCleanup(shutil.rmtree, fixture.tmp, True)
        result = PreflightResult("blocked", ("tests/test_x.py",),
                                 ("tests/test_x.py::test_x",), {}, .1, "same")
        service = fixture.service(
            issue_lister=lambda: [{"number": 739, "author": {"login": "tire0011"},
                                   "title": "Gate", "body": "tests/test_x.py"}],
            gate_preflight=lambda *_: result,
        )
        self.addCleanup(service.close)
        decision = service.tick()
        self.assertEqual(decision.decision, DECISION_PREFLIGHT_BLOCKED)
        self.assertEqual(fixture.controller.issues, [])

    def test_same_signature_opens_one_host_blocker(self):
        fixture = InitiativeFixture()
        self.addCleanup(shutil.rmtree, fixture.tmp, True)
        created = []
        service = fixture.service(issue_creator=lambda title, body: created.append((title, body)) or 900)
        self.addCleanup(service.close)
        result = PreflightResult("blocked", failing_ids=("tests/test_x.py::test_x",), signature="same")
        for number in (1, 2):
            service._record(InitiativeDecision(
                DECISION_PREFLIGHT_BLOCKED, detail='{"signature": "same"}',
                candidate=unittest.mock.Mock(kind="open_issue", ref=str(number), reason=""),
                tick_id=f"blocked-{number}",
            ))
            service._maybe_open_host_blocker(result)
        service._maybe_open_host_blocker(result)
        self.assertEqual(len(created), 1)
        self.assertIn("host_blocker", created[0][0])


if __name__ == "__main__":
    unittest.main()
