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
# The stored rows that follow up to and including #696 (both succeeded).
_EMBEDDED_2026_11_14_AFTER = [
    ("2a2374508cfb40d382e127e0ba08f00a", "2026-11-15T10:13:15.649053+00:00", 703, None, "succeeded"),
    ("fc9688489c534e868c2e1caacc37846b", "2026-11-15T10:57:59.408410+00:00", 696, None, "succeeded"),
]


def _embedded_replay() -> list[tuple[str, str, int, str | None, str]]:
    return [(*row, "needs_review") for row in _EMBEDDED_2026_11_14] + _EMBEDDED_2026_11_14_AFTER


def _snapshot_2026_11_14() -> list[tuple[str, str, int, str | None, str]] | None:
    """The same sequence read from the stored production rows, when available:
    every finished experiment from the first #694 gate failure up to and
    including the #696 run (#694, #705, #709, #709, #703, #696)."""
    path = os.environ.get("NEXUS_PROD_DB_SNAPSHOT")
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT e.id, e.ended_at, l.issue_number, e.error, e.status FROM experiments e "
            "JOIN github_experiment_links l ON l.experiment_id=e.id "
            "WHERE e.ended_at >= '2026-11-14T18:16:53' AND e.ended_at <= '2026-11-15T10:57:59.5' "
            "ORDER BY e.ended_at"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return None
    return [(str(r[0]), str(r[1]), int(r[2]), r[3], str(r[4])) for r in rows] or None


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
        """Measure: replay the stored 2026-11-14 sequence (#694, #705, #709,
        then #709 again, #703 and #696); the switch-off must happen right
        after the third finished experiment (#709 at 19:43), only once, and
        the gate is rerun on main exactly once."""
        rows = _snapshot_2026_11_14() or _embedded_replay()
        issues = [r[2] for r in rows]
        self.assertEqual(issues[:3], [694, 705, 709])
        self.assertIn(696, issues[3:])
        deliver = self.FakeDeliver()
        runner_calls: list[list[str]] = []

        def runner(repo_path: str, tests: list[str]) -> str:
            runner_calls.append(list(tests))
            return self.failing_runner(repo_path, tests)

        switched_at: list[int] = []
        for index, (experiment_id, ended_at, issue, error, status) in enumerate(rows, start=1):
            conn = self._conn()
            self.add_finished(conn, experiment_id, ended_at, error, issue=issue, status=status)
            conn.close()
            outcome = gsr.check_and_repair(
                db_path=self.db_path, repo_root=str(self.repo.path),
                deliver=deliver, runner=runner,
            )
            if outcome["switched_off"]:
                switched_at.append(index)
        self.assertEqual(switched_at, [3])
        self.assertEqual(len(runner_calls), 1)
        github_calls = [p for kind, p in deliver.calls if kind == "github_issue"]
        self.assertEqual(len(github_calls), 1)
        self.assertEqual(github_calls[0]["title"], "Repair host blocker: timing_check")
        state = gsr.status(self.db_path)
        self.assertEqual(state["timing_check"], "off")
        self.assertEqual(sorted(state["state"]["issues"]), [694, 705, 709])
        self.assertEqual(state["pending_reports"], 0)


