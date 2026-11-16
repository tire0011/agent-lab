"""#714: a gate of Nexus' own that blocks everything on this host is switched
off, repaired and switched on again - only on evidence, only allowlisted gates."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules import gate_self_repair as gsr
from modules.run_store import init_db

HOST_REASON = (
    "timing check failed closed: error: these tests fail on this host without drift too, "
    "so there is no timing verdict: "
)


def host_failure(*tests: str) -> str:
    return HOST_REASON + ", ".join(tests or ("tests/test_api.py::ApiTests::test_secure_health",))


class FakeGitHub:
    def __init__(self) -> None:
        self.need_issues: list[tuple[str, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []

    def open_need_issue(self, title: str, body: str) -> dict:
        self.need_issues.append((title, body))
        return {"number": 900, "url": "https://example.invalid/900", "reused": False}

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def close_issue(self, number: int) -> None:
        self.closed.append(number)


class GateSelfRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "db.sqlite"))
        self.github = FakeGitHub()
        self.main_results: list[tuple[bool | None, str]] = []
        self.main_calls: list[list[str]] = []

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _repair(self, gate: str = gsr.TIMING_CHECK_GATE) -> gsr.GateSelfRepair:
        def check(_root: str, tests: list[str]) -> tuple[bool | None, str]:
            self.main_calls.append(tests)
            return self.main_results.pop(0)

        return gsr.GateSelfRepair(self.conn, repo_root=self.tmp.name, main_checks={gate: check})

    def _feed(self, repair, outcomes):
        return [
            repair.record_outcome(exp_id, error, issue_number=issue, github=self.github)
            for exp_id, issue, error in outcomes
        ]

    # Detect + switch off ---------------------------------------------------

    def test_three_same_reason_failures_and_main_failure_switch_the_gate_off(self) -> None:
        self.main_results = [(True, "fail on main without drift too: tests/test_api.py::A::b")]
        decisions = self._feed(self._repair(), [
            ("e1", 694, host_failure()), ("e2", 705, host_failure("tests/test_x.py::X::y")),
            ("e3", 709, host_failure()),
        ])
        self.assertEqual([d.action for d in decisions], ["streak", "streak", "switched_off"])
        last = decisions[-1]
        self.assertEqual(last.classification, "host_blocker:timing_check")
        self.assertEqual(last.experiment_ids, ["e1", "e2", "e3"])
        self.assertTrue(gsr.gate_is_off(self.conn, "timing_check"))
        state = gsr.gate_state(self.conn, "timing_check")
        self.assertEqual(state["env_var"], "NEXUS_TIMING_CHECK")
        self.assertEqual(state["issue_number"], 900)
        # The main check reran the tests the experiments named.
        self.assertEqual(self.main_calls, [[
            "tests/test_api.py::ApiTests::test_secure_health", "tests/test_x.py::X::y",
        ]])
        # A "Nexus needs" issue with gate, reason and the three experiment ids.
        self.assertEqual(len(self.github.need_issues), 1)
        title, body = self.github.need_issues[0]
        self.assertIn("timing_check", title)
        for text in ("`timing_check`", "fail on this host without drift too", "`e1`", "`e2`", "`e3`",
                     "#694", "#705", "#709"):
            self.assertIn(text, body)
        # Reported in the status and logged.
        status = gsr.status_lines(self.conn)
        self.assertEqual(len(status), 1)
        self.assertIn("timing_check", status[0])
        self.assertIn("#900", status[0])
        self.assertEqual([h["event"] for h in gsr.history(self.conn)], ["switched_off"])

    def test_switch_off_survives_a_restart(self) -> None:
        self.main_results = [(True, "fails on main")]
        self._feed(self._repair(), [(f"e{i}", 700 + i, host_failure()) for i in range(3)])
        db = str(Path(self.tmp.name) / "db.sqlite")
        self.conn.close()
        self.conn = init_db(db)
        self.assertTrue(gsr.gate_is_off(self.conn, "timing_check"))

    def test_two_failures_do_nothing(self) -> None:
        decisions = self._feed(self._repair(), [("e1", 1, host_failure()), ("e2", 2, host_failure())])
        self.assertEqual([d.action for d in decisions], ["streak", "streak"])
        self.assertEqual(self.main_calls, [])
        self.assertFalse(gsr.gate_is_off(self.conn, "timing_check"))
        self.assertEqual(self.github.need_issues, [])

    def test_different_reasons_do_nothing(self) -> None:
        decisions = self._feed(self._repair(), [
            ("e1", 1, host_failure()),
            ("e2", 2, "timing check failed closed: error: control run without drift gave no verdict"),
            ("e3", 3, host_failure()),
        ])
        self.assertNotIn("switched_off", [d.action for d in decisions])
        self.assertEqual(self.main_calls, [])
        self.assertFalse(gsr.gate_is_off(self.conn, "timing_check"))

    def test_a_pass_in_between_breaks_the_row(self) -> None:
        decisions = self._feed(self._repair(), [
            ("e1", 1, host_failure()), ("e2", 2, None), ("e3", 3, host_failure()),
            ("e4", 4, host_failure()),
        ])
        self.assertEqual(decisions[-1].action, "streak")
        self.assertFalse(gsr.gate_is_off(self.conn, "timing_check"))

    def test_same_issue_twice_counts_once(self) -> None:
        decisions = self._feed(self._repair(), [
            ("e1", 1, host_failure()), ("e2", 1, host_failure()), ("e3", 2, host_failure()),
        ])
        self.assertEqual(decisions[-1].action, "streak")
        self.assertEqual(decisions[-1].experiment_ids, ["e2", "e3"])
        self.assertEqual(self.main_calls, [])

    def test_trigger_uses_the_consecutive_suffix_after_a_repeated_issue(self) -> None:
        self.main_results = [(True, "fails on main")]
        decisions = self._feed(self._repair(), [
            ("e1", 1, host_failure()), ("e2", 1, host_failure()),
            ("e3", 2, host_failure()), ("e4", 3, host_failure()),
        ])
        self.assertEqual(decisions[-1].action, "switched_off")
        self.assertEqual(decisions[-1].experiment_ids, ["e2", "e3", "e4"])
        self.assertEqual(
            gsr.gate_state(self.conn, "timing_check")["experiment_ids"],
            ["e2", "e3", "e4"],
        )

    def test_gate_passing_on_main_does_nothing(self) -> None:
        self.main_results = [(False, "timing check passed on main")]
        decisions = self._feed(self._repair(), [(f"e{i}", i, host_failure()) for i in range(3)])
        self.assertEqual(decisions[-1].action, "main_passes")
        self.assertIsNone(decisions[-1].classification)
        self.assertFalse(gsr.gate_is_off(self.conn, "timing_check"))
        self.assertEqual(self.github.need_issues, [])

    def test_no_verdict_on_main_does_nothing(self) -> None:
        self.main_results = [(None, "worktree failed")]
        decisions = self._feed(self._repair(), [(f"e{i}", i, host_failure()) for i in range(3)])
        self.assertEqual(decisions[-1].action, "no_verdict")
        self.assertFalse(gsr.gate_is_off(self.conn, "timing_check"))

    def test_gates_off_the_allowlist_are_never_switched_off(self) -> None:
        for gate, error in (("test_suite", "tests failed (exit code 1): tests/test_api.py::A::b"),
                            ("quality_gate", "quality evaluation: missing artifact")):
            with self.subTest(gate=gate):
                self.main_results = [(True, "fails on main")] * 3
                repair = self._repair(gate)
                decisions = self._feed(repair, [(f"{gate}{i}", 100 + i, error) for i in range(3)])
                self.assertEqual(decisions[-1].action, "not_switchable")
                self.assertEqual(self.main_calls, [])
                self.assertEqual(gsr.gates_off(self.conn), {})
        for gate in ("review", "test_suite", "main_guard", "safeguards"):
            self.assertIn(gate, gsr.PROTECTED_GATES)
            self.assertNotIn(gate, gsr.SWITCHABLE_GATES)
        self.assertEqual(set(gsr.SWITCHABLE_GATES), {"timing_check"})
        self.assertEqual(self.github.need_issues, [])

    def test_at_most_one_gate_off(self) -> None:
        with patch.dict(gsr.SWITCHABLE_GATES, {"other_gate": "NEXUS_OTHER"}):
            self.main_results = [(True, "fails on main")]
            self._feed(self._repair(), [(f"e{i}", i, host_failure()) for i in range(3)])
            failure = gsr.GateFailure("other_gate", "boom")
            repair = self._repair("other_gate")
            with patch.object(gsr, "classify_gate_failure", return_value=failure):
                decisions = self._feed(repair, [(f"o{i}", 50 + i, "x") for i in range(3)])
            self.assertEqual(decisions[-1].action, "busy")
            self.assertEqual(list(gsr.gates_off(self.conn)), ["timing_check"])

    # Switch back on ----------------------------------------------------------

    def _switch_off(self) -> gsr.GateSelfRepair:
        repair = self._repair()
        self.main_results = [(True, "fails on main")]
        self._feed(repair, [(f"e{i}", i, host_failure()) for i in range(3)])
        self.assertTrue(gsr.gate_is_off(self.conn, "timing_check"))
        return repair

    def test_merged_repair_switches_the_gate_on_and_closes_the_issue(self) -> None:
        repair = self._switch_off()
        self.main_results = [(False, "timing check passed on main")]
        decision = repair.after_merge("fix1", github=self.github)
        self.assertEqual(decision.action, "switched_on")
        self.assertFalse(gsr.gate_is_off(self.conn, "timing_check"))
        self.assertEqual(self.github.closed, [900])
        self.assertEqual(gsr.status_lines(self.conn), [])
        self.assertEqual([h["event"] for h in gsr.history(self.conn)], ["switched_off", "switched_on"])

    def test_failing_recheck_keeps_the_gate_off_with_a_comment_and_no_loop(self) -> None:
        repair = self._switch_off()
        self.main_results = [(True, "still fails on main")]
        decision = repair.after_merge("fix1", github=self.github)
        self.assertEqual(decision.action, "still_off")
        self.assertTrue(gsr.gate_is_off(self.conn, "timing_check"))
        self.assertEqual(self.github.closed, [])
        self.assertEqual(len(self.github.comments), 1)
        self.assertEqual(self.github.comments[0][0], 900)
        self.assertIn("stays off", self.github.comments[0][1])
        # The next merge does not run the gate again: the owner decides.
        calls = len(self.main_calls)
        self.assertEqual(repair.after_merge("fix2", github=self.github).action, "none")
        self.assertEqual(len(self.main_calls), calls)
        self.assertTrue(gsr.gate_is_off(self.conn, "timing_check"))
        self.assertTrue(repair.switch_on_by_owner("timing_check"))
        self.assertFalse(gsr.gate_is_off(self.conn, "timing_check"))

    def test_merge_with_no_gate_off_runs_nothing(self) -> None:
        self.assertEqual(self._repair().after_merge("x", github=self.github).action, "none")
        self.assertEqual(self.main_calls, [])

    # Measure: the 2026-11-14 sequence -----------------------------------------

    def test_replay_of_the_2026_11_14_sequence(self) -> None:
        """#694, #705, #709 (twice), #696: off after the third failure."""
        repair = self._repair()
        self.main_results = [(True, "fail on main without drift too")]
        blocked = 0
        for exp_id, issue in (("e694", 694), ("e705", 705), ("e709a", 709), ("e709b", 709),
                              ("e696", 696)):
            if gsr.gate_is_off(self.conn, "timing_check"):
                continue  # the gate no longer blocks this run
            blocked += 1
            repair.record_outcome(exp_id, host_failure(), issue_number=issue, github=self.github)
        self.assertEqual(blocked, 3)  # two runs saved
        self.assertEqual(gsr.gate_state(self.conn, "timing_check")["experiment_ids"],
                         ["e694", "e705", "e709a"])


