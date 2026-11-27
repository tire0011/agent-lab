"""#730: gate self-repair - a repeated host blocker switches the timing check off.

The last three finished experiments on three different issues ending with the
same normalized gate reason (``timing check failed closed: ... without drift
too``) trigger one rerun of the gate on current main; only when that fails too
is the gate classified ``host_blocker:timing_check`` and switched off
persistently (allowlisted, at most one gate off, repair issue without the
``proposal`` label, durable pending reports retried on the next tick, state
visible in status).
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from modules import gate_self_repair as gsr
from modules.run_store import init_db


def gate_error(*test_ids: str) -> str:
    """The stored error of a timing-check failure that failed closed because
    the tests fail on this host without drift too."""
    return (
        "timing check failed closed: error: these tests fail on this host "
        "without drift too, so there is no timing verdict: " + ", ".join(test_ids)
    )


# The stored 2026-11-14 rows of the production sequence (#694, #705, #709 and
# the repeated #709), used when no production snapshot is available.
_EMBEDDED_2026_11_14 = [
    (
        "77908da13b4e4d09826f82d367341a49", "2026-11-14T18:16:53.119189+00:00", 694,
        gate_error(
            "tests/test_issue_637_local_shadow_replay.py::ReadToolTests::test_lists_replays_and_variant_rates",
            "tests/test_issue_649_hunk_replay.py::ReadSideTests::test_api_and_operation_report_per_variant_unit_aggregates",
        ),
    ),
    (
        "bfed30d54e1340bc993389d9782c752a", "2026-11-14T18:59:31.588494+00:00", 705,
        gate_error(
            "tests/test_issue_689_timing_check_verification.py::TimingCheckVerificationTests::test_test_failing_without_drift_too_gives_needs_review",
        ),
    ),
    (
        "1e9d2040bb8347d88cc315bf393a6107", "2026-11-14T19:43:24.937152+00:00", 709,
        gate_error(
            "tests/test_issue_689_timing_check_verification.py::TimingCheckVerificationTests::test_only_drift_only_tests_are_reported",
            "tests/test_issue_689_timing_check_verification.py::TimingCheckVerificationTests::test_test_failing_without_drift_too_gives_needs_review",
        ),
    ),
    (
        "46e7ae00afa6475da3e0f41ad9a05ce6", "2026-11-14T20:27:43.382533+00:00", 709,
        gate_error(
            "tests/test_issue_689_timing_check_verification.py::TimingCheckVerificationTests::test_only_drift_only_tests_are_reported",
            "tests/test_issue_689_timing_check_verification.py::TimingCheckVerificationTests::test_test_failing_without_drift_too_gives_needs_review",
        ),
    ),
]


def _snapshot_2026_11_14() -> list[tuple[str, str, int, str]] | None:
    """The same sequence read from the stored production rows, when available."""
    path = os.environ.get("NEXUS_PROD_DB_SNAPSHOT")
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT e.id, e.ended_at, l.issue_number, e.error FROM experiments e "
            "JOIN github_experiment_links l ON l.experiment_id=e.id "
            "WHERE l.issue_number IN (694, 705, 709) AND e.error LIKE 'timing check failed closed%' "
            "AND e.ended_at LIKE '2026-11-14%' ORDER BY e.ended_at"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return None
    return [(str(r[0]), str(r[1]), int(r[2]), str(r[3])) for r in rows] or None


class _Repo:
    """A throwaway git repository with a ``main`` branch (current main)."""

    def __init__(self, root: Path) -> None:
        self.path = root / "repo"
        self.path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        (self.path / "f.txt").write_text("x", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.path, check=True, text=True, capture_output=True,
        ).stdout


def _ensure_links_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS github_experiment_links (
            experiment_id TEXT PRIMARY KEY,
            issue_number INTEGER NOT NULL,
            issue_title TEXT NOT NULL,
            branch_name TEXT NOT NULL,
            pr_number INTEGER,
            checkpoint_sha TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


class GateSelfRepairTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = _Repo(root)
        self.db_path = str(root / "db.sqlite")
        conn = init_db(self.db_path)
        _ensure_links_schema(conn)
        conn.close()

    # -- helpers ---------------------------------------------------------- #

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        self.addCleanup(conn.close)
        return conn

    def add_finished(
        self, conn: sqlite3.Connection, experiment_id: str, ended_at: str, error: str | None,
        *, issue: int | None = None, status: str = "needs_review", goal: str | None = None,
    ) -> None:
        goal = goal if goal is not None else f"GitHub Issue #{issue}: work" if issue else "work"
        conn.execute(
            "INSERT INTO experiments (id, goal, status, created_at, ended_at, error) VALUES (?,?,?,?,?,?)",
            (experiment_id, goal, status, ended_at, ended_at, error),
        )
        if issue is not None:
            conn.execute(
                "INSERT INTO github_experiment_links "
                "(experiment_id, issue_number, issue_title, branch_name, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (experiment_id, issue, f"#{issue}", "branch", ended_at, ended_at),
            )
        conn.commit()

    def add_three_same_reason(self, conn: sqlite3.Connection) -> None:
        self.add_finished(
            conn, "exp-a", "2026-11-14T18:16:53+00:00",
            gate_error("tests/test_a.py::T::test_x"), issue=694,
        )
        self.add_finished(
            conn, "exp-b", "2026-11-14T18:59:31+00:00",
            gate_error("tests/test_b.py::T::test_y"), issue=705,
        )
        self.add_finished(
            conn, "exp-c", "2026-11-14T19:43:24+00:00",
            gate_error("tests/test_c.py::T::test_z", "tests/test_a.py::T::test_x"), issue=709,
        )

    class FakeDeliver:
        """Records deliveries; the GitHub issue gets the next fake number."""

        def __init__(self, *, fail_github: bool = False) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.fail_github = fail_github
            self._next = 4200

        def __call__(self, kind: str, payload: dict) -> dict:
            self.calls.append((kind, dict(payload)))
            if kind == "github_issue":
                if self.fail_github:
                    raise RuntimeError("gh unavailable")
                self._next += 1
                return {"number": self._next}
            return {"outbox_file": f"{kind}.md"}

    @staticmethod
    def failing_runner(repo_path: str, tests: list[str]) -> str:
        return "error: these tests fail on this host without drift too, so there is no timing verdict: " + ", ".join(tests)

    @staticmethod
    def passing_runner(repo_path: str, tests: list[str]) -> str:
        return "ok"


class NormalizationTests(GateSelfRepairTestCase):
    def test_same_prefix_different_test_ids_normalize_equal(self):
        e1 = gate_error("tests/test_a.py::T::test_x", "tests/test_b.py::test_y")
        e2 = gate_error("tests/test_c.py::test_z")
        self.assertEqual(gsr.normalize_gate_reason(e1), gsr.normalize_gate_reason(e2))
        self.assertTrue(gsr.normalize_gate_reason(e1).startswith("timing check failed closed:"))
        self.assertTrue(gsr.normalize_gate_reason(e1).endswith("without drift too"))

    def test_other_reasons_do_not_normalize(self):
        self.assertIsNone(gsr.normalize_gate_reason("timing_sensitive: tests/test_a.py::T::test_x; hint: ..."))
        self.assertIsNone(gsr.normalize_gate_reason("timing check failed closed: error: no verdict"))
        self.assertIsNone(gsr.normalize_gate_reason("GitHub checkpoint failed before verification: ..."))
        self.assertIsNone(gsr.normalize_gate_reason(None))
        self.assertIsNone(gsr.normalize_gate_reason(""))

    def test_extract_test_ids(self):
        error = gate_error("tests/test_a.py::T::test_x", "tests/test_b.py::test_y[param]")
        self.assertEqual(
            gsr.extract_test_ids(error),
            ["tests/test_a.py::T::test_x", "tests/test_b.py::test_y[param]"],
        )
        self.assertEqual(gsr.extract_test_ids("no mark here"), [])


class DetectionTests(GateSelfRepairTestCase):
    def test_three_same_reason_failures_on_distinct_issues_are_detected(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        detection = gsr.detect_repeated_blocker(conn)
        self.assertIsNotNone(detection)
        self.assertEqual(sorted(detection["issues"]), [694, 705, 709])
        self.assertEqual(
            detection["reason"],
            "timing check failed closed: error: these tests fail on this host without drift too",
        )
        self.assertEqual(
            detection["test_ids"],
            ["tests/test_a.py::T::test_x", "tests/test_b.py::T::test_y", "tests/test_c.py::T::test_z"],
        )

    def test_two_failures_are_not_enough(self):
        conn = self._conn()
        self.add_finished(conn, "exp-a", "2026-11-14T18:16:53+00:00", gate_error("tests/test_a.py::T::test_x"), issue=694)
        self.add_finished(conn, "exp-b", "2026-11-14T18:59:31+00:00", gate_error("tests/test_b.py::T::test_y"), issue=705)
        self.assertIsNone(gsr.detect_repeated_blocker(conn))

    def test_mixed_reasons_are_not_detected(self):
        conn = self._conn()
        self.add_finished(conn, "exp-a", "2026-11-14T18:16:53+00:00", gate_error("tests/test_a.py::T::test_x"), issue=694)
        self.add_finished(conn, "exp-b", "2026-11-14T18:59:31+00:00", gate_error("tests/test_b.py::T::test_y"), issue=705)
        self.add_finished(conn, "exp-c", "2026-11-14T19:43:24+00:00", "quality evaluation: nur Tests geaendert", issue=709)
        self.assertIsNone(gsr.detect_repeated_blocker(conn))

    def test_same_issue_three_times_is_not_detected(self):
        conn = self._conn()
        for i, ended in enumerate(("2026-11-14T18:16:53+00:00", "2026-11-14T18:59:31+00:00", "2026-11-14T19:43:24+00:00")):
            self.add_finished(conn, f"exp-{i}", ended, gate_error(f"tests/test_{i}.py::test_x"), issue=709)
        self.assertIsNone(gsr.detect_repeated_blocker(conn))

    def test_unfinished_experiments_do_not_count(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.execute(
            "INSERT INTO experiments (id, goal, status, created_at, error) VALUES ('running', 'g', 'verifying', 't', ?)",
            (gate_error("tests/test_new.py::test_q"),),
        )
        conn.commit()
        detection = gsr.detect_repeated_blocker(conn)
        self.assertIsNotNone(detection)
        self.assertNotIn("running", detection["experiments"])

    def test_issue_number_falls_back_to_goal_text(self):
        conn = self._conn()
        for i, (ended, issue) in enumerate([
            ("2026-11-14T18:16:53+00:00", 694),
            ("2026-11-14T18:59:31+00:00", 705),
            ("2026-11-14T19:43:24+00:00", 709),
        ]):
            self.add_finished(
                conn, f"exp-{i}", ended, gate_error(f"tests/test_{i}.py::test_x"),
                issue=None, goal=f"GitHub Issue #{issue}: do something",
            )
        detection = gsr.detect_repeated_blocker(conn)
        self.assertIsNotNone(detection)
        self.assertEqual(sorted(detection["issues"]), [694, 705, 709])


class SwitchOffTests(GateSelfRepairTestCase):
    def test_three_failures_plus_main_failure_switch_the_gate_off(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        deliver = self.FakeDeliver()
        runner_calls: list[list[str]] = []

        def runner(repo_path: str, tests: list[str]) -> str:
            runner_calls.append(list(tests))
            return self.failing_runner(repo_path, tests)

        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver, runner=runner,
        )
        self.assertTrue(outcome["switched_off"])
        self.assertEqual(outcome["blocker"], "host_blocker:timing_check")
        self.assertEqual(outcome["disabled_gates"], ["timing_check"])
        self.assertEqual(outcome["timing_check"], "off")
        # The gate was rerun once on main with the union of the test ids.
        self.assertEqual(len(runner_calls), 1)
        self.assertEqual(
            sorted(runner_calls[0]),
            ["tests/test_a.py::T::test_x", "tests/test_b.py::T::test_y", "tests/test_c.py::T::test_z"],
        )
        # The repair issue was opened and its number stored with the switch-off.
        github_calls = [payload for kind, payload in deliver.calls if kind == "github_issue"]
        self.assertEqual(len(github_calls), 1)
        self.assertEqual(github_calls[0]["title"], "Repair host blocker: timing_check")
        self.assertEqual(outcome["repair_issue_number"], 4201)
        self.assertEqual(outcome["pending_reports"], 0)
        # Persisted, and timing_check_enabled() reads it (the suite runs with
        # NEXUS_TIMING_CHECK=0, so opt in to prove the persisted switch-off,
        # not the environment, disables the check).
        from modules.experiment_service.service_parts.verification import timing_check_enabled

        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            self.assertFalse(timing_check_enabled(self.db_path))
        state = gsr.status(self.db_path)
        self.assertEqual(state["timing_check"], "off")
        self.assertEqual(state["host_blocker"], "host_blocker:timing_check")
        self.assertEqual(state["repair_issue_number"], 4201)

    def test_repair_issue_has_no_proposal_label_and_no_proposal_footer(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        deliver = self.FakeDeliver()
        gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver,
            runner=self.failing_runner,
        )
        github_payloads = [payload for kind, payload in deliver.calls if kind == "github_issue"]
        self.assertEqual(len(github_payloads), 1)
        payload = github_payloads[0]
        self.assertEqual(payload["title"], "Repair host blocker: timing_check")
        # No labels at all (in particular not `proposal`), and no need-issue footer.
        self.assertNotIn("labels", payload)
        self.assertNotIn("proposal", json.dumps(payload))
        self.assertNotIn("Remove the `proposal` label", payload["body"])
        # The module never routes this through open_need_issue (no such call).
        self.assertFalse(hasattr(gsr, "open_need_issue"))
        self.assertNotIn("open_need_issue(", inspect.getsource(gsr))
        # The default gh delivery opens a plain issue: no --label flag.
        source = inspect.getsource(gsr._gh_create_issue)
        self.assertIn('"--title"', source)
        self.assertNotIn('"--label"', source)

    def test_status_shows_the_state(self):
        import chat

        from modules import nexus_status_issue

        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        deliver = self.FakeDeliver()
        gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver,
            runner=self.failing_runner,
        )
        status = gsr.status(self.db_path)
        self.assertEqual(status["timing_check"], "off")
        self.assertEqual(status["host_blocker"], "host_blocker:timing_check")
        self.assertEqual(status["repair_issue_number"], 4201)
        built = chat.build_status(self.db_path)
        self.assertEqual(built["gate_self_repair"]["timing_check"], "off")
        self.assertEqual(built["gate_self_repair"]["disabled_gates"], ["timing_check"])
        conn = self._conn()
        body = nexus_status_issue.render(
            conn, now=datetime.now(timezone.utc), budget=10, spent=1, paused=False, busy=None,
        )
        conn.close()
        self.assertIn("switched off: timing_check", body)
        self.assertIn("host_blocker:timing_check", body)
        self.assertIn("repair issue #4201", body)

    def test_gate_passing_on_main_leads_to_nothing(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        deliver = self.FakeDeliver()
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver,
            runner=self.passing_runner,
        )
        self.assertFalse(outcome["switched_off"])
        self.assertIsNotNone(outcome["detected"])
        self.assertTrue(outcome["rerun"]["passed"])
        self.assertEqual(outcome["timing_check"], "on")
        self.assertEqual(outcome["pending_reports"], 0)
        self.assertEqual(deliver.calls, [])
        from modules.experiment_service.service_parts.verification import timing_check_enabled

        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            self.assertTrue(timing_check_enabled(self.db_path))

    def test_two_failures_lead_to_nothing(self):
        conn = self._conn()
        self.add_finished(conn, "exp-a", "2026-11-14T18:16:53+00:00", gate_error("tests/test_a.py::T::test_x"), issue=694)
        self.add_finished(conn, "exp-b", "2026-11-14T18:59:31+00:00", gate_error("tests/test_b.py::T::test_y"), issue=705)
        conn.close()
        deliver = self.FakeDeliver()
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver,
            runner=self.failing_runner,
        )
        self.assertFalse(outcome["switched_off"])
        self.assertIsNone(outcome["detected"])
        self.assertEqual(outcome["timing_check"], "on")
        self.assertEqual(deliver.calls, [])

    def test_mixed_reasons_lead_to_nothing(self):
        conn = self._conn()
        self.add_finished(conn, "exp-a", "2026-11-14T18:16:53+00:00", gate_error("tests/test_a.py::T::test_x"), issue=694)
        self.add_finished(conn, "exp-b", "2026-11-14T18:59:31+00:00", gate_error("tests/test_b.py::T::test_y"), issue=705)
        self.add_finished(conn, "exp-c", "2026-11-14T19:43:24+00:00", "timing_sensitive: tests/test_c.py::T::test_z", issue=709)
        conn.close()
        deliver = self.FakeDeliver()
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver,
            runner=self.failing_runner,
        )
        self.assertFalse(outcome["switched_off"])
        self.assertEqual(outcome["timing_check"], "on")
        self.assertEqual(deliver.calls, [])

    def test_same_issue_three_times_leads_to_nothing(self):
        conn = self._conn()
        for i, ended in enumerate(("2026-11-14T18:16:53+00:00", "2026-11-14T18:59:31+00:00", "2026-11-14T19:43:24+00:00")):
            self.add_finished(conn, f"exp-{i}", ended, gate_error(f"tests/test_{i}.py::test_x"), issue=709)
        conn.close()
        deliver = self.FakeDeliver()
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver,
            runner=self.failing_runner,
        )
        self.assertFalse(outcome["switched_off"])
        self.assertEqual(outcome["timing_check"], "on")
        self.assertEqual(deliver.calls, [])


class AllowlistTests(GateSelfRepairTestCase):
    def test_non_allowlisted_gates_are_never_switched_off(self):
        conn = self._conn()
        for gate in ("review", "test_suite", "main_guard", "safeguards", "ci", "anything"):
            with self.assertRaises(ValueError, msg=gate):
                gsr.disable_gate(conn, gate, classification=f"host_blocker:{gate}", reason="r")
        self.assertEqual(gsr.gate_state(conn)["disabled_gates"], [])
        self.assertEqual(gsr.gate_disabled_at(self.db_path, "review"), None)
        conn.close()

    def test_at_most_one_gate_is_off_at_a_time(self):
        conn = self._conn()
        gsr.disable_gate(conn, "timing_check", classification="host_blocker:timing_check", reason="r")
        # Re-disabling the same gate is idempotent, not a second gate.
        gsr.disable_gate(conn, "timing_check", classification="host_blocker:timing_check", reason="r2")
        self.assertEqual(gsr.gate_state(conn)["disabled_gates"], ["timing_check"])
        # Another gate already off blocks even the allowlisted one.
        conn.execute(
            "INSERT INTO service_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (gsr.SETTING_DISABLED_GATES, json.dumps([{"gate": "review"}]), "now"),
        )
        conn.commit()
        with self.assertRaises(ValueError):
            gsr.disable_gate(conn, "timing_check", classification="x", reason="y")
        self.assertEqual(gsr.gate_state(conn)["disabled_gates"], ["review"])
        conn.close()


class PendingReportTests(GateSelfRepairTestCase):
    def test_failed_github_call_leaves_a_pending_report_the_next_tick_sends(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        flaky = self.FakeDeliver(fail_github=True)
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=flaky,
            runner=self.failing_runner,
        )
        # The switch-off itself is persisted before any reporting happens.
        self.assertTrue(outcome["switched_off"])
        self.assertEqual(outcome["timing_check"], "off")
        self.assertIsNone(outcome["repair_issue_number"])
        self.assertEqual(outcome["pending_reports"], 1)
        conn = self._conn()
        row = conn.execute(
            "SELECT status, attempts, last_error FROM gate_repair_reports WHERE kind='github_issue'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "pending")
        self.assertEqual(row[1], 1)
        self.assertIn("gh unavailable", row[2])
        # The next tick retries the pending row and stores the issue number.
        working = self.FakeDeliver()
        outcome2 = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=working,
            runner=self.failing_runner,
        )
        self.assertFalse(outcome2["switched_off"])  # already off, not switched again
        self.assertTrue(outcome2["already_off"])
        self.assertEqual(outcome2["pending_reports"], 0)
        self.assertEqual(outcome2["repair_issue_number"], 4201)
        conn = self._conn()
        row = conn.execute(
            "SELECT status, attempts, result_json FROM gate_repair_reports WHERE kind='github_issue'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "done")
        self.assertEqual(row[1], 2)
        self.assertEqual(json.loads(row[2])["number"], 4201)

    def test_mailbox_report_is_enqueued_too(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        deliver = self.FakeDeliver()
        gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver,
            runner=self.failing_runner,
        )
        kinds = [kind for kind, _ in deliver.calls]
        self.assertEqual(kinds, ["github_issue", "mailbox"])
        conn = self._conn()
        rows = conn.execute("SELECT kind, status FROM gate_repair_reports ORDER BY id").fetchall()
        conn.close()
        self.assertEqual([(r[0], r[1]) for r in rows], [("github_issue", "done"), ("mailbox", "done")])


class PersistenceTests(GateSelfRepairTestCase):
    def test_restart_keeps_the_gate_off(self):
        from modules.experiment_service.service_parts.verification import timing_check_enabled

        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path),
            deliver=self.FakeDeliver(), runner=self.failing_runner,
        )
        # "Restart": drop every connection and re-open the same database file.
        fresh = init_db(self.db_path)
        self.assertIsNotNone(gsr.gate_disabled(fresh, "timing_check"))
        fresh.close()
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            self.assertFalse(timing_check_enabled(self.db_path))
        self.assertEqual(gsr.status(self.db_path)["timing_check"], "off")

    def test_environment_override_semantics(self):
        from modules.experiment_service.service_parts.verification import timing_check_enabled

        # Fresh DB: enabled when the environment allows it, disabled by "0".
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            self.assertTrue(timing_check_enabled(self.db_path))
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "0"}):
            self.assertFalse(timing_check_enabled(self.db_path))
        # Persisted switch-off: off even with NEXUS_TIMING_CHECK=1.
        conn = self._conn()
        gsr.disable_gate(conn, "timing_check", classification="host_blocker:timing_check", reason="r")
        conn.close()
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            self.assertFalse(timing_check_enabled(self.db_path))
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "0"}):
            self.assertFalse(timing_check_enabled(self.db_path))

    def test_missing_database_means_enabled(self):
        from modules.experiment_service.service_parts.verification import timing_check_enabled

        missing = str(Path(self.tmp.name) / "nope.db")
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            self.assertTrue(timing_check_enabled(missing))
        self.assertEqual(gsr.status(missing)["timing_check"], "on")


class ServiceIntegrationTests(GateSelfRepairTestCase):
    def _service(self):
        from modules.experiment_service import ExperimentService, STATUS_VERIFYING

        service = ExperimentService(
            db_path=self.db_path, repo_root=str(self.repo.path),
            worktrees_root=str(Path(self.tmp.name) / "worktrees"),
            experiment_log_dir=str(Path(self.tmp.name) / "logs"),
            worker_factory=lambda _: MagicMock(), plan_worker_factory=lambda _: None,
        )
        self.addCleanup(service.close)
        return service, STATUS_VERIFYING

    def test_run_timing_check_skips_when_the_gate_is_off(self):
        service, verifying = self._service()
        conn = self._conn()
        gsr.disable_gate(conn, "timing_check", classification="host_blocker:timing_check", reason="r")
        conn.close()
        experiment = service.create_experiment("work")
        service._force_status(experiment.id, verifying)
        elog = service.get_experiment_logger(experiment.id)
        # The environment allows the check; only the persisted switch-off skips it.
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            with patch("modules.timing_check.run_timing_check", side_effect=AssertionError("must not run")):
                result = service._run_timing_check(experiment.id, str(self.repo.path), "", elog)
        self.assertIsNone(result)

    def test_run_timing_check_skips_on_env_override(self):
        service, verifying = self._service()
        experiment = service.create_experiment("work")
        service._force_status(experiment.id, verifying)
        elog = service.get_experiment_logger(experiment.id)
        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "0"}):
            with patch("modules.timing_check.run_timing_check", side_effect=AssertionError("must not run")):
                result = service._run_timing_check(experiment.id, str(self.repo.path), "", elog)
        self.assertIsNone(result)


class SchemaTests(GateSelfRepairTestCase):
    def test_init_db_creates_the_pending_report_table(self):
        conn = self._conn()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gate_repair_reports'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)


class ReplayMeasureTests(GateSelfRepairTestCase):
    def test_2026_11_14_replay_switches_off_after_the_third_failure(self):
        """Measure: replay the stored 2026-11-14 sequence; the switch-off must
        happen after the third finished experiment (#709 at 19:43), not later."""
        rows = _snapshot_2026_11_14() or _EMBEDDED_2026_11_14
        self.assertGreaterEqual(len(rows), 4)
        self.assertEqual([r[2] for r in rows[:4]], [694, 705, 709, 709])
        deliver = self.FakeDeliver()
        switched_at: list[int] = []
        for index, (experiment_id, ended_at, issue, error) in enumerate(rows, start=1):
            conn = self._conn()
            self.add_finished(conn, experiment_id, ended_at, error, issue=issue)
            conn.close()
            outcome = gsr.check_and_repair(
                db_path=self.db_path, repo_root=str(self.repo.path),
                deliver=deliver, runner=self.failing_runner,
            )
            if outcome["switched_off"]:
                switched_at.append(index)
        # Exactly after the third one, and only once.
        self.assertEqual(switched_at, [3])
        github_calls = [p for kind, p in deliver.calls if kind == "github_issue"]
        self.assertEqual(len(github_calls), 1)
        self.assertEqual(github_calls[0]["title"], "Repair host blocker: timing_check")
        # After the fourth row (same issue #709 again) the last three finished
        # experiments span only two issues, so no fresh detection fires.
        conn = self._conn()
        self.assertIsNone(gsr.detect_repeated_blocker(conn))
        conn.close()
        self.assertEqual(gsr.status(self.db_path)["timing_check"], "off")


if __name__ == "__main__":
    unittest.main()