class RerunVerdictTests(GateSelfRepairTestCase):
    """Review finding (1): only the same gate failing the same way on main
    switches the gate off; a timing-sensitive or verdict-less rerun never does."""

    REASON = "timing check failed closed: error: these tests fail on this host without drift too"

    def _run(self, runner):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        deliver = self.FakeDeliver()
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver, runner=runner,
        )
        return outcome, deliver

    def assert_nothing_switched(self, outcome, deliver, verdict):
        self.assertFalse(outcome["switched_off"])
        self.assertEqual(outcome["timing_check"], "on")
        self.assertEqual(outcome["rerun"]["verdict"], verdict)
        self.assertEqual(outcome["pending_reports"], 0)
        self.assertEqual(deliver.calls, [])
        conn = self._conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM gate_repair_reports").fetchone()[0], 0)
        from modules.experiment_service.service_parts.verification import timing_check_enabled

        with patch.dict(os.environ, {"NEXUS_TIMING_CHECK": "1"}):
            self.assertTrue(timing_check_enabled(self.db_path))

    def test_timing_sensitive_on_main_switches_nothing_off(self):
        outcome, deliver = self._run(lambda repo, tests: "timing_sensitive: " + ", ".join(tests))
        self.assert_nothing_switched(outcome, deliver, gsr.VERDICT_TIMING_SENSITIVE)

    def test_error_without_verdict_switches_nothing_off(self):
        for result in (
            "error",
            "error: control run without drift gave no verdict",
            "error: watchdog stopped the rerun (tests/test_a.py::T::test_x)",
            "error: none of the test ids exist on main",
        ):
            with self.subTest(result=result):
                self.setUp()
                outcome, deliver = self._run(lambda repo, tests, result=result: result)
                self.assert_nothing_switched(outcome, deliver, gsr.VERDICT_NO_VERDICT)

    def test_crashing_runner_switches_nothing_off(self):
        def runner(repo, tests):
            raise RuntimeError("pytest child crashed")

        outcome, deliver = self._run(runner)
        self.assert_nothing_switched(outcome, deliver, gsr.VERDICT_NO_VERDICT)
        self.assertIn("pytest child crashed", outcome["rerun"]["result"])

    def test_missing_main_is_no_verdict(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        self.repo.git("branch", "-m", "main", "trunk")
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path),
            deliver=self.FakeDeliver(), runner=self.failing_runner,
        )
        self.assertFalse(outcome["switched_off"])
        self.assertEqual(outcome["rerun"]["verdict"], gsr.VERDICT_NO_VERDICT)

    def test_classify_rerun(self):
        fails = gsr.FAILS_WITHOUT_DRIFT_RESULT + "tests/test_a.py::T::test_x"
        self.assertEqual(gsr.classify_rerun("ok", self.REASON), gsr.VERDICT_PASSED)
        self.assertEqual(gsr.classify_rerun(fails, self.REASON), gsr.VERDICT_FAILED)
        self.assertEqual(gsr.classify_rerun(fails, "some other reason"), gsr.VERDICT_NO_VERDICT)
        self.assertEqual(gsr.classify_rerun(gsr.FAILS_WITHOUT_DRIFT_RESULT, self.REASON), gsr.VERDICT_NO_VERDICT)
        self.assertEqual(
            gsr.classify_rerun("timing_sensitive: tests/test_a.py::T::test_x", self.REASON),
            gsr.VERDICT_TIMING_SENSITIVE,
        )
        for result in (None, "", "error", "error: interrupted (KeyboardInterrupt)"):
            self.assertEqual(gsr.classify_rerun(result, self.REASON), gsr.VERDICT_NO_VERDICT)


