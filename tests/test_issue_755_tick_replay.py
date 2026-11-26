"""Counterfactual tick replay (#755).

The replay tests use fixture rows only: no DB, no network. The store/route
tests at the end use a temporary SQLite file.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import api
from modules import nexus_initiative, tick_replay, tick_replay_store
from modules.coding_provider import DEFAULT_LIMIT_COOLDOWN
from modules.nexus_mcp_tools import READ_ONLY_TOOLS
from modules.run_store import init_db

REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)
WEEKLY_LIMIT = "You've hit your weekly limit · resets 12pm (UTC)"
EMPTY_USAGE = json.dumps({"input_tokens": 0, "output_tokens": 0})
USED = json.dumps({"input_tokens": 52000, "output_tokens": 4100})


def _row(exp_id, minute, *, issue, provider, status="failed", error=None, seconds=600.0,
         usage=USED, merged=False, has_diff=False, start=T0):
    started = start + timedelta(minutes=minute)
    return {
        "id": exp_id,
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(seconds=seconds)).isoformat(),
        "issue": issue,
        "provider": provider,
        "status": status,
        "error": error,
        "implementation_usage_json": usage,
        "duration": seconds,
        "merged": merged,
        "has_diff": has_diff,
    }


def sequence_2026_11_18():
    """The 2026-11-18 08:00-10:00 starts on #745/#746 (#753): 5 Claude runs that
    ended on the weekly limit and 9 Codex runs of about 4 s with empty usage,
    one start roughly every 8 minutes, all without changes."""
    rows = []
    claude_minutes = (0, 17, 42, 68, 93)
    codex_minutes = (8, 25, 34, 51, 59, 76, 85, 101, 110)
    for n, minute in enumerate(claude_minutes):
        rows.append(_row(f"claude-{n}", minute, issue=745 + n % 2, provider="claude",
                         error=WEEKLY_LIMIT, seconds=6.0, usage=None))
        # The limit text came after a few seconds: ended, no usage.
    for n, minute in enumerate(codex_minutes):
        rows.append(_row(f"codex-{n}", minute, issue=745 + n % 2, provider="codex",
                         error="codex exited 1", seconds=4.0, usage=EMPTY_USAGE))
    return rows


def normal_merges(count=8, start=T0 + timedelta(days=1)):
    return [
        _row(f"ok-{n}", 45 * n, issue=800 + n, provider=("codex", "claude")[n % 2],
             status="succeeded", seconds=900.0, merged=True, has_diff=True, start=start)
        for n in range(count)
    ]


class SequenceTests(unittest.TestCase):
    def test_2026_11_18_sequence_saves_at_least_12_of_14(self):
        rows = sequence_2026_11_18()
        self.assertEqual(len(rows), 14)
        report = tick_replay.replay(
            rows, ["provider_cooldown", "issue_loop_guard"],
            T0, T0 + timedelta(hours=2),
        )
        self.assertEqual(report.actual_paid_starts, 14)
        self.assertLessEqual(report.simulated_paid_starts, 2)
        self.assertGreaterEqual(report.saved_starts, 12)
        self.assertEqual(report.lost_merges, [])
        self.assertTrue(all(row["policy"] == "provider_cooldown" for row in report.skipped))
        self.assertIn("providers_cooling_down", report.skipped[0]["reason"])

    def test_simulated_history_also_stops_most_starts(self):
        report = tick_replay.replay(
            sequence_2026_11_18(), ["provider_cooldown", "issue_loop_guard"],
            T0, T0 + timedelta(hours=2), history="simulated",
        )
        # One Codex probe per expired 1 h cooldown may run again.
        self.assertLessEqual(report.simulated_paid_starts, 3)
        self.assertGreaterEqual(report.saved_starts, 11)

    def test_loop_guard_alone_stops_the_fifth_quick_failure_on_an_issue(self):
        rows = [
            _row(f"x{n}", 10 * n, issue=745, provider="codex", seconds=60.0)
            for n in range(6)
        ]
        report = tick_replay.replay(rows, ["issue_loop_guard"])
        # Starts 0..3 run; their 4 no-change ends within 1 h stop the rest.
        self.assertEqual(report.simulated_paid_starts, 4)
        self.assertEqual(report.saved_starts, 2)
        self.assertIn("skipped_loop_guard: #745", report.skipped[0]["reason"])

    def test_loop_guard_ignores_runs_with_a_diff_and_other_issues(self):
        rows = [
            _row(f"x{n}", 10 * n, issue=745 + n, provider="codex", seconds=60.0)
            for n in range(6)
        ] + [
            _row(f"d{n}", 70 + 10 * n, issue=900, provider="codex", seconds=60.0, has_diff=True)
            for n in range(6)
        ]
        report = tick_replay.replay(rows, ["issue_loop_guard"])
        self.assertEqual(report.saved_starts, 0)

    def test_same_gate_reason_on_three_issues_stops_starts(self):
        reason = ("timing check failed closed: error: these tests fail on this host without "
                  "drift too, so there is no timing verdict: tests/test_{n}.py::T::t")
        rows = [
            _row(f"g{n}", 30 * n, issue=694 + n, provider="codex", status="needs_review",
                 error=reason.format(n=n), has_diff=True)
            for n in range(5)
        ]
        report = tick_replay.replay(rows, ["same_gate_reason_x3"])
        self.assertEqual(report.simulated_paid_starts, 3)
        self.assertEqual(report.saved_starts, 2)
        self.assertIn("host_blocker: timing check failed closed", report.skipped[0]["reason"])

    def test_same_gate_reason_on_one_issue_or_mixed_reasons_is_no_blocker(self):
        same_issue = [
            _row(f"s{n}", 30 * n, issue=694, provider="codex", status="failed",
                 error="tests failed: 3 failures", has_diff=True)
            for n in range(5)
        ]
        mixed = [
            _row(f"m{n}", 30 * n, issue=700 + n, provider="codex", status="failed",
                 error=("tests failed: x", "diff check failed: y")[n % 2], has_diff=True)
            for n in range(5)
        ]
        for rows in (same_issue, mixed):
            self.assertEqual(tick_replay.replay(rows, ["same_gate_reason_x3"]).saved_starts, 0)

    def test_window_limits_candidates_but_earlier_rows_still_count(self):
        rows = sequence_2026_11_18()
        report = tick_replay.replay(rows, ["provider_cooldown"], T0 + timedelta(minutes=30))
        # Both providers already cooled before 08:30: nothing in the window runs.
        self.assertEqual(report.simulated_paid_starts, 0)
        self.assertEqual(report.actual_paid_starts, 10)

    def test_a_working_run_lifts_the_cooldown_of_its_provider(self):
        rows = [
            _row("dead", 0, issue=1, provider="codex", error="codex exited 1", seconds=4.0,
                 usage=EMPTY_USAGE),
            _row("back", 5, issue=2, provider="codex", status="succeeded", seconds=300.0,
                 merged=True, has_diff=True),
            _row("next", 20, issue=3, provider="codex", status="succeeded", seconds=300.0,
                 merged=True, has_diff=True),
        ]
        report = tick_replay.replay(rows, ["provider_cooldown"])
        # "back" started while codex was cooling: a lost merge the report shows;
        # its ended run proves codex works, so "next" runs.
        self.assertEqual([r["id"] for r in report.lost_merges], ["back"])
        self.assertEqual(report.simulated_paid_starts, 2)

    def test_merges_during_the_sequence_show_up_as_lost(self):
        rows = sequence_2026_11_18() + normal_merges(start=T0)
        report = tick_replay.replay(rows, ["provider_cooldown"], T0, T0 + timedelta(hours=2))
        self.assertTrue(report.lost_merges)
        self.assertTrue(all(row["merged"] for row in report.lost_merges))

    def test_local_runs_are_not_paid_starts(self):
        rows = sequence_2026_11_18() + [
            _row("local-1", 50, issue=745, provider="local", seconds=3.0, usage=None),
        ]
        report = tick_replay.replay(rows, ["provider_cooldown"])
        self.assertEqual(report.actual_paid_starts, 14)
        self.assertNotIn("local-1", [row["id"] for row in report.skipped])


class NormalWorkTests(unittest.TestCase):
    def test_normal_merges_give_no_skips_for_every_policy(self):
        rows = normal_merges()
        for name in tick_replay.POLICIES:
            with self.subTest(policy=name):
                report = tick_replay.replay(rows, [name])
                self.assertEqual(report.saved_starts, 0)
                self.assertEqual(report.skipped, [])
                self.assertEqual(report.lost_merges, [])
                self.assertEqual(report.actual_paid_starts, len(rows))
        report = tick_replay.replay(rows, None)
        self.assertEqual(report.policies, list(tick_replay.POLICIES))
        self.assertEqual(report.saved_starts, 0)

    def test_a_policy_that_skips_a_merged_run_reports_a_lost_merge(self):
        def skip_issue_803(history, candidate):
            return ("skip", "test") if candidate.issue == 803 else "allow"

        report = tick_replay.replay(normal_merges(), {"skip_803": skip_issue_803})
        self.assertEqual(report.saved_starts, 1)
        self.assertEqual([row["id"] for row in report.lost_merges], ["ok-3"])
        self.assertEqual(report.lost_merges[0]["policy"], "skip_803")
        self.assertEqual(report.per_policy["skip_803"], {"saved_starts": 1, "lost_merges": 1})
        self.assertEqual(report.to_dict()["lost_merge_count"], 1)

    def test_unknown_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            tick_replay.replay([], "provider_cooldown,nope")

    def test_report_text_and_dict(self):
        report = tick_replay.replay(sequence_2026_11_18(), "provider_cooldown,issue_loop_guard")
        text = tick_replay.format_report(report)
        self.assertIn("paid starts actual: 14", text)
        self.assertIn("first 5 skipped:", text)
        data = report.to_dict()
        self.assertEqual(len(data["first_skipped"]), 5)
        json.dumps(data)  # serializable


class PurityTests(unittest.TestCase):
    def test_same_input_same_report(self):
        rows = sequence_2026_11_18() + normal_merges()
        first = tick_replay.replay(rows, None, history="simulated")
        second = tick_replay.replay(list(reversed(rows)), None, history="simulated")
        self.assertEqual(first, second)
        self.assertEqual(tick_replay.replay(rows, None), tick_replay.replay(rows, None))

    def test_replay_makes_no_db_or_subprocess_call(self):
        boom = MagicMock(side_effect=AssertionError("I/O during replay"))
        with patch.object(sqlite3, "connect", boom), patch.object(subprocess, "Popen", boom), \
                patch.object(subprocess, "run", boom):
            tick_replay.replay(sequence_2026_11_18() + normal_merges(), None)
        boom.assert_not_called()

    def test_module_source_has_no_io(self):
        source = (REPO / "modules" / "tick_replay.py").read_text(encoding="utf-8")
        for needle in ("import sqlite3", "import subprocess", "open(", "datetime.now", "requests"):
            self.assertNotIn(needle, source)

    def test_constants_match_the_live_rules(self):
        self.assertEqual(tick_replay.LIMIT_COOLDOWN, DEFAULT_LIMIT_COOLDOWN)
        self.assertEqual(tick_replay.LOOP_GUARD_MAX_NO_CHANGE, nexus_initiative._LOOP_GUARD_MAX_NO_CHANGE)
        self.assertEqual(tick_replay.LOOP_GUARD_WINDOW, nexus_initiative._LOOP_GUARD_WINDOW)
        self.assertEqual(tick_replay.LOOP_GUARD_PAUSE, nexus_initiative._LOOP_GUARD_PAUSE)
        self.assertEqual(tick_replay.NO_CHANGE_STATUSES, nexus_initiative._NO_CHANGE_STATUSES)


class RowTests(unittest.TestCase):
    def test_limit_reset_is_parsed_from_the_end_time(self):
        row = tick_replay.ReplayRow.from_mapping(
            _row("c", 0, issue=1, provider="claude", error=WEEKLY_LIMIT, seconds=6.0))
        self.assertEqual(row.limit_until, datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc))
        self.assertTrue(row.quick_empty is False)  # it had usage

    def test_unfinished_rows_are_left_out(self):
        self.assertIsNone(tick_replay.ReplayRow.from_mapping({"id": "x", "started_at": T0}))

    def test_usage_is_empty(self):
        for value in (None, "", "{}", "null", EMPTY_USAGE, {"a": {"b": 0}}):
            self.assertTrue(tick_replay.usage_is_empty(value), value)
        for value in (USED, {"cost": 0.01}, "not json"):
            self.assertFalse(tick_replay.usage_is_empty(value), value)


# --- store, script and route (temporary SQLite file) ----------------------------


def _db_with_sequence() -> str:
    db = str(Path(tempfile.mkdtemp(prefix="nexus-755-")) / "agent.db")
    init_db(db).close()
    conn = sqlite3.connect(db)
    for row in sequence_2026_11_18() + normal_merges():
        conn.execute(
            "INSERT INTO experiments (id, goal, status, created_at, implementation_started_at, "
            "ended_at, implementation_provider, error, implementation_usage_json, "
            "implementation_runtime_ms, diff_text, source_type, source_id) "
            "VALUES (?, 'g', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'github_issue', ?)",
            (row["id"], row["status"], row["started_at"], row["started_at"], row["ended_at"],
             row["provider"], row["error"], row["implementation_usage_json"],
             int(row["duration"] * 1000), "diff" if row["has_diff"] else None, str(row["issue"])),
        )
        if row["merged"]:
            conn.execute(
                "INSERT INTO experiment_adoptions (id, experiment_id, status, created_at, updated_at) "
                "VALUES (?, ?, 'adopted', ?, ?)",
                (f"a-{row['id']}", row["id"], row["ended_at"], row["ended_at"]),
            )
    conn.commit()
    conn.close()
    return db


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = _db_with_sequence()

    def test_db_rows_give_the_fixture_report(self):
        report, seconds = tick_replay_store.report_from_db(
            self.db, "2026-11-18", "provider_cooldown,issue_loop_guard", until="2026-11-18T10:00:00Z",
        )
        self.assertEqual(report.actual_paid_starts, 14)
        self.assertLessEqual(report.simulated_paid_starts, 2)
        self.assertLess(seconds, 10)

    def test_merges_are_read_from_adoptions(self):
        rows = tick_replay_store.load_rows(self.db, T0)
        merged = {r["id"] for r in rows if r["merged"]}
        self.assertEqual(merged, {f"ok-{n}" for n in range(8)})
        self.assertEqual({r["issue"] for r in rows if r["id"].startswith("claude")}, {"745", "746"})

    def test_script_prints_text_and_json(self):
        script = REPO / "scripts" / "tick_replay.py"
        args = ["python3", str(script), "--db", self.db, "--since", "2026-11-18",
                "--policy", "provider_cooldown,issue_loop_guard"]
        text = subprocess.run(args, capture_output=True, text=True, timeout=60, check=True).stdout
        self.assertIn("paid starts actual: 22", text)
        self.assertIn("first 5 skipped:", text)
        data = json.loads(subprocess.run(args + ["--json"], capture_output=True, text=True,
                                         timeout=60, check=True).stdout)
        self.assertEqual(data["actual_paid_starts"], 22)
        self.assertEqual(data["lost_merge_count"], 0)
        bad = subprocess.run(args[:-1] + ["nope"], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(bad.returncode, 0)

    def test_thirty_days_replay_in_under_ten_seconds(self):
        conn = sqlite3.connect(self.db)
        start = datetime(2026, 10, 20, tzinfo=timezone.utc)
        for n in range(1500):  # ~50 starts a day for 30 days, far above the real rate
            started = start + timedelta(minutes=29 * n)
            conn.execute(
                "INSERT INTO experiments (id, goal, status, created_at, ended_at, "
                "implementation_provider, error, implementation_runtime_ms, source_type, source_id) "
                "VALUES (?, 'g', 'failed', ?, ?, ?, ?, 600000, 'github_issue', ?)",
                (f"bulk-{n}", started.isoformat(), (started + timedelta(minutes=10)).isoformat(),
                 ("codex", "claude")[n % 2], "tests failed: x", str(600 + n % 40)),
            )
        conn.commit()
        conn.close()
        began = time.monotonic()
        report, _ = tick_replay_store.report_from_db(self.db, "2026-10-20", None)
        self.assertLess(time.monotonic() - began, 10)
        self.assertGreaterEqual(report.actual_paid_starts, 1500)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.db = _db_with_sequence()
        service = MagicMock()
        service.db_path = self.db
        mailbox = MagicMock()
        mailbox.recover_after_restart.return_value = None
        api.app.dependency_overrides[api.get_experiment_service] = lambda: service
        api.app.dependency_overrides[api.get_codex_service] = lambda: MagicMock()
        api.app.dependency_overrides[api.get_mailbox_task_service] = lambda: mailbox
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)

    def test_401_without_or_with_a_wrong_key(self):
        for headers in ({}, {"x-api-key": "wrong"}):
            self.assertEqual(self.client.get("/tick-replay", headers=headers).status_code, 401)

    def test_returns_the_report_with_the_key(self):
        response = self.client.get(
            "/tick-replay?since=2026-11-18&policies=provider_cooldown,issue_loop_guard",
            headers={"x-api-key": api.API_KEY},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["policies"], ["provider_cooldown", "issue_loop_guard"])
        self.assertEqual(body["actual_paid_starts"], 22)
        self.assertGreaterEqual(body["saved_starts"], 12)
        self.assertLessEqual(len(body["first_skipped"]), 5)

    def test_bad_input_is_a_400(self):
        for query in ("since=yesterday", "policies=nope"):
            response = self.client.get(f"/tick-replay?{query}", headers={"x-api-key": api.API_KEY})
            self.assertEqual(response.status_code, 400, query)

    def test_is_a_read_tool(self):
        self.assertIn("tick_replay_get", READ_ONLY_TOOLS)


if __name__ == "__main__":
    unittest.main()