class ClassifyTests(unittest.TestCase):
    def test_timing_reasons_ignore_the_test_list(self) -> None:
        a = gsr.classify_gate_failure(host_failure("tests/test_a.py::A::a"))
        b = gsr.classify_gate_failure(host_failure("tests/test_b.py::B::b"))
        self.assertEqual(a.gate, "timing_check")
        self.assertEqual(a.reason, b.reason)
        self.assertEqual(a.tests, ("tests/test_a.py::A::a",))

    def test_non_gate_errors_are_not_classified(self) -> None:
        self.assertIsNone(gsr.classify_gate_failure(None))
        self.assertIsNone(gsr.classify_gate_failure("coding worker timed out"))


class VerificationRespectsThePersistedSwitchTests(unittest.TestCase):
    def test_switched_off_gate_skips_the_timing_check(self) -> None:
        from modules.experiment_service.service_parts.verification import VerificationMixin
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            conn = init_db(str(Path(tmp) / "db.sqlite"))
            try:
                gsr._save_json(conn, "gate_self_repair:state:timing_check",
                               {"state": gsr.STATE_OFF, "reason": "host"})
                service = VerificationMixin()
                service._conn = conn
                service._lock = threading.Lock()
                elog = MagicMock()
                with patch.dict("os.environ", {"NEXUS_TIMING_CHECK": "1"}), \
                        patch("modules.timing_check.run_timing_check") as check:
                    self.assertIsNone(service._run_timing_check("x", tmp, "", elog))
                check.assert_not_called()
                self.assertIn("host_blocker:timing_check", elog.warning.call_args.args[2])
            finally:
                conn.close()