class DefaultGateRunnerTests(GateSelfRepairTestCase):
    """The default runner repeats the gate itself: drift run in the
    verification env on the same test ids, then the control run without drift."""

    IDS = ["tests/test_a.py::T::test_x", "tests/test_b.py::T::test_y"]

    def setUp(self) -> None:
        super().setUp()
        self.worktree = Path(self.tmp.name) / "wt"
        (self.worktree / "tests").mkdir(parents=True)
        for name in ("test_a.py", "test_b.py"):
            (self.worktree / "tests" / name).write_text("", encoding="utf-8")

    def _run(self, drift_result, control_result=None, collected=None):
        from modules import timing_check as tc

        drift = MagicMock(return_value=drift_result)
        control = MagicMock(return_value=control_result)
        with patch.object(tc, "_collected_ids", return_value=set(self.IDS) if collected is None else collected), \
                patch.object(tc, "run_timing_check", drift), patch.object(tc, "control_check", control):
            result = gsr.default_gate_runner(python="/py")(str(self.worktree), list(self.IDS))
        return result, drift, control

    def test_fails_without_drift_on_main_reproduces_the_gate_reason(self):
        result, drift, control = self._run("timing_sensitive: " + ", ".join(self.IDS), ([], list(self.IDS)))
        self.assertEqual(result, gsr.FAILS_WITHOUT_DRIFT_RESULT + ", ".join(self.IDS))
        self.assertEqual(gsr.classify_rerun(result, RerunVerdictTests.REASON), gsr.VERDICT_FAILED)
        # Same test ids, the drift (verification) environment, the given interpreter.
        args, kwargs = drift.call_args
        self.assertEqual(args[1], self.IDS)
        self.assertEqual(kwargs["python"], "/py")
        from modules.verification_env import CLOCK_DRIFT_ENV

        self.assertEqual(kwargs["env"][CLOCK_DRIFT_ENV], "50")
        self.assertEqual(control.call_args[0][1], self.IDS)

    def test_drift_only_on_main_is_timing_sensitive(self):
        result, _, _ = self._run(
            "timing_sensitive: " + ", ".join(self.IDS), ([self.IDS[0]], [self.IDS[1]]),
        )
        self.assertEqual(result, "timing_sensitive: " + self.IDS[0])
        self.assertEqual(gsr.classify_rerun(result, RerunVerdictTests.REASON), gsr.VERDICT_TIMING_SENSITIVE)

    def test_control_without_verdict_is_no_verdict(self):
        result, _, _ = self._run("timing_sensitive: " + self.IDS[0], None)
        self.assertEqual(gsr.classify_rerun(result, RerunVerdictTests.REASON), gsr.VERDICT_NO_VERDICT)

    def test_passing_drift_run_skips_the_control_run(self):
        result, _, control = self._run("ok")
        self.assertEqual(result, "ok")
        control.assert_not_called()

    def test_drift_error_is_no_verdict(self):
        result, _, control = self._run("error")
        self.assertEqual(gsr.classify_rerun(result, RerunVerdictTests.REASON), gsr.VERDICT_NO_VERDICT)
        control.assert_not_called()

    def test_only_test_ids_present_on_main_are_run(self):
        result, drift, _ = self._run("ok", collected={self.IDS[1]})
        self.assertEqual(drift.call_args[0][1], [self.IDS[1]])
        result, drift, _ = self._run("ok", collected=set())
        self.assertEqual(gsr.classify_rerun(result, RerunVerdictTests.REASON), gsr.VERDICT_NO_VERDICT)
        drift.assert_not_called()

    def test_hung_rerun_is_no_verdict(self):
        from modules import timing_check as tc
        from modules.verification_runner import HungTest

        with patch.object(tc, "_collected_ids", return_value=set(self.IDS)), \
                patch.object(tc, "run_timing_check", side_effect=HungTest("tests/test_a.py::T::test_x")):
            result = gsr.default_gate_runner()(str(self.worktree), list(self.IDS))
        self.assertEqual(gsr.classify_rerun(result, RerunVerdictTests.REASON), gsr.VERDICT_NO_VERDICT)


