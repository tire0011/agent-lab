import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.gate_preflight import PreflightResult, preflight
from modules.nexus_initiative import (
    DECISION_PREFLIGHT_BLOCKED, Candidate, InitiativeDecision, KIND_ISSUE,
)
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

    def test_localizer_failure_is_skipped_not_raised(self):
        with patch("modules.gate_preflight.likely_tests", side_effect=OSError("walk failed")):
            result = preflight("modules/example.py", self.repo)
        self.assertEqual(result.status, "skipped")
        self.assertIn("OSError", result.reason)


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

    def test_preflight_exception_fails_open_and_launcher_runs(self):
        fixture = InitiativeFixture()
        self.addCleanup(shutil.rmtree, fixture.tmp, True)
        service = fixture.service(
            issue_lister=lambda: [{"number": 740, "author": {"login": "tire0011"},
                                   "title": "Gate", "body": "tests/test_x.py"}],
            gate_preflight=unittest.mock.Mock(side_effect=OSError("localizer broke")),
        )
        self.addCleanup(service.close)
        self.assertEqual(service.tick().decision, "dispatched")
        self.assertEqual(fixture.controller.issues, [740])

    def test_failed_comment_retries_and_marks_only_after_success(self):
        fixture = InitiativeFixture()
        self.addCleanup(shutil.rmtree, fixture.tmp, True)
        attempts = []

        def comment(number, body):
            attempts.append((number, body))
            if len(attempts) == 1:
                raise RuntimeError("before delivery")

        service = fixture.service(issue_commenter=comment)
        self.addCleanup(service.close)
        chosen = Candidate(KIND_ISSUE, "12", "x", "")
        result = PreflightResult("blocked", failing_ids=("tests/test_x.py::test_x",),
                                 signature="comment-same")
        service._comment_preflight_block(chosen, result)
        self.assertEqual(service._action("preflight-comment:12:comment-same")["status"], "pending")
        service._comment_preflight_block(chosen, result)
        service._comment_preflight_block(chosen, result)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(service._action("preflight-comment:12:comment-same")["status"], "done")

    def test_ambiguous_comment_is_reconciled_without_duplicate(self):
        fixture = InitiativeFixture()
        self.addCleanup(shutil.rmtree, fixture.tmp, True)
        remote = []

        def accepted_then_timeout(_number, body):
            remote.append({"body": body})
            raise TimeoutError("response lost")

        service = fixture.service(issue_commenter=accepted_then_timeout,
                                  issue_comments_lister=lambda _number: list(remote))
        chosen = Candidate(KIND_ISSUE, "13", "x", "")
        result = PreflightResult("blocked", signature="ambiguous")
        service._comment_preflight_block(chosen, result)
        service.close()  # Simulate a process crash/restart after the ambiguous write.
        service = fixture.service(issue_commenter=accepted_then_timeout,
                                  issue_comments_lister=lambda _number: list(remote))
        self.addCleanup(service.close)
        service._comment_preflight_block(chosen, result)
        self.assertEqual(len(remote), 1)
        self.assertEqual(service._action("preflight-comment:13:ambiguous")["status"], "done")

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

    def test_ambiguous_host_blocker_create_is_reconciled_and_updated(self):
        fixture = InitiativeFixture()
        self.addCleanup(shutil.rmtree, fixture.tmp, True)
        remote = []
        updates = []

        def create(title, body):
            remote.append({"number": 901, "title": title, "body": body})
            raise TimeoutError("accepted but response lost")

        service = fixture.service(issue_lister=lambda: list(remote), issue_creator=create,
                                  issue_updater=lambda *args: updates.append(args))
        result = PreflightResult("blocked", failing_ids=("tests/test_x.py::test_x",),
                                 signature="ambiguous-create")
        for number in (1, 2):
            service._record(InitiativeDecision(
                DECISION_PREFLIGHT_BLOCKED,
                detail='{"signature": "ambiguous-create"}',
                candidate=Candidate(KIND_ISSUE, str(number), "", ""),
                tick_id=f"ambiguous-{number}",
            ))
        service._maybe_open_host_blocker(result)
        service.close()  # Pending marker survives a crash before local confirmation.
        service = fixture.service(issue_lister=lambda: list(remote), issue_creator=create,
                                  issue_updater=lambda *args: updates.append(args))
        self.addCleanup(service.close)
        service._maybe_open_host_blocker(result)
        service._maybe_open_host_blocker(result)
        self.assertEqual(len(remote), 1)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], 901)
        self.assertEqual(service._action("host-blocker:ambiguous-create")["status"], "done")


if __name__ == "__main__":
    unittest.main()