ROOT = Path(__file__).resolve().parents[1]
ALWAYS_FAILING = (
    "import os\n"
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_host(self):\n"
    "        self.assertEqual(os.environ.get('NEXUS_714_HOST'), 'fixed')\n"
)


class TimingCheckOnMainTests(unittest.TestCase):
    """The real main check: a worktree of main, the drift fixture, pytest."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "test_host.py").write_text(ALWAYS_FAILING, encoding="utf-8")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import timing_check_replay
        finally:
            sys.path.pop(0)
        self.assertTrue(timing_check_replay.ensure_plugin(self.repo, ROOT))
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, check=True, text=True,
                              capture_output=True).stdout

    def test_host_failure_fails_on_main_and_a_fixed_host_passes(self) -> None:
        tests = ["tests/test_host.py::T::test_host"]
        with patch.dict("os.environ", {"NEXUS_714_HOST": "broken"}):
            fails, detail = gsr.timing_check_on_main(str(self.repo), tests, ref="main")
        self.assertIs(fails, True, detail)
        self.assertIn("without drift too", detail)
        with patch.dict("os.environ", {"NEXUS_714_HOST": "fixed"}):
            fails, detail = gsr.timing_check_on_main(str(self.repo), tests, ref="main")
        self.assertIs(fails, False, detail)
        # The temporary worktree is gone again.
        self.assertEqual(len(self._git("worktree", "list").splitlines()), 1)

    def test_missing_tests_give_no_verdict(self) -> None:
        fails, _ = gsr.timing_check_on_main(str(self.repo), ["tests/test_gone.py::A::b"], ref="main")
        self.assertIsNone(fails)


class ServiceWiringTests(unittest.TestCase):
    """run_experiment feeds the detector; adoption rechecks after a merge."""

    def setUp(self) -> None:
        from modules.experiment_service import ExperimentService

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.service = ExperimentService(
            db_path=str(root / "db.sqlite"), repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"), test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"),
            worker_factory=lambda _: MagicMock(), plan_worker_factory=lambda _: None,
            selection_worker=MagicMock(),
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def _run(self, issue: int, status: str, error: str | None):
        experiment = self.service.create_experiment(f"GitHub Issue #{issue}: something")

        def pipeline(experiment_id, **_kwargs):
            self.service._force_status(experiment_id, status, error=error)
            return self.service.get_experiment(experiment_id)

        with patch.object(self.service, "_run_pipeline", side_effect=pipeline):
            return self.service.run_experiment(experiment.id, github_work_tracking=self.github)

    def test_run_experiment_switches_off_after_three_host_failures(self) -> None:
        self.github = FakeGitHub()
        with patch.object(gsr, "timing_check_on_main", return_value=(True, "fails on main")) as main:
            self._run(694, "needs_review", host_failure())
            self._run(705, "needs_review", host_failure())
            self.assertFalse(gsr.gate_is_off(self.service._conn, "timing_check"))
            self._run(709, "needs_review", host_failure())
        main.assert_called_once()
        self.assertTrue(gsr.gate_is_off(self.service._conn, "timing_check"))
        self.assertEqual(len(self.github.need_issues), 1)

    def test_a_succeeded_run_resets_the_row(self) -> None:
        self.github = FakeGitHub()
        with patch.object(gsr, "timing_check_on_main", return_value=(True, "fails on main")) as main:
            self._run(1, "needs_review", host_failure())
            self._run(2, "needs_review", host_failure())
            self._run(3, "succeeded", None)
            self._run(4, "needs_review", host_failure())
        main.assert_not_called()
        self.assertFalse(gsr.gate_is_off(self.service._conn, "timing_check"))

    def test_adoption_hook_switches_on_after_merge(self) -> None:
        from modules.experiment_adoption_parts.pipeline import PipelineMixin
        import threading

        gsr._save_json(self.service._conn, "gate_self_repair:state:timing_check",
                       {"state": gsr.STATE_OFF, "reason": "host", "tests": ["tests/t.py"],
                        "issue_number": 900})
        adoption = PipelineMixin()
        adoption._conn = self.service._conn
        adoption._lock = threading.Lock()
        adoption.repo_root = self.repo
        adoption.experiment_service = self.service
        adoption.github_work_tracking = FakeGitHub()
        with patch.object(gsr, "timing_check_on_main", return_value=(False, "passed")) as main:
            adoption._recheck_switched_off_gates("fix", MagicMock())
        main.assert_called_once()
        self.assertFalse(gsr.gate_is_off(self.service._conn, "timing_check"))
        self.assertEqual(adoption.github_work_tracking.closed, [900])


if __name__ == "__main__":
    unittest.main()