class RealRerunOnMainTests(GateSelfRepairTestCase):
    """End to end with real pytest children on a main worktree."""

    def _main_with_test(self, body: str) -> None:
        tests = self.repo.path / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_host.py").write_text(body, encoding="utf-8")
        self.repo.git("add", ".")
        self.repo.git("commit", "-q", "-m", "test")

    def _three(self, test_id: str) -> None:
        conn = self._conn()
        for name, ended, issue in (("exp-a", "2026-11-14T18:16:53+00:00", 694),
                                   ("exp-b", "2026-11-14T18:59:31+00:00", 705),
                                   ("exp-c", "2026-11-14T19:43:24+00:00", 709)):
            self.add_finished(conn, name, ended, gate_error(test_id), issue=issue)
        conn.close()

    def test_test_failing_on_main_without_drift_switches_the_gate_off(self):
        self._main_with_test("def test_host():\n    assert False, 'host state'\n")
        self._three("tests/test_host.py::test_host")
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=self.FakeDeliver(),
        )
        self.assertEqual(outcome["rerun"]["verdict"], gsr.VERDICT_FAILED, outcome["rerun"])
        self.assertTrue(outcome["switched_off"])
        # The temporary main worktree is gone again.
        listed = self.repo.git("worktree", "list")
        self.assertEqual(len(listed.strip().splitlines()), 1, listed)

    def test_test_passing_on_main_switches_nothing_off(self):
        self._main_with_test("def test_host():\n    assert True\n")
        self._three("tests/test_host.py::test_host")
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=self.FakeDeliver(),
        )
        self.assertEqual(outcome["rerun"]["verdict"], gsr.VERDICT_PASSED, outcome["rerun"])
        self.assertFalse(outcome["switched_off"])

    def test_test_missing_on_main_is_no_verdict(self):
        self._three("tests/test_host.py::test_host")
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=self.FakeDeliver(),
        )
        self.assertEqual(outcome["rerun"]["verdict"], gsr.VERDICT_NO_VERDICT, outcome["rerun"])
        self.assertFalse(outcome["switched_off"])


