"""Issue #690: deterministic local GGUF bake-off and rollback."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
import os
from pathlib import Path

from modules.local_model_bakeoff import choose_winner, list_bakeoffs
from modules.run_store import init_db

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_model_bakeoff_script", ROOT / "scripts/local_model_bakeoff.py")
assert SPEC and SPEC.loader
bakeoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bakeoff)


class BakeoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = str(self.root / "agent.db")
        init_db(self.db).close()
        self.fixture = self.root / "hunks.json"
        self.fixture.write_text(json.dumps([{"experiment_id": f"e{i}"} for i in range(10)]))
        self.calls = []
        self.active = "7b.gguf"

    def command(self, argv):
        self.calls.append(argv[-1] if argv[-2] == "switch" else "list")
        if argv[-1] == "list":
            return subprocess.CompletedProcess(argv, 0, "current configuration:\n  MODEL=7b.gguf\n", "")
        self.active = argv[-1].split("=", 1)[1]
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    @staticmethod
    def bench(*_args, **_kwargs):
        return {"summary": {"all": {"prompt_tokens_per_second": 10.0,
                                      "output_tokens_per_second": 5.0}}}

    @staticmethod
    def replay(experiment_id, **_kwargs):
        return {"units": [{"status": "scored", "applied": True, "compiled": True,
                            "tests_passed": experiment_id != "e9", "seconds": 2.0}]}

    def execute(self, candidates=None, **kwargs):
        bench_fn = kwargs.pop("bench_fn", self.bench)
        replay_fn = kwargs.pop("replay_fn", self.replay)
        return bakeoff.run_bakeoff(
            candidates or ["7b.gguf", "3b.gguf"], db_path=self.db,
            fixture_path=self.fixture, run_command=self.command, bench_fn=bench_fn,
            replay_fn=replay_fn, **kwargs,
        )

    def test_candidates_are_ordered_and_original_restored_and_rows_stored(self):
        report = self.execute()
        self.assertEqual(self.calls, ["list", "MODEL=7b.gguf", "MODEL=3b.gguf", "MODEL=7b.gguf"])
        self.assertEqual(self.active, "7b.gguf")
        rows = list_bakeoffs(10, db_path=self.db)
        self.assertEqual({r["model"] for r in rows}, {"7b.gguf", "3b.gguf"})
        self.assertTrue(all(r["hunks_total"] == 10 for r in rows))
        self.assertEqual(report["winner"], "7b.gguf")  # stable first candidate on a full tie

    def test_original_restored_after_exception_and_keyboard_interrupt(self):
        for error in (RuntimeError("boom"), KeyboardInterrupt()):
            self.calls.clear()
            def broken(*_args, **_kwargs):
                raise error
            with self.assertRaises(type(error)):
                self.execute(candidates=["3b.gguf"], bench_fn=broken)
            self.assertEqual(self.calls[-1], "MODEL=7b.gguf")
            self.assertEqual(self.active, "7b.gguf")
        rows = list_bakeoffs(10, db_path=self.db)
        self.assertEqual([row["status"] for row in rows], ["interrupted", "error"])

    def test_failure_halfway_records_remaining_candidates_as_not_run(self):
        calls = 0
        def bench(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("candidate crashed")
            return self.bench(*args, **kwargs)
        with self.assertRaises(RuntimeError):
            self.execute(candidates=["7b.gguf", "3b.gguf", "1b.gguf"], bench_fn=bench)
        rows = list_bakeoffs(10, db_path=self.db)
        by_model = {row["model"]: row["status"] for row in rows}
        self.assertEqual(by_model, {"7b.gguf": "completed", "3b.gguf": "error",
                                    "1b.gguf": "not_run"})
        self.assertEqual(self.active, "7b.gguf")

    def test_switch_failure_is_recorded_and_skipped(self):
        def command(argv):
            result = self.command(argv)
            if argv[-1] == "MODEL=bad.gguf":
                return subprocess.CompletedProcess(argv, 2, "", "missing")
            return result
        report = bakeoff.run_bakeoff(
            ["bad.gguf", "7b.gguf"], db_path=self.db, fixture_path=self.fixture,
            run_command=command, bench_fn=self.bench, replay_fn=self.replay,
        )
        bad = next(r for r in report["rows"] if r["model"] == "bad.gguf")
        self.assertEqual(bad["status"], "switch_failed")
        self.assertEqual(bad["hunks_total"], 0)

    def test_winner_tiebreak_and_guard(self):
        current = {"model": "7b", "status": "completed", "passing_hunks_per_hour": 10,
                   "passing_hunks": 4, "hunks_total": 10, "local_too_slow": 0}
        fast = {"model": "3b", "status": "completed", "passing_hunks_per_hour": 11,
                "passing_hunks": 5, "hunks_total": 10, "local_too_slow": 2}
        self.assertTrue(choose_winner([current, fast], "7b")["recommend_switch"])
        fast["local_too_slow"] = 3
        self.assertFalse(choose_winner([current, fast], "7b")["recommend_switch"])
        tied = {**fast, "model": "1b", "local_too_slow": 0,
                "passing_hunks_per_hour": 11, "passing_hunks": 6}
        self.assertEqual(choose_winner([current, fast, tied], "7b")["winner"]["model"], "1b")

    def test_candidate_model_rates_seed_and_timeout_reach_every_replay(self):
        seen = []
        class FakeClient:
            model = "wrong-7b"
        fake = FakeClient()
        def replay(experiment_id, **kwargs):
            seen.append((kwargs["client"].model,
                         os.environ.get("NEXUS_SHADOW_REPLAY_UNIT_TIMEOUT_SECONDS"),
                         kwargs["client"].model in json.loads(Path(
                             os.environ["LOCAL_MODEL_RATES_PATH"]).read_text())["models"]))
            return self.replay(experiment_id, **kwargs)
        self.execute(candidates=["3b.gguf"], replay_fn=replay, client=fake,
                     seed=123, timeout=17)
        self.assertEqual(seen, [("3b.gguf", "17", True)] * 10)
        self.assertEqual(list(self.root.glob(".bakeoff-rates-*.json")), [])
        row = list_bakeoffs(1, db_path=self.db)[0]
        self.assertEqual((row["details"]["seed"], row["details"]["timeout_seconds"]), (123, 17))

    def test_lease_busy_is_retried_and_never_counted_as_failed_hunk(self):
        attempts = 0
        clock = [0.0]
        def replay(experiment_id, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return None
            return self.replay(experiment_id, **kwargs)
        def sleep(seconds):
            clock[0] += seconds
        report = self.execute(candidates=["3b.gguf"], replay_fn=replay,
                              lease_wait_seconds=5, sleep_fn=sleep,
                              monotonic_fn=lambda: clock[0])
        self.assertEqual(attempts, 12)
        self.assertEqual(report["rows"][0]["hunks_total"], 10)
        self.assertEqual(report["rows"][0]["passing_hunks"], 9)

    def test_persistent_lease_busy_aborts_candidate_without_failed_hunk(self):
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        report = self.execute(candidates=["3b.gguf"], replay_fn=lambda *_a, **_k: None,
                              lease_wait_seconds=2, sleep_fn=sleep,
                              monotonic_fn=lambda: clock[0])
        row = report["rows"][0]
        self.assertEqual(row["status"], "lease_busy")
        self.assertEqual(row["passing_hunks"], 0)
        self.assertEqual(row["local_too_slow"], 0)
        self.assertEqual(row["details_json"].count("lease_busy"), 1)

    def test_bad_fixture_case_is_recorded_and_remaining_hunks_continue(self):
        def replay(experiment_id, **kwargs):
            if experiment_id == "e3":
                raise ValueError("not merged")
            return self.replay(experiment_id, **kwargs)
        report = self.execute(candidates=["3b.gguf"], replay_fn=replay)
        row = report["rows"][0]
        self.assertEqual(row["hunks_total"], 10)
        self.assertEqual(row["passing_hunks"], 8)
        self.assertIn("fixture_error", row["details_json"])

    def test_repeated_runs_have_distinct_ids_and_rows(self):
        first = self.execute(candidates=["7b.gguf"])
        second = self.execute(candidates=["7b.gguf"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(list_bakeoffs(10, db_path=self.db)), 2)


if __name__ == "__main__":
    unittest.main()
