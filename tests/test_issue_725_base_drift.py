"""#725: a drift failure in a test main already has is rerun on the base commit."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI

from modules import known_drift_tests
from modules import timing_check as timing_check_module
from modules.experiment_evaluation import EvaluationResult
from modules.experiment_service import ExperimentService, STATUS_VERIFYING
from modules.experiment_service.service_parts.verification import TIMING_VERDICT_KNOWN_DRIFT
from modules.nexus_mcp_tools import READ_ONLY_TOOLS, WRITE_TOOLS
from modules.run_store import init_db

ROOT = Path(__file__).resolve().parents[1]

# A module the test imports, so a change to it selects the test (affected_tests).
CLOCK = "def budget():\n    return 0.02\n"
# Fails only under the drift fixture: a budget computed from wall-clock deltas.
DRIFT_SENSITIVE = (
    "import time\n"
    "import unittest\n"
    "import clock\n"
    "class T(unittest.TestCase):\n"
    "    def test_budget(self):\n"
    "        started = time.monotonic()\n"
    "        self.assertLess(time.monotonic() - started, clock.budget())\n"
)
# Same test id, but independent of the clock.
CLOCK_FREE = (
    "import unittest\n"
    "import clock\n"
    "class T(unittest.TestCase):\n"
    "    def test_budget(self):\n"
    "        self.assertGreater(clock.budget(), 0)\n"
)
OLD_ID = "tests/test_old.py::T::test_budget"
NEW_ID = "tests/test_new.py::T::test_budget"


def _replay_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import timing_check_replay
    finally:
        sys.path.pop(0)
    return timing_check_replay


class _Repo:
    """A throwaway git repository with the drift fixture committed."""

    def __init__(self, root: Path) -> None:
        self.path = root / "repo"
        (self.path / "tests").mkdir(parents=True)
        (self.path / "clock.py").write_text(CLOCK, encoding="utf-8")
        (self.path / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        assert _replay_module().ensure_plugin(self.path, ROOT)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.path, check=True, text=True, capture_output=True,
        ).stdout

    def commit(self, relative: str, text: str) -> str:
        (self.path / relative).write_text(text, encoding="utf-8")
        self.git("add", relative)
        self.git("commit", "-q", "-m", f"add {relative}")
        return self.git("rev-parse", "HEAD").strip()

    def touch_clock(self) -> None:
        """The experiment's change: the module the test imports."""
        (self.path / "clock.py").write_text(CLOCK + "# changed\n", encoding="utf-8")


class BaseDriftVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = _Repo(root)
        self.db_path = str(root / "db.sqlite")
        self.service = ExperimentService(
            db_path=self.db_path, repo_root=str(self.repo.path),
            worktrees_root=str(root / "worktrees"), test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"), max_repair_iterations=1,
            worker_factory=lambda _: MagicMock(), plan_worker_factory=lambda _: None,
        )
        quality = patch(
            "modules.experiment_service.evaluate_implementation",
            return_value=EvaluationResult(outcome="pass", reason="ok"),
        )
        quality.start()
        self.addCleanup(quality.stop)
        env = patch.dict("os.environ", {"NEXUS_TIMING_CHECK": "1"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def _experiment(self, *, with_base: bool = True) -> str:
        experiment = self.service.create_experiment("Change clock")
        fields = {"base_commit_sha": self.repo.git("rev-parse", "HEAD").strip()} if with_base else {}
        self.service._force_status(experiment.id, STATUS_VERIFYING, **fields)
        return experiment.id

    def _verify(self, *, with_base: bool = True):
        experiment_id = self._experiment(with_base=with_base)
        return experiment_id, self.service._verify(experiment_id, str(self.repo.path))

    def _timing_check(self, tracker):
        experiment_id = self._experiment()
        elog = self.service.get_experiment_logger(experiment_id)
        diff = self.repo.git("diff")
        return self.service._run_timing_check(
            experiment_id, str(self.repo.path), diff, elog, github_work_tracking=tracker,
        )

    def _rows(self) -> list[dict]:
        conn = init_db(self.db_path)
        try:
            return known_drift_tests.list_known_drift(conn)
        finally:
            conn.close()

    def _events(self, experiment_id: str) -> list[str]:
        log = Path(self.service.experiment_log_dir)
        return [
            line for path in log.rglob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
            if experiment_id in line
        ]

    def test_drift_on_base_and_branch_passes_with_known_drift(self) -> None:
        base = self.repo.commit("tests/test_old.py", DRIFT_SENSITIVE)
        self.repo.touch_clock()
        experiment_id, result = self._verify()
        self.assertEqual(result.status, "succeeded", result.error)
        events = "\n".join(self._events(experiment_id))
        self.assertIn("timing_check_passed_with_known_drift", events)
        self.assertIn(TIMING_VERDICT_KNOWN_DRIFT, events)
        rows = self._rows()
        self.assertEqual([r["test_id"] for r in rows], [OLD_ID])
        self.assertEqual(rows[0]["first_seen_commit"], base)
        self.assertEqual(rows[0]["count"], 1)

    def test_passing_on_base_and_failing_on_branch_stays_failed(self) -> None:
        self.repo.commit("tests/test_old.py", CLOCK_FREE)
        (self.repo.path / "tests" / "test_old.py").write_text(DRIFT_SENSITIVE, encoding="utf-8")
        _, result = self._verify()
        self.assertEqual(result.status, "failed")
        self.assertIn(f"timing_sensitive: {OLD_ID}; hint:", result.error)
        self.assertEqual(self._rows(), [])

    def test_a_test_the_branch_added_counts_as_regressed(self) -> None:
        (self.repo.path / "tests" / "test_new.py").write_text(DRIFT_SENSITIVE, encoding="utf-8")
        _, result = self._verify()
        self.assertEqual(result.status, "failed")
        self.assertIn(f"timing_sensitive: {NEW_ID}; hint:", result.error)
        self.assertEqual(self._rows(), [])

    def test_mixed_lists_only_the_regressed_test_and_stores_the_known_one(self) -> None:
        self.repo.commit("tests/test_old.py", DRIFT_SENSITIVE)
        self.repo.touch_clock()
        (self.repo.path / "tests" / "test_new.py").write_text(DRIFT_SENSITIVE, encoding="utf-8")
        _, result = self._verify()
        self.assertEqual(result.status, "failed")
        self.assertIn(f"timing_sensitive: {NEW_ID}; hint:", result.error)
        self.assertNotIn(OLD_ID, result.error)
        self.assertEqual([r["test_id"] for r in self._rows()], [OLD_ID])

    def test_second_experiment_increments_count_and_opens_no_second_issue(self) -> None:
        self.repo.commit("tests/test_old.py", DRIFT_SENSITIVE)
        self.repo.touch_clock()
        tracker = MagicMock()
        tracker.open_issue.return_value = {"number": 901, "url": "u", "reused": False}
        self.assertIsNone(self._timing_check(tracker))
        self.assertIsNone(self._timing_check(tracker))
        tracker.open_issue.assert_called_once()
        title, body = tracker.open_issue.call_args.args
        self.assertEqual(title, f"Make {OLD_ID} independent of wall-clock time")
        self.assertIn("local coding worker", body)
        self.assertIn(f"NEXUS_TEST_CLOCK_DRIFT_MS=50 python -m pytest", body)
        self.assertIn("AssertionError", body)  # the failure tail is the context
        rows = self._rows()
        self.assertEqual((rows[0]["count"], rows[0]["issue_number"]), (2, 901))

    def test_a_failed_issue_open_is_retried_at_the_next_sighting(self) -> None:
        self.repo.commit("tests/test_old.py", DRIFT_SENSITIVE)
        self.repo.touch_clock()
        tracker = MagicMock()
        tracker.open_issue.side_effect = [RuntimeError("github down"), {"number": 7}]
        self.assertIsNone(self._timing_check(tracker))
        self.assertIsNone(self._rows()[0]["issue_number"])
        self.assertIsNone(self._timing_check(tracker))
        self.assertEqual(tracker.open_issue.call_count, 2)
        self.assertEqual(self._rows()[0]["issue_number"], 7)

    def test_without_base_commit_the_flag_stays_a_failure(self) -> None:
        self.repo.commit("tests/test_old.py", DRIFT_SENSITIVE)
        self.repo.touch_clock()
        with patch("modules.timing_check.base_drift_check") as base_check:
            _, result = self._verify(with_base=False)
        base_check.assert_not_called()
        self.assertEqual(result.status, "failed")
        self.assertIn(f"timing_sensitive: {OLD_ID}; hint:", result.error)
        self.assertEqual(self._rows(), [])

    def test_base_check_without_verdict_or_crash_keeps_the_failure(self) -> None:
        self.repo.commit("tests/test_old.py", DRIFT_SENSITIVE)
        self.repo.touch_clock()
        for effect in ({"return_value": None}, {"side_effect": RuntimeError("boom")}):
            with self.subTest(effect=effect), \
                    patch("modules.timing_check.base_drift_check", **effect):
                _, result = self._verify()
            self.assertEqual(result.status, "failed")
            self.assertIn(OLD_ID, result.error)
        self.assertEqual(self._rows(), [])

    def test_base_rerun_uses_the_branch_drift_env(self) -> None:
        self.repo.commit("tests/test_old.py", DRIFT_SENSITIVE)
        self.repo.touch_clock()
        seen: dict[str, dict] = {}
        real_run = timing_check_module.run_timing_check

        def spy_run(*args, **kwargs):
            if "env" in kwargs:  # the control run without drift passes none
                seen["branch"] = kwargs["env"]
            return real_run(*args, **kwargs)

        with patch("modules.timing_check.run_timing_check", side_effect=spy_run), \
                patch("modules.timing_check.base_drift_check",
                      return_value=([], [OLD_ID])) as base_check:
            _, result = self._verify()
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertIs(base_check.call_args.args[3], seen["branch"])
        self.assertEqual(seen["branch"]["NEXUS_TEST_CLOCK_DRIFT_MS"], "50")


class BaseDriftCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = _Repo(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _worktrees(self) -> int:
        return self.repo.git("worktree", "list").count("\n")

    def test_classifies_and_cleans_up_the_temp_worktree(self) -> None:
        (self.repo.path / "tests" / "test_old.py").write_text(
            DRIFT_SENSITIVE + "class P(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
            encoding="utf-8",
        )
        base = self.repo.commit("tests/test_old.py", (self.repo.path / "tests" / "test_old.py").read_text())
        self.repo.commit("tests/test_new.py", DRIFT_SENSITIVE)
        before = self._worktrees()
        result = timing_check_module.base_drift_check(
            base, self.repo.path,
            [OLD_ID, "tests/test_old.py::P::test_ok", NEW_ID],
            timing_check_module.drift_env(),
        )
        self.assertEqual(
            result, (["tests/test_old.py::P::test_ok", NEW_ID], [OLD_ID]),
        )
        self.assertEqual(self._worktrees(), before)

    def test_unknown_base_commit_gives_no_verdict(self) -> None:
        self.assertIsNone(timing_check_module.base_drift_check(
            "0" * 40, self.repo.path, [OLD_ID], timing_check_module.drift_env(),
        ))

    def test_no_tests_is_an_empty_verdict(self) -> None:
        self.assertEqual(
            timing_check_module.base_drift_check("HEAD", self.repo.path, [], {}), ([], []),
        )


class KnownDriftListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "db.sqlite")
        conn = init_db(self.db_path)
        known_drift_tests.record_sighting(conn, OLD_ID, "a" * 40, "2026-11-25T10:00:00Z", "e1")
        known_drift_tests.record_sighting(conn, OLD_ID, "b" * 40, "2026-11-25T11:00:00Z", "e2")
        known_drift_tests.set_issue(conn, OLD_ID, 42)
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_operation_lists_count_commit_and_issue(self) -> None:
        from modules.operations_registry import OperationsRegistry

        registry = OperationsRegistry(experiment_service=MagicMock(), db_path=self.db_path)
        self.addCleanup(registry.close)
        specs = {spec.name: spec for spec in registry.catalog()}
        self.assertFalse(specs["known_drift_tests_list"].mutates_state)
        result = registry.execute("known_drift_tests_list", {"limit": 5})
        self.assertTrue(result.success)
        self.assertEqual(
            result.output_lines,
            [f"{OLD_ID}: seen 2x, first on aaaaaaaaaaaa, last 2026-11-25T11:00:00Z, issue #42"],
        )

    def test_mcp_tool_is_read_only(self) -> None:
        # api needs a mailbox path at import; conftest sets one under pytest only.
        mailbox = os.environ.get("AGENT_MAILBOX_REPO_PATH") or self.tmp.name
        with patch.dict("os.environ", {"AGENT_MAILBOX_REPO_PATH": mailbox}):
            os.environ.setdefault("AGENT_API_KEY", "test-key")
            import api
            import nexus_mcp

        self.assertIn("known_drift_tests_list", READ_ONLY_TOOLS)
        self.assertNotIn("known_drift_tests_list", WRITE_TOOLS)

        class FakeMcp:
            def __init__(self):
                self.tools = {}
                self.session_manager = object()

            def tool(self, **_kwargs):
                def decorate(function):
                    self.tools[function.__name__] = function
                    return function
                return decorate

            def streamable_http_app(self, **_kwargs):
                return FastAPI()

        service = ExperimentService(
            db_path=self.db_path, repo_root=self.tmp.name,
            worktrees_root=str(Path(self.tmp.name) / "wt"),
            experiment_log_dir=str(Path(self.tmp.name) / "logs"),
        )
        self.addCleanup(service.close)
        fake = FakeMcp()
        with patch.object(nexus_mcp, "MCPServer", return_value=fake), \
                patch.object(api, "_resolve_experiment_service", return_value=service):
            nexus_mcp._build_mount_app()
            listed = fake.tools["known_drift_tests_list"](limit=3)
            with self.assertRaisesRegex(ValueError, "invalid limit"):
                fake.tools["known_drift_tests_list"](limit=0)
        self.assertEqual(listed["count"], 1)
        row = listed["known_drift_tests"][0]
        self.assertEqual(
            (row["test_id"], row["count"], row["first_seen_commit"], row["issue_number"]),
            (OLD_ID, 2, "a" * 40, 42),
        )


if __name__ == "__main__":
    unittest.main()