class RerunOnceTests(GateSelfRepairTestCase):
    """Review finding (2): the gate is rerun on main once per triple, not every tick."""

    def _tick(self, runner):
        return gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path),
            deliver=self.FakeDeliver(), runner=runner,
        )

    def _counting(self, result):
        calls: list[list[str]] = []

        def runner(repo, tests):
            calls.append(list(tests))
            if isinstance(result, BaseException):
                raise result
            return result

        return runner, calls

    def test_passing_rerun_is_not_repeated_on_the_next_ticks(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        runner, calls = self._counting("ok")
        first = self._tick(runner)
        self.assertEqual(first["rerun"]["verdict"], gsr.VERDICT_PASSED)
        for _ in range(3):
            later = self._tick(runner)
            self.assertEqual(later["rerun_skipped"], "already_checked")
            self.assertNotIn("rerun", later)
            self.assertFalse(later["switched_off"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(gsr.status(self.db_path)["last_rerun"]["verdict"], gsr.VERDICT_PASSED)

    def test_no_verdict_rerun_is_not_repeated(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        runner, calls = self._counting("error")
        self._tick(runner)
        # A later failing runner must not be asked: the triple was checked.
        failing, failing_calls = self._counting(gsr.FAILS_WITHOUT_DRIFT_RESULT + "tests/test_a.py::T::test_x")
        outcome = self._tick(failing)
        self.assertEqual(len(calls), 1)
        self.assertEqual(failing_calls, [])
        self.assertFalse(outcome["switched_off"])

    def test_a_new_triple_gets_its_own_single_rerun(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        runner, calls = self._counting("ok")
        self._tick(runner)
        conn = self._conn()
        self.add_finished(
            conn, "exp-d", "2026-11-14T21:00:00+00:00",
            gate_error("tests/test_d.py::T::test_w"), issue=720,
        )
        conn.close()
        self._tick(runner)
        self._tick(runner)
        self.assertEqual(len(calls), 2)

    def test_interrupted_rerun_is_never_repeated_and_switches_nothing_off(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        runner, calls = self._counting(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self._tick(runner)
        failing, failing_calls = self._counting(gsr.FAILS_WITHOUT_DRIFT_RESULT + "tests/test_a.py::T::test_x")
        outcome = self._tick(failing)
        self.assertEqual(outcome["rerun_skipped"], "in_progress_or_interrupted")
        self.assertEqual(outcome["rerun_record"]["status"], gsr.RERUN_RUNNING)
        self.assertEqual(failing_calls, [])
        self.assertFalse(outcome["switched_off"])
        self.assertEqual(outcome["timing_check"], "on")

    def test_a_second_claim_of_the_same_triple_is_refused(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        detection = gsr.detect_repeated_blocker(conn)
        _, first = gsr.claim_rerun(conn, detection)
        other = sqlite3.connect(self.db_path)
        self.addCleanup(other.close)
        record, second = gsr.claim_rerun(other, detection)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(record["status"], gsr.RERUN_RUNNING)

    def test_manual_re_enable_is_not_undone_by_the_same_triple(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        runner, calls = self._counting(gsr.FAILS_WITHOUT_DRIFT_RESULT + "tests/test_a.py::T::test_x")
        self.assertTrue(self._tick(runner)["switched_off"])
        conn = self._conn()
        conn.execute("UPDATE service_settings SET value='[]' WHERE key=?", (gsr.SETTING_DISABLED_GATES,))
        conn.commit()
        conn.close()
        outcome = self._tick(runner)
        self.assertFalse(outcome["switched_off"])
        self.assertEqual(outcome["timing_check"], "on")
        self.assertEqual(len(calls), 1)


class AtomicSwitchOffTests(GateSelfRepairTestCase):
    """Review finding (3): the switch-off and its pending reports commit together."""

    FAIL = staticmethod(GateSelfRepairTestCase.failing_runner)

    def _prepare(self):
        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()

    def _report_rows(self):
        conn = self._conn()
        return conn.execute("SELECT kind, status FROM gate_repair_reports ORDER BY id").fetchall()

    def test_crash_while_writing_the_reports_rolls_the_switch_off_back(self):
        self._prepare()
        real_insert = gsr._insert_missing_reports

        def crash(conn, entry, now):
            conn.execute(
                "INSERT INTO gate_repair_reports (kind, payload_json, report_key, created_at, updated_at) "
                "VALUES ('github_issue', '{}', 'partial', ?, ?)", (now, now),
            )
            raise sqlite3.OperationalError("database is locked")

        runner_calls = []

        def runner(repo, tests):
            runner_calls.append(tests)
            return self.FAIL(repo, tests)

        with patch.object(gsr, "_insert_missing_reports", crash):
            with self.assertRaises(sqlite3.OperationalError):
                gsr.check_and_repair(
                    db_path=self.db_path, repo_root=str(self.repo.path),
                    deliver=self.FakeDeliver(), runner=runner,
                )
        # Nothing of the switch-off survived: gate on, no (partial) reports.
        state = gsr.status(self.db_path)
        self.assertEqual(state["timing_check"], "on")
        self.assertEqual(self._report_rows(), [])
        self.assertFalse(state["last_rerun"]["applied"])
        self.assertEqual(state["last_rerun"]["verdict"], gsr.VERDICT_FAILED)
        # The next tick completes the switch-off from the stored rerun, without rerunning.
        self.assertIs(gsr._insert_missing_reports, real_insert)
        deliver = self.FakeDeliver()
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver, runner=runner,
        )
        self.assertTrue(outcome["switched_off"])
        self.assertEqual(outcome["rerun_skipped"], "already_checked")
        self.assertEqual(len(runner_calls), 1)
        self.assertEqual([tuple(r) for r in self._report_rows()], [("github_issue", "done"), ("mailbox", "done")])
        self.assertEqual(outcome["repair_issue_number"], 4201)

    def test_crash_between_rerun_and_switch_off_is_completed_next_tick(self):
        self._prepare()
        runner_calls = []

        def runner(repo, tests):
            runner_calls.append(tests)
            return self.FAIL(repo, tests)

        with patch.object(gsr, "disable_gate", side_effect=RuntimeError("process killed")):
            with self.assertRaises(RuntimeError):
                gsr.check_and_repair(
                    db_path=self.db_path, repo_root=str(self.repo.path),
                    deliver=self.FakeDeliver(), runner=runner,
                )
        self.assertEqual(gsr.status(self.db_path)["timing_check"], "on")
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path),
            deliver=self.FakeDeliver(), runner=runner,
        )
        self.assertTrue(outcome["switched_off"])
        self.assertEqual(len(runner_calls), 1)
        # And only once: a further tick is already_off, no new reports.
        again = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path),
            deliver=self.FakeDeliver(), runner=runner,
        )
        self.assertTrue(again["already_off"])
        self.assertEqual(len(self._report_rows()), 2)

    def test_switch_off_without_reports_gets_them_on_the_already_off_path(self):
        # A switch-off written without its report rows (by hand or an older version).
        conn = self._conn()
        conn.execute(
            "INSERT INTO service_settings (key, value, updated_at) VALUES (?, ?, 'now')",
            (gsr.SETTING_DISABLED_GATES, json.dumps([{
                "gate": "timing_check", "classification": "host_blocker:timing_check", "reason": "r",
            }])),
        )
        conn.commit()
        conn.close()
        deliver = self.FakeDeliver()
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver, runner=self.FAIL,
        )
        self.assertTrue(outcome["already_off"])
        self.assertEqual([k for k, _ in deliver.calls], ["github_issue", "mailbox"])
        self.assertEqual(outcome["repair_issue_number"], 4201)
        # Running again neither re-enqueues nor re-delivers.
        deliver2 = self.FakeDeliver()
        gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver2, runner=self.FAIL,
        )
        self.assertEqual(deliver2.calls, [])
        self.assertEqual(len(self._report_rows()), 2)

    def test_crash_after_delivery_before_marking_done_keeps_the_row_pending(self):
        self._prepare()
        deliver = self.FakeDeliver()
        with patch.object(gsr, "_store_repair_issue_number", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                gsr.check_and_repair(
                    db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver, runner=self.FAIL,
                )
        # The switch-off and both reports are committed; the GitHub row stays pending.
        state = gsr.status(self.db_path)
        self.assertEqual(state["timing_check"], "off")
        self.assertEqual(state["pending_reports"], 2)
        outcome = gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver, runner=self.FAIL,
        )
        self.assertEqual(outcome["pending_reports"], 0)
        self.assertIsNotNone(outcome["repair_issue_number"])

    def test_retrying_twice_after_success_sends_nothing_more(self):
        self._prepare()
        deliver = self.FakeDeliver()
        gsr.check_and_repair(db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver, runner=self.FAIL)
        sent = len(deliver.calls)
        for _ in range(2):
            gsr.check_and_repair(db_path=self.db_path, repo_root=str(self.repo.path), deliver=deliver, runner=self.FAIL)
        self.assertEqual(len(deliver.calls), sent)

    def test_report_of_an_earlier_switch_off_does_not_mislabel_the_current_one(self):
        conn = self._conn()
        gsr.ensure_schema(conn)
        entry = gsr.disable_gate(conn, "timing_check", classification="host_blocker:timing_check", reason="r")
        conn.execute("UPDATE gate_repair_reports SET status='done'")
        conn.commit()
        gsr.enqueue_report(conn, "github_issue", {"title": "t", "switch_off_id": "older", "report_key": "older:gh"})
        gsr.retry_pending_reports(conn, self.FakeDeliver())
        self.assertIsNone(gsr.gate_disabled(conn, "timing_check")["repair_issue_number"])
        self.assertEqual(gsr.gate_disabled(conn, "timing_check")["switch_off_id"], entry["switch_off_id"])


class IdempotentDeliveryTests(GateSelfRepairTestCase):
    def test_gh_reuses_an_open_issue_with_the_same_title(self):
        listed = subprocess.CompletedProcess(
            [], 0, json.dumps([{"number": 77, "title": "Repair host blocker: timing_check", "url": "u"}]), "",
        )
        with patch.object(gsr.subprocess, "run", return_value=listed) as run:
            result = gsr._gh_create_issue("/repo", "Repair host blocker: timing_check", "body")
        self.assertEqual(result["number"], 77)
        self.assertTrue(result["reused"])
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("create", run.call_args[0][0])

    def test_gh_creates_without_label_when_none_is_open(self):
        responses = [
            subprocess.CompletedProcess([], 0, json.dumps([{"number": 5, "title": "Repair host blocker: timing_check (old)"}]), ""),
            subprocess.CompletedProcess([], 0, "https://github.com/o/r/issues/901\n", ""),
        ]
        with patch.object(gsr.subprocess, "run", side_effect=responses) as run:
            result = gsr._gh_create_issue("/repo", "Repair host blocker: timing_check", "body")
        self.assertEqual(result["number"], 901)
        create = run.call_args_list[1][0][0]
        self.assertIn("create", create)
        self.assertNotIn("--label", create)

    def test_gh_list_failure_raises_so_the_row_stays_pending(self):
        failed = subprocess.CompletedProcess([], 1, "", "network down")
        with patch.object(gsr.subprocess, "run", return_value=failed) as run:
            with self.assertRaises(RuntimeError):
                gsr._gh_create_issue("/repo", "Repair host blocker: timing_check", "body")
        self.assertEqual(run.call_count, 1)

    def test_mailbox_post_twice_writes_one_file(self):
        outbox = Path(self.tmp.name) / "outbox"
        payload = {"title": "Repair host blocker: timing_check", "body": "b",
                   "report_key": "abc:mailbox", "created_at": "2026-11-14T19:43:24+00:00"}
        first = gsr._mailbox_post(payload, outbox)
        second = gsr._mailbox_post(payload, outbox)
        self.assertEqual(first["outbox_file"], second["outbox_file"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(list(outbox.iterdir())), 1)
        text = (outbox / first["outbox_file"]).read_text(encoding="utf-8")
        self.assertIn("type: report", text)
        self.assertIn("created_by: nexus", text)


class TickIntegrationTests(GateSelfRepairTestCase):
    def test_tick_passes_the_verification_interpreter_and_watchdog(self):
        import nexus_tick
        from modules.verification_runner import WatchdogConfig

        watchdog = WatchdogConfig(stall_timeout_seconds=9, check_interval_seconds=1, safety_cap_seconds=99)
        service = MagicMock()
        service.experiment_service.test_python_executable = "/venv/bin/python"
        service.experiment_service._get_test_watchdog_config.return_value = watchdog
        with patch.object(gsr, "check_and_repair", return_value={"switched_off": False}) as check:
            nexus_tick._gate_self_repair_tick(self.db_path, str(self.repo.path), service)
        kwargs = check.call_args.kwargs
        self.assertEqual(kwargs["python"], "/venv/bin/python")
        self.assertIs(kwargs["watchdog"], watchdog)

    def test_tick_survives_a_failing_check(self):
        import nexus_tick

        with patch.object(gsr, "check_and_repair", side_effect=sqlite3.OperationalError("locked")):
            nexus_tick._gate_self_repair_tick(self.db_path, str(self.repo.path), None)

    def test_status_issue_shows_pending_reports(self):
        from modules import nexus_status_issue

        conn = self._conn()
        self.add_three_same_reason(conn)
        conn.close()
        gsr.check_and_repair(
            db_path=self.db_path, repo_root=str(self.repo.path),
            deliver=self.FakeDeliver(fail_github=True), runner=self.failing_runner,
        )
        conn = self._conn()
        body = nexus_status_issue.render(
            conn, now=datetime.now(timezone.utc), budget=10, spent=1, paused=False, busy=None,
        )
        self.assertIn("switched off: timing_check", body)
        self.assertIn("1 report(s) pending", body)


class SchemaMigrationTests(GateSelfRepairTestCase):
    def test_an_older_report_table_gets_the_report_key_column(self):
        path = str(Path(self.tmp.name) / "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE gate_repair_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
            "last_error TEXT NULL, result_json TEXT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        conn = init_db(path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(gate_repair_reports)")}
        conn.close()
        self.assertIn("report_key", columns)
        # Idempotent.
        init_db(path).close()

if __name__ == "__main__":
    unittest.main()
