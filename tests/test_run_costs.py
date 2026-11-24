"""#810: every coding-agent run records its cost per provider.

Covers the built-in price table and its seeding, cost calculation from usage,
subscription marking, the ``runs`` schema migration, the weekly
cost-per-provider query, the 7-day completeness metric, and every worker
audit path (Codex, Claude, Vibe, Mistral API, task service, weekly review).
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import modules.run_store as run_store
from modules.run_store import (
    DEFAULT_MODEL_PRICES,
    calculate_estimated_cost,
    get_effective_price,
    init_db,
    insert_run,
    resolve_run_cost,
    run_cost_completeness,
    weekly_cost_by_provider,
)
from modules.run_store.prices import seed_default_model_prices

NOW = datetime(2026, 11, 29, 12, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago: float, hour: int = 10) -> str:
    return (NOW - timedelta(days=days_ago)).replace(hour=hour).isoformat()


class RunStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "agent.db")
        self.conns: list[sqlite3.Connection] = []

    def tearDown(self) -> None:
        for conn in self.conns:
            conn.close()

    def _init(self) -> sqlite3.Connection:
        conn = init_db(self.db_path)
        self.conns.append(conn)
        return conn

    def _insert(self, conn, **overrides) -> int:
        defaults = dict(
            timestamp=_ts(1),
            provider="codex",
            model="codex",
            input_description="do a thing",
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            runtime_ms=500,
            estimated_cost=None,
            actual_cost=None,
            cost_status="unknown",
            success=True,
            error=None,
        )
        defaults.update(overrides)
        return insert_run(conn, **defaults)


# ---------------------------------------------------------------------------
# Price table seeding
# ---------------------------------------------------------------------------

class PriceSeedTests(RunStoreTestCase):
    def test_init_db_seeds_builtin_prices(self):
        conn = self._init()
        rows = conn.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0]
        self.assertEqual(rows, len(DEFAULT_MODEL_PRICES))
        row = conn.execute(
            "SELECT input_price_per_million_tokens, output_price_per_million_tokens, currency, source "
            "FROM model_prices WHERE provider='azure_openai' AND model='gpt-4o'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual((row[0], row[1], row[2]), (2.50, 10.00, "USD"))
        self.assertIn("builtin", row[3])

    def test_seeding_is_idempotent_and_never_overrides(self):
        conn = self._init()
        # An operator row for the same key must survive a re-seed untouched.
        conn.execute(
            "UPDATE model_prices SET input_price_per_million_tokens=99.0 "
            "WHERE provider='codex' AND model='gpt-5' AND valid_from='2026-01-01T00:00:00Z'"
        )
        conn.commit()
        self.assertEqual(seed_default_model_prices(conn), 0)
        value = conn.execute(
            "SELECT input_price_per_million_tokens FROM model_prices "
            "WHERE provider='codex' AND model='gpt-5' AND valid_from='2026-01-01T00:00:00Z'"
        ).fetchone()[0]
        self.assertEqual(value, 99.0)
        # A second full init_db() changes nothing either.
        conn.close()
        conn2 = self._init()
        self.assertEqual(
            conn2.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0],
            len(DEFAULT_MODEL_PRICES),
        )

    def test_unknown_model_has_no_fallback_price(self):
        conn = self._init()
        exact = get_effective_price(conn, "codex", "gpt-5", _ts(1))
        self.assertEqual(exact["input_price_per_million_tokens"], 1.25)
        wildcard = get_effective_price(conn, "codex", "some-future-model", _ts(1))
        self.assertIsNone(wildcard)
        self.assertIsNone(get_effective_price(conn, "no-such-provider", "x", _ts(1)))


# ---------------------------------------------------------------------------
# Cost resolution
# ---------------------------------------------------------------------------

class ResolveRunCostTests(RunStoreTestCase):
    def test_subscription_takes_precedence_over_cli_cost(self):
        conn = self._init()
        cost = resolve_run_cost(
            conn, provider="claude", model="claude-sonnet-4-5", timestamp=_ts(1),
            usage={"input_tokens": 1000, "output_tokens": 100},
            actual_cost=0.0123, subscription=True,
        )
        self.assertEqual(cost["cost_status"], "subscription")
        self.assertIsNone(cost["actual_cost"])
        self.assertIsNone(cost["estimated_cost"])
        self.assertIsNone(cost["cost_currency"])

    def test_subscription_run_is_marked_not_priced(self):
        conn = self._init()
        cost = resolve_run_cost(
            conn, provider="vibe", model="vibe", timestamp=_ts(1),
            usage={"input_tokens": 1000, "output_tokens": 100}, subscription=True,
        )
        self.assertEqual(cost["cost_status"], "subscription")
        self.assertIsNone(cost["actual_cost"])
        self.assertIsNone(cost["estimated_cost"])

    def test_estimated_from_usage_and_price_table_with_cache_subtracted(self):
        conn = self._init()
        cost = resolve_run_cost(
            conn, provider="codex", model="gpt-5", timestamp=_ts(1),
            usage={"input_tokens": 1_000_000, "output_tokens": 100_000, "cached_input_tokens": 400_000},
        )
        self.assertEqual(cost["cost_status"], "estimated")
        # 600k billable input at 1.25/1M + 100k output at 10/1M.
        self.assertAlmostEqual(cost["estimated_cost"], 600_000 / 1e6 * 1.25 + 400_000 / 1e6 * 0.125 + 100_000 / 1e6 * 10.0)
        self.assertEqual(cost["cost_currency"], "USD")

    def test_claude_cache_reads_are_separate_from_input(self):
        conn = self._init()
        cost = resolve_run_cost(
            conn, provider="claude", model="claude-sonnet-4-5", timestamp=_ts(1),
            usage={"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 250_000},
        )
        self.assertAlmostEqual(cost["estimated_cost"], 3.0 + 250_000 / 1e6 * 0.30)

    def test_claude_cache_creation_uses_both_rates(self):
        conn = self._init()
        cost = resolve_run_cost(
            conn, provider="claude", model="claude-sonnet-4-5", timestamp=_ts(1),
            usage={"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1000,
                   "cache_creation_1h_input_tokens": 400},
        )
        self.assertAlmostEqual(cost["estimated_cost"], (600 * 3.75 + 400 * 6) / 1e6)

    def test_usage_without_price_is_unknown_not_zero(self):
        conn = self._init()
        cost = resolve_run_cost(
            conn, provider="no-such-provider", model="x", timestamp=_ts(1),
            usage={"input_tokens": 10, "output_tokens": 5},
        )
        self.assertEqual(cost["cost_status"], "unknown")
        self.assertIsNone(cost["estimated_cost"])

    def test_no_usage_is_unknown(self):
        conn = self._init()
        cost = resolve_run_cost(conn, provider="codex", model="codex", timestamp=_ts(1), usage=None)
        self.assertEqual(cost["cost_status"], "unknown")

    def test_calculate_estimated_cost_formula(self):
        self.assertAlmostEqual(
            calculate_estimated_cost(1_000_000, 500_000, 2.5, 10.0), 2.5 + 5.0
        )


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

class RunsMigrationTests(RunStoreTestCase):
    def test_legacy_runs_table_gains_cost_columns_and_keeps_rows(self):
        legacy = sqlite3.connect(self.db_path)
        legacy.execute(
            """CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                input_description TEXT NOT NULL, input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                runtime_ms INTEGER NOT NULL, estimated_cost REAL NULL, actual_cost REAL NULL,
                cost_status TEXT NOT NULL,
                success INTEGER NOT NULL CHECK (success IN (0, 1)), error TEXT NULL
            )"""
        )
        legacy.execute(
            "INSERT INTO runs (timestamp, provider, model, input_description, input_tokens,"
            " output_tokens, total_tokens, runtime_ms, estimated_cost, actual_cost, cost_status,"
            " success, error) VALUES ('2026-11-20T10:00:00+00:00','codex','codex','old',1,1,2,3,"
            " NULL,NULL,'unknown',1,NULL)"
        )
        legacy.execute(
            "INSERT INTO runs (timestamp, provider, model, input_description, input_tokens,"
            " output_tokens, total_tokens, runtime_ms, estimated_cost, actual_cost, cost_status,"
            " success, error) VALUES ('2026-11-20T11:00:00+00:00','codex','codex','no usage',0,0,0,3,"
            " NULL,NULL,'unknown',0,'binary not found')"
        )
        legacy.commit()
        legacy.close()

        conn = self._init()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        self.assertIn("cost_currency", columns)
        self.assertIn("run_kind", columns)
        rows = conn.execute(
            "SELECT provider, cost_status, cost_currency, run_kind, estimated_cost FROM runs ORDER BY id"
        ).fetchall()
        # Legacy counts alone cannot establish the model or cache rates.
        self.assertEqual(rows[0][0], "codex")
        self.assertEqual(rows[0][1], "unknown")
        self.assertIsNone(rows[0][2])
        self.assertIsNone(rows[0][4])
        self.assertEqual(rows[1][1], "unknown")
        self.assertIsNone(rows[1][2])
        self.assertIsNone(rows[1][4])
        # run_kind is NULL for pre-#810 rows.
        self.assertIsNone(rows[0][3])
        self.assertIsNone(rows[1][3])

    def test_insert_run_persists_cost_currency_and_run_kind(self):
        conn = self._init()
        run_id = self._insert(
            conn, estimated_cost=0.5, cost_status="estimated", cost_currency="USD",
            run_kind="implementation",
        )
        row = conn.execute(
            "SELECT estimated_cost, cost_status, cost_currency, run_kind FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        self.assertEqual(row, (0.5, "estimated", "USD", "implementation"))


# ---------------------------------------------------------------------------
# Weekly query and completeness metric
# ---------------------------------------------------------------------------

class WeeklyCostQueryTests(RunStoreTestCase):
    def _seed_week(self, conn):
        # Week A (8-14 days ago): codex estimated, vibe subscription, claude actual.
        self._insert(conn, timestamp=_ts(9), provider="codex", model="codex",
                     estimated_cost=0.10, cost_status="estimated", cost_currency="USD",
                     run_kind="implementation")
        self._insert(conn, timestamp=_ts(10), provider="vibe", model="vibe",
                     cost_status="subscription", run_kind="weekly_review")
        self._insert(conn, timestamp=_ts(11), provider="claude", model="claude-sonnet-4-5",
                     actual_cost=1.25, cost_status="actual", cost_currency="USD",
                     run_kind="review")
        # Week B (this week): codex estimated, codex unknown (no usage), vibe subscription.
        self._insert(conn, timestamp=_ts(1), provider="codex", model="codex",
                     estimated_cost=0.20, cost_status="estimated", cost_currency="USD")
        self._insert(conn, timestamp=_ts(2), provider="codex", model="codex",
                     cost_status="unknown", success=False, error="boom")
        self._insert(conn, timestamp=_ts(3), provider="vibe", model="vibe",
                     cost_status="subscription")

    def test_one_query_gives_cost_per_provider_per_week(self):
        conn = self._init()
        self._seed_week(conn)
        since = _ts(14)
        rows = weekly_cost_by_provider(conn, since=since)
        by_key = {(row["week"], row["provider"], row["currency"]): row for row in rows}
        # Week A: codex + vibe + claude; week B: codex + vibe.
        self.assertEqual(len(rows), 6)
        codex_weeks = sorted(row["week"] for (week, provider, currency), row in by_key.items()
                             if provider == "codex" and currency == "USD")
        self.assertEqual(len(codex_weeks), 2)
        this_week_codex = next(
            row for (week, provider, currency), row in by_key.items()
            if provider == "codex" and currency == "USD" and row["total_cost"] == 0.20
        )
        self.assertEqual(this_week_codex["runs"], 1)
        self.assertEqual(this_week_codex["runs_with_cost"], 1)  # the unknown one has none
        self.assertAlmostEqual(this_week_codex["total_cost"], 0.20)
        self.assertEqual(this_week_codex["currency"], "USD")
        vibe_rows = [row for (_w, provider, _currency), row in by_key.items() if provider == "vibe"]
        self.assertTrue(all(row["subscription_runs"] == row["runs"] for row in vibe_rows))
        self.assertTrue(all(row["total_cost"] is None for row in vibe_rows))
        claude = next(row for (_w, provider, _currency), row in by_key.items() if provider == "claude")
        self.assertAlmostEqual(claude["total_cost"], 1.25)
        self.assertEqual(claude["runs_with_cost"], 1)

    def test_raw_sql_constant_is_the_same_query(self):
        conn = self._init()
        self._seed_week(conn)
        rows = conn.execute(run_store.WEEKLY_COST_BY_PROVIDER_SQL, (_ts(14),)).fetchall()
        self.assertEqual(len(rows), 6)

    def test_completeness_share_over_last_7_days(self):
        conn = self._init()
        self._insert(conn, timestamp=_ts(0), estimated_cost=0.0, cost_status="estimated")
        self._insert(conn, timestamp=_ts(0, hour=22), cost_status="subscription")  # future
        self._insert(conn, timestamp=_ts(1), cost_status="unknown", success=False, error="x")
        self._insert(conn, timestamp=_ts(30), cost_status="unknown")  # outside the window
        metric = run_cost_completeness(conn, days=7, now=NOW)
        self.assertEqual(metric["runs"], 2)
        self.assertEqual(metric["runs_with_cost_or_subscription"], 1)
        self.assertAlmostEqual(metric["share"], 0.5)

    def test_completeness_counts_subscription_and_can_be_scoped_to_providers(self):
        conn = self._init()
        self._insert(conn, timestamp=_ts(1), provider="vibe", cost_status="subscription")
        self._insert(conn, timestamp=_ts(1), provider="codex", cost_status="unknown")
        metric = run_cost_completeness(conn, days=7, now=NOW)
        self.assertEqual(metric["runs"], 2)
        self.assertAlmostEqual(metric["share"], 0.5)
        scoped = run_cost_completeness(conn, days=7, now=NOW, providers=("vibe",))
        self.assertEqual(scoped["runs"], 1)
        self.assertAlmostEqual(scoped["share"], 1.0)

    def test_completeness_with_no_runs_has_no_share(self):
        conn = self._init()
        metric = run_cost_completeness(conn, days=7, now=NOW)
        self.assertEqual(metric["runs"], 0)
        self.assertIsNone(metric["share"])


# ---------------------------------------------------------------------------
# Historical backfill (#810): pre-price-table runs with tokens get estimated
# ---------------------------------------------------------------------------

class BackfillTests(RunStoreTestCase):
    def test_unknown_runs_with_tokens_are_backfilled_from_the_price_table(self):
        conn = self._init()
        legacy_id = self._insert(conn, timestamp=_ts(5), provider="azure_openai", model="gpt-4o",
                                 input_tokens=2_000_000, output_tokens=100_000,
                                 cost_status="unknown")
        no_tokens = self._insert(conn, timestamp=_ts(5), provider="azure_openai", model="gpt-4o",
                                 input_tokens=0, output_tokens=0, cost_status="unknown")
        already = self._insert(conn, timestamp=_ts(5), provider="claude", model="claude-sonnet-4-5",
                               actual_cost=0.5, cost_status="actual")
        conn.close()

        conn = self._init()  # second init runs the one-time backfill
        rows = {row[0]: row for row in conn.execute(
            "SELECT id, estimated_cost, cost_status, cost_currency FROM runs"
        ).fetchall()}
        self.assertAlmostEqual(rows[legacy_id][1], 2_000_000 / 1e6 * 2.50 + 100_000 / 1e6 * 10.0)
        self.assertEqual(rows[legacy_id][2], "estimated")
        self.assertEqual(rows[legacy_id][3], "USD")
        # No usage -> stays unknown, no invented cost.
        self.assertEqual(rows[no_tokens][2], "unknown")
        self.assertIsNone(rows[no_tokens][1])
        # Already-costed rows are untouched.
        self.assertEqual(rows[already][2], "actual")

    def test_backfill_is_idempotent_and_runs_once_per_database(self):
        conn = self._init()
        run_id = self._insert(conn, timestamp=_ts(5), provider="azure_openai", model="gpt-4o",
                              input_tokens=1_000_000, output_tokens=0, cost_status="unknown")
        conn.close()
        conn = self._init()
        first = conn.execute("SELECT estimated_cost, cost_status FROM runs WHERE id=?",
                             (run_id,)).fetchone()
        self.assertEqual(first[1], "estimated")
        # Manually resetting a row to unknown must not be re-backfilled after
        # the marker is set (one pass per database).
        conn.execute("UPDATE runs SET cost_status='unknown', estimated_cost=NULL WHERE id=?",
                     (run_id,))
        conn.commit()
        conn.close()
        conn = self._init()
        again = conn.execute("SELECT estimated_cost, cost_status FROM runs WHERE id=?",
                             (run_id,)).fetchone()
        self.assertEqual(again[1], "unknown")
        self.assertIsNone(again[0])

    def test_backfill_uses_the_price_valid_at_run_time(self):
        conn = self._init()
        conn.execute(
            "INSERT INTO model_prices (provider, model, input_price_per_million_tokens,"
            " output_price_per_million_tokens, currency, valid_from, source)"
            " VALUES ('azure_openai', 'gpt-4o', 5.0, 20.0, 'USD', '2026-11-25T00:00:00Z', 'test')"
        )
        conn.commit()
        old = self._insert(conn, timestamp="2026-11-20T10:00:00+00:00", provider="azure_openai",
                           model="gpt-4o", input_tokens=1_000_000, output_tokens=0,
                           cost_status="unknown")
        new = self._insert(conn, timestamp="2026-11-28T10:00:00+00:00", provider="azure_openai",
                           model="gpt-4o", input_tokens=1_000_000, output_tokens=0,
                           cost_status="unknown")
        conn.close()
        conn = self._init()
        old_cost = conn.execute("SELECT estimated_cost FROM runs WHERE id=?", (old,)).fetchone()[0]
        new_cost = conn.execute("SELECT estimated_cost FROM runs WHERE id=?", (new,)).fetchone()[0]
        self.assertAlmostEqual(old_cost, 2.50)   # older valid_from row
        self.assertAlmostEqual(new_cost, 5.0)    # newer row wins for the newer run


# ---------------------------------------------------------------------------
# Worker audit paths
# ---------------------------------------------------------------------------

def _completed_proc(stdout="", stderr="", returncode=0):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


CODEX_JSONL = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "tid-1"}),
    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
    json.dumps({"type": "turn.completed",
                "usage": {"input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": 400}}),
])


class CodexAuditTests(RunStoreTestCase):
    def _worker(self, **kwargs):
        from modules.codex_worker import CodexWorker
        defaults = dict(codex_bin="/fake/codex", db_path=self.db_path)
        defaults.update(kwargs)
        return CodexWorker(**defaults)

    def _latest_run(self):
        conn = init_db(self.db_path)
        self.conns.append(conn)
        return conn.execute(
            "SELECT provider, model, input_tokens, output_tokens, estimated_cost, actual_cost,"
            " cost_status, cost_currency, run_kind, success, error FROM runs"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()

    @patch("subprocess.run")
    def test_run_cost_is_estimated_from_usage_and_price_table(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=CODEX_JSONL, stderr="model: gpt-5\n")
        result = self._worker().run("implement x", self.tmp.name, phase="implementation")
        self.assertTrue(result.success)
        row = self._latest_run()
        self.assertEqual(row[0], "codex")
        self.assertEqual(row[2], 1000)
        self.assertEqual(row[3], 100)
        # billable input 600 at 1.25/1M + output 100 at 10/1M
        self.assertAlmostEqual(row[4], 600 / 1e6 * 1.25 + 400 / 1e6 * 0.125 + 100 / 1e6 * 10.0)
        self.assertIsNone(row[5])
        self.assertEqual(row[6], "estimated")
        self.assertEqual(row[7], "USD")
        self.assertEqual(row[8], "implementation")
        self.assertEqual(row[9], 1)

    def test_failed_run_is_still_recorded(self):
        # Binary missing -> worker-level failure; the run must still be audited.
        worker = self._worker(codex_bin="/nonexistent/codex")
        result = worker.run("implement x", self.tmp.name, phase="implementation_repair")
        self.assertFalse(result.success)
        row = self._latest_run()
        self.assertEqual(row[9], 0)
        self.assertIsNotNone(row[10])
        self.assertEqual(row[8], "implementation_repair")
        # No usage was produced, so no cost is invented.
        self.assertEqual(row[6], "unknown")
        self.assertIsNone(row[4])

    def test_invalid_config_is_recorded_as_a_failed_run(self):
        result = self._worker().run("x", "relative/path", phase="review")
        self.assertFalse(result.success)
        row = self._latest_run()
        self.assertEqual(row[9], 0)
        self.assertEqual(row[8], "review")

    @patch("subprocess.run")
    def test_codex_timeout_recovers_model_and_usage_from_linked_rollout(self, mock_run):
        from datetime import datetime

        session = "c14948c9-e21a-4b4c-ba65-18468adb97f0"
        codex_home = Path(self.tmp.name) / "codex-home"
        day = codex_home / "sessions" / datetime.now().strftime("%Y/%m/%d")
        day.mkdir(parents=True)
        (day / f"rollout-test-{session}.jsonl").write_text("\n".join(map(json.dumps, [
            {"type": "turn_context", "payload": {"model": "gpt-5"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": 400},
            }}},
        ])))
        event = json.dumps({"type": "thread.started", "thread_id": session})
        mock_run.side_effect = subprocess.TimeoutExpired("codex", 1, output=event.encode())
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            result = self._worker().run("task", self.tmp.name)
        self.assertTrue(result.timed_out)
        row = self._latest_run()
        self.assertEqual(row[1], "gpt-5")
        self.assertAlmostEqual(row[4], (600 * 1.25 + 400 * .125 + 100 * 10) / 1e6)

    def test_codex_watchdog_stop_keeps_usage(self):
        from modules.coding_worker import STOP_STALLED, WatchedRun

        with patch("modules.codex_worker.run_watched", return_value=WatchedRun(
            None, CODEX_JSONL, "model: gpt-5\n", STOP_STALLED,
        )):
            result = self._worker().run("task", self.tmp.name, stall_seconds=1)
        self.assertTrue(result.timed_out)
        self.assertEqual(self._latest_run()[6], "estimated")

    @patch("subprocess.run")
    def test_result_logging_failure_keeps_the_codex_audit(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=CODEX_JSONL, stderr="model: gpt-5\n")
        logger = MagicMock()
        logger.log_process.side_effect = RuntimeError("logger failed")
        with self.assertRaisesRegex(RuntimeError, "logger failed"):
            self._worker().run("task", self.tmp.name, experiment_logger=logger)
        rows = self._init().execute("SELECT cost_status FROM runs").fetchall()
        self.assertEqual([row[0] for row in rows], ["estimated"])


class ClaudeAuditTests(RunStoreTestCase):
    def _worker(self, **kwargs):
        from modules.claude_worker import ClaudeWorker
        defaults = dict(claude_bin="/fake/claude", db_path=self.db_path)
        defaults.update(kwargs)
        return ClaudeWorker(**defaults)

    def _latest_run(self):
        conn = init_db(self.db_path)
        self.conns.append(conn)
        return conn.execute(
            "SELECT provider, model, estimated_cost, actual_cost, cost_status, cost_currency,"
            " run_kind, success FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    @staticmethod
    def _result_json(**overrides):
        payload = {
            "type": "result", "subtype": "success", "is_error": False, "session_id": "sid-1",
            "result": "ok", "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 200},
            "modelUsage": {"claude-sonnet-4-5": {"inputTokens": 1000, "outputTokens": 100, "cacheReadInputTokens": 200}},
        }
        payload.update(overrides)
        return json.dumps(payload)

    def _clean_claude_env(self, **overrides):
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENT_CLAUDE_SUBSCRIPTION", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")}
        env.update(overrides)
        return patch.dict(os.environ, env, clear=True)

    @patch("subprocess.run")
    def test_api_billed_run_records_usage_based_estimate(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=self._result_json())
        with self._clean_claude_env(AGENT_CLAUDE_SUBSCRIPTION="0"):
            self._worker().run("task", self.tmp.name, phase="implementation")
        row = self._latest_run()
        self.assertEqual(row[0], "claude")
        self.assertEqual(row[1], "claude-sonnet-4-5")
        self.assertIsNone(row[3])
        self.assertAlmostEqual(row[2], (1000 * 3 + 200 * 0.30 + 100 * 15) / 1e6)
        self.assertEqual(row[4], "estimated")
        self.assertEqual(row[5], "USD")
        self.assertEqual(row[6], "implementation")

    @patch("subprocess.run")
    def test_subscription_run_is_marked_not_given_a_fake_price(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=self._result_json())
        with self._clean_claude_env(AGENT_CLAUDE_SUBSCRIPTION="1"):
            self._worker().run("task", self.tmp.name, phase="review")
        row = self._latest_run()
        self.assertEqual(row[4], "subscription")
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])
        self.assertIsNone(row[5])

    @patch("subprocess.run")
    def test_zero_reported_cost_on_subscription_is_not_a_fake_price(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=self._result_json(total_cost_usd=0.0))
        with self._clean_claude_env(AGENT_CLAUDE_SUBSCRIPTION="true"):
            self._worker().run("task", self.tmp.name)
        row = self._latest_run()
        self.assertEqual(row[4], "subscription")
        self.assertIsNone(row[3])

    @patch("subprocess.run")
    def test_api_run_without_reported_cost_is_estimated_from_usage(self, mock_run):
        parsed = json.loads(self._result_json())
        parsed.pop("total_cost_usd")
        mock_run.return_value = _completed_proc(stdout=json.dumps(parsed))
        with self._clean_claude_env(AGENT_CLAUDE_SUBSCRIPTION="0"):
            self._worker().run("task", self.tmp.name, phase="weekly_review")
        row = self._latest_run()
        # billable input 800 at 3/1M + output 100 at 15/1M
        self.assertAlmostEqual(row[2], (1000 * 3 + 200 * 0.30 + 100 * 15) / 1e6)
        self.assertEqual(row[4], "estimated")
        self.assertEqual(row[5], "USD")
        self.assertEqual(row[6], "weekly_review")

    @patch("subprocess.run")
    def test_oauth_login_without_api_key_counts_as_subscription(self, mock_run):
        parsed = json.loads(self._result_json())
        parsed.pop("total_cost_usd")
        mock_run.return_value = _completed_proc(stdout=json.dumps(parsed))
        with self._clean_claude_env(CLAUDE_CODE_OAUTH_TOKEN="oauth-123"):
            self._worker().run("task", self.tmp.name)
        self.assertEqual(self._latest_run()[4], "subscription")
        with self._clean_claude_env(CLAUDE_CODE_OAUTH_TOKEN="oauth-123",
                                     ANTHROPIC_API_KEY="sk-ant-api"):
            self._worker().run("task", self.tmp.name)
        self.assertEqual(self._latest_run()[4], "estimated")

    @patch("subprocess.run")
    def test_failed_run_keeps_its_usage_based_cost(self, mock_run):
        parsed = json.loads(self._result_json())
        parsed.pop("total_cost_usd")
        parsed["is_error"] = True
        parsed["result"] = "API Error: 529 Overloaded"
        mock_run.return_value = _completed_proc(stdout=json.dumps(parsed), returncode=1)
        with self._clean_claude_env(AGENT_CLAUDE_SUBSCRIPTION="0"):
            result = self._worker().run("task", self.tmp.name)
        self.assertFalse(result.success)
        row = self._latest_run()
        self.assertEqual(row[7], 0)
        self.assertEqual(row[4], "estimated")  # usage was recorded, so cost is computed
        self.assertIsNotNone(row[2])

    @patch("subprocess.run")
    def test_stored_claude_login_wins_over_positive_reported_cost(self, mock_run):
        mock_run.side_effect = [
            _completed_proc(stdout=json.dumps({
                "loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max",
            })),
            _completed_proc(stdout=self._result_json()),
        ]
        with self._clean_claude_env(ANTHROPIC_AUTH_TOKEN=""):
            self._worker().run("task", self.tmp.name)
        self.assertEqual(mock_run.call_args_list[0].args[0][1:], ["auth", "status", "--json"])
        self.assertEqual(self._latest_run()[4], "subscription")

    @patch("subprocess.run")
    def test_claude_prices_each_model_and_cache_counter_even_when_output_is_capped(self, mock_run):
        mock_run.return_value = _completed_proc(stdout=self._result_json(
            result="long " * 100,
            modelUsage={
                "claude-sonnet-4-5-20250929": {
                    "inputTokens": 1000, "outputTokens": 100,
                    "cacheReadInputTokens": 200, "cacheCreationInputTokens": 300,
                },
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 500, "outputTokens": 50, "cacheReadInputTokens": 800,
                },
            },
        ))
        worker = self._worker()
        worker.max_output_bytes = 20
        with self._clean_claude_env(AGENT_CLAUDE_SUBSCRIPTION="0"):
            result = worker.run("task", self.tmp.name)
        self.assertTrue(result.stdout_truncated)
        expected = (1000 * 3 + 100 * 15 + 200 * .3 + 300 * 3.75 + 500 + 50 * 5 + 800 * .1) / 1e6
        self.assertAlmostEqual(self._latest_run()[2], expected)

    @patch("subprocess.run")
    def test_claude_timeout_retains_streamed_usage(self, mock_run):
        event = json.dumps({"type": "assistant", "message": {
            "id": "m1", "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 200, "output_tokens": 10, "cache_read_input_tokens": 400},
        }})
        mock_run.side_effect = subprocess.TimeoutExpired("claude", 1, output=(event + "\n" + event).encode())
        with self._clean_claude_env(AGENT_CLAUDE_SUBSCRIPTION="0"):
            result = self._worker().run("task", self.tmp.name)
        self.assertFalse(result.success)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.usage["input_tokens"], 200)
        self.assertAlmostEqual(self._latest_run()[2], (200 * 3 + 10 * 15 + 400 * .3) / 1e6)


FAKE_VIBE = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    args = sys.argv[1:]
    def opt(name):
        return args[args.index(name) + 1] if name in args else None
    out = opt("--output-dir"); os.makedirs(out, exist_ok=True)
    export = {{"outcome": "finished", "error": None, "session_id": "s-1", "steps": 1,
               "usage": {{"input_tokens": 500, "output_tokens": 50, "cached_input_tokens": 0}}}}
    cost = os.environ.get("FAKE_VIBE_COST")
    if cost:
        export["cost_usd"] = float(cost)
    if os.environ.get("FAKE_VIBE_MODE") == "402":
        export.update(outcome="infrastructure_failure", steps=0,
                      error={{"message": "402 Payment Required", "code": "backend_error"}})
        open(os.path.join(out, "export.json"), "w").write(json.dumps(export)); sys.exit(2)
    open(os.path.join(out, "export.json"), "w").write(json.dumps(export))
    print(json.dumps({{"type": "message", "role": "assistant",
                       "content": [{{"type": "text", "text": "done"}}]}}), flush=True)
''')


class VibeAuditTests(RunStoreTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name)
        self.workdir = self.root / "wt"
        self.workdir.mkdir()
        self.vibe = self.root / "vibe"
        self.vibe.write_text(FAKE_VIBE.format(python=sys.executable), encoding="utf-8")
        self.vibe.chmod(self.vibe.stat().st_mode | stat.S_IEXEC)
        from modules.vibe_worker import VibeWorker
        keys = patch.object(VibeWorker, "_EXTRA_ENV_KEYS",
                            VibeWorker._EXTRA_ENV_KEYS | {"FAKE_VIBE_COST", "FAKE_VIBE_MODE"})
        keys.start()
        self.addCleanup(keys.stop)

    def _worker(self):
        from modules.vibe_worker import VibeWorker
        return VibeWorker(vibe_bin=str(self.vibe), db_path=self.db_path)

    def _latest_run(self):
        conn = init_db(self.db_path)
        self.conns.append(conn)
        return conn.execute(
            "SELECT provider, model, input_tokens, estimated_cost, actual_cost, cost_status,"
            " cost_currency, run_kind, success, error FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def test_export_cost_usd_is_recorded_as_actual(self):
        with patch.dict(os.environ, {"FAKE_VIBE_COST": "0.42"}):
            result = self._worker().run("task", str(self.workdir), phase="implementation")
        self.assertTrue(result.success, result.error)
        self.assertAlmostEqual(result.cost_usd, 0.42)
        row = self._latest_run()
        self.assertEqual(row[0], "vibe")
        self.assertEqual(row[2], 500)
        self.assertAlmostEqual(row[4], 0.42)
        self.assertEqual(row[5], "actual")
        self.assertEqual(row[6], "USD")
        self.assertEqual(row[7], "implementation")

    def test_subscription_run_is_marked_not_priced(self):
        with patch.dict(os.environ, {"AGENT_VIBE_SUBSCRIPTION": "1"}):
            result = self._worker().run("task", str(self.workdir), phase="weekly_review")
        self.assertTrue(result.success, result.error)
        self.assertIsNone(result.cost_usd)
        row = self._latest_run()
        self.assertEqual(row[0], "vibe")
        self.assertEqual(row[5], "subscription")
        self.assertIsNone(row[3])
        self.assertIsNone(row[4])
        self.assertEqual(row[7], "weekly_review")

    def test_failed_run_without_billing_evidence_stays_unknown(self):
        with patch.dict(os.environ, {"FAKE_VIBE_MODE": "402", "FAKE_VIBE_COST": "0",
                                     "AGENT_VIBE_SUBSCRIPTION": "1"}):
            result = self._worker().run("task", str(self.workdir))
        self.assertFalse(result.success)
        row = self._latest_run()
        self.assertEqual(row[8], 0)
        self.assertEqual(row[5], "unknown")
        self.assertIsNone(row[4])

    def test_vibe_accepts_zero_and_rejects_malformed_export(self):
        from modules.vibe_worker import _cost_usd, _read_export

        with patch.dict(os.environ, {"FAKE_VIBE_COST": "0", "AGENT_VIBE_SUBSCRIPTION": "0"}):
            self._worker().run("task", str(self.workdir))
        self.assertEqual(tuple(self._latest_run()[4:6]), (0.0, "actual"))
        for payload in ("null", "[]", "{broken", '{"cost_usd": NaN}', '{"cost_usd": true}'):
            (self.root / "export.json").write_text(payload)
            self.assertIsNone(_cost_usd(_read_export(str(self.root))))


class MistralApiAuditTests(RunStoreTestCase):
    def test_api_run_is_priced_from_the_price_table(self):
        import httpx
        from modules.mistral_worker import MistralWorker

        def transport(url, headers, body):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
            })

        env = {k: v for k, v in os.environ.items() if k != "MISTRAL_API_KEY"}
        with patch.dict(os.environ, {**env, "MISTRAL_API_KEY": "test-key"}, clear=True):
            worker = MistralWorker(transport=transport, db_path=self.db_path)
            result = worker.run("say hi", self.tmp.name, sandbox="read-only", phase="implementation")
        self.assertTrue(result.success, result.error)
        conn = init_db(self.db_path)
        self.conns.append(conn)
        row = conn.execute(
            "SELECT provider, model, estimated_cost, cost_status, run_kind, success FROM runs"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], "mistral")
        # Free-tier price row: a real 0, recorded as estimated, not NULL/unknown.
        self.assertEqual(row[2], 0.0)
        self.assertEqual(row[3], "estimated")
        self.assertEqual(row[4], "implementation")
        self.assertEqual(row[5], 1)


class CodexTaskServiceAuditTests(RunStoreTestCase):
    @patch("subprocess.run")
    def test_task_run_lands_in_runs_with_cost(self, mock_run):
        from modules.codex_task_service import CodexTaskService

        mock_run.return_value = _completed_proc(stdout=CODEX_JSONL, stderr="model: gpt-5\n")
        service = CodexTaskService(db_path=self.db_path)
        self.addCleanup(service.close)
        task = service.create_task(prompt="do it", working_dir=self.tmp.name, provider="codex")
        finished = service.run_task(task.id)
        self.assertEqual(finished.status, "succeeded")
        conn = init_db(self.db_path)
        self.conns.append(conn)
        row = conn.execute(
            "SELECT provider, estimated_cost, cost_status, run_kind FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], "codex")
        self.assertAlmostEqual(row[1], 600 / 1e6 * 1.25 + 400 / 1e6 * 0.125 + 100 / 1e6 * 10.0)
        self.assertEqual(row[2], "estimated")
        self.assertEqual(row[3], "task")


class WeeklyReviewWiringTests(RunStoreTestCase):
    def test_default_runners_pass_db_path_so_the_review_is_audited(self):
        from modules import weekly_review

        with patch("modules.codex_worker.CodexWorker") as cls:
            cls.return_value.run.return_value = MagicMock(success=True, final_message="bericht")
            runners = weekly_review.default_runners(self.db_path)
            self.assertEqual(set(runners), {"codex", "claude", "azure"})
            self.assertEqual(runners["codex"]("prompt", self.tmp.name), "bericht")
            cls.assert_called_once_with(db_path=self.db_path)
            kwargs = cls.return_value.run.call_args.kwargs
            self.assertEqual(kwargs["phase"], "weekly_review")


class NexusReviewerWiringTests(RunStoreTestCase):
    def test_review_runners_pass_db_path(self):
        from modules import nexus_reviewer

        with patch.object(nexus_reviewer, "_run_codex") as run_codex:
            run_codex.return_value = "verdict: approve"
            reviewer = nexus_reviewer.NexusReviewer(db_path=self.db_path)
            reviewer._runners["codex"]("prompt", self.tmp.name)
            run_codex.assert_called_once_with("prompt", self.tmp.name, self.db_path)


class CostRegressionTests(RunStoreTestCase):
    def test_same_provider_week_preserves_currencies(self):
        conn = self._init()
        for currency, price in (("EUR", 3), ("USD", 5), (None, 7)):
            self._insert(conn, actual_cost=price, cost_currency=currency, cost_status="actual")
        totals = {row["currency"]: row["total_cost"]
                  for row in weekly_cost_by_provider(conn, now=NOW)}
        self.assertEqual(totals, {"EUR": 3, "USD": 5, None: 7})

    def test_weekly_review_shows_only_coding_costs_and_their_currencies(self):
        from modules.weekly_review import starter_pack

        conn = self._init()
        for provider, kind, currency, actual in (
            ("azure_openai", "agent", "USD", 999),
            ("claude", "planning", "USD", 888),
            ("codex", "implementation", "USD", 5),
            ("codex", "implementation_repair", "EUR", 3),
            ("azure_openai", "weekly_review", "USD", 2),
        ):
            self._insert(conn, provider=provider, run_kind=kind, cost_currency=currency,
                         actual_cost=actual, cost_status="actual")
        pack = starter_pack(self.db_path, self.tmp.name, NOW)
        self.assertIn("codex | USD | 1 | 1 | 0 | 5.0", pack)
        self.assertIn("codex | EUR | 1 | 1 | 0 | 3.0", pack)
        self.assertIn("azure_openai | USD | 1 | 1 | 0 | 2.0", pack)
        self.assertNotIn("999.0", pack)
        self.assertNotIn("888.0", pack)

    def test_interrupted_attempt_and_later_retry_have_one_row_each(self):
        from modules.worker_audit import audit_run

        with self.assertRaises(KeyboardInterrupt):
            with audit_run(self.db_path, "codex", "implementation", "first"):
                raise KeyboardInterrupt
        with audit_run(self.db_path, "codex", "implementation", "retry") as audit:
            fields = dict(model="gpt-5", success=True, error=None, runtime_ms=1,
                          usage={"input_tokens": 100, "output_tokens": 10})
            audit.finish(**fields)
            audit.finish(**fields)
        rows = self._init().execute("SELECT cost_status, error FROM runs ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("unknown", "KeyboardInterrupt"), ("estimated", None)])

    def test_process_hard_exit_leaves_visible_unknown_attempt(self):
        code = (
            "import os, sys\n"
            "from modules.worker_audit import audit_run\n"
            "with audit_run(sys.argv[1], 'codex', 'implementation', 'crash'):\n"
            "    os._exit(27)\n"
        )
        result = subprocess.run([sys.executable, "-c", code, self.db_path], check=False)
        self.assertEqual(result.returncode, 27)
        row = self._init().execute("SELECT cost_status, error FROM runs").fetchone()
        self.assertEqual(tuple(row), ("unknown", "Provider attempt did not finalize"))

    def test_finalize_retries_same_row_after_transient_database_failure(self):
        from modules.worker_audit import audit_run
        from modules.run_store import _connect

        with audit_run(self.db_path, "vibe", "task", "task") as audit:
            with patch("modules.worker_audit._connect",
                       side_effect=[sqlite3.OperationalError("locked"), _connect(self.db_path)]):
                audit.finish(model="vibe", success=True, error=None, runtime_ms=1, actual_cost=0)
        rows = self._init().execute("SELECT cost_status, actual_cost FROM runs").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("actual", 0.0)])

    def test_backfill_rollback_and_retry(self):
        conn = self._init()
        row_id = self._insert(conn, provider="azure_openai", model="gpt-4o",
                              input_tokens=1000, output_tokens=100)
        from modules.run_store.records import backfill_run_costs

        def interrupt(conn, *, after_id):
            backfill_run_costs(conn, after_id=after_id)
            raise RuntimeError("interrupted before checkpoint")

        with patch("modules.run_store.records.backfill_run_costs", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                init_db(self.db_path)
        state = conn.execute("SELECT cost_status FROM runs WHERE id=?", (row_id,)).fetchone()
        self.assertEqual(state[0], "unknown")
        state = self._init().execute("SELECT cost_status FROM runs WHERE id=?", (row_id,)).fetchone()
        self.assertEqual(state[0], "estimated")

    def test_historical_evidence_requires_exact_link_and_replays_atomically(self):
        from modules.run_store.cost_recovery import recover_run_costs

        conn = self._init()
        stamp = _ts(1)
        row_id = self._insert(conn, provider="vibe", timestamp=stamp, model="vibe")
        entry = dict(id=row_id, provider="vibe", timestamp=stamp, source="linked export",
                     actual_usd=0, billing_mode="api")
        with self.assertRaisesRegex(ValueError, "does not match"):
            recover_run_costs(conn, [entry, dict(entry, id=row_id + 1)])
        self.assertEqual(run_cost_completeness(conn, now=NOW)["share"], 0.0)
        self.assertEqual(recover_run_costs(conn, [dict(entry, billing_mode=None)]), 0)
        self.assertEqual(recover_run_costs(conn, [entry]), 1)
        self.assertEqual(recover_run_costs(conn, [entry]), 0)
        self.assertEqual(run_cost_completeness(conn, now=NOW)["share"], 1.0)

    def test_azure_weekly_review_audits_failure_and_success(self):
        from modules.nexus_delegation import DelegationResult
        from modules.weekly_review import default_runners

        runner = default_runners(self.db_path)["azure"]
        with patch("modules.nexus_delegation.NexusDelegationService") as service:
            from unittest.mock import AsyncMock

            service.return_value.azure_chat = AsyncMock(side_effect=[
                RuntimeError("transport"),
                DelegationResult("azure_openai", True, "answer", {
                    "deployment": "gpt-4o",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5},
                }),
            ])
            with self.assertRaisesRegex(RuntimeError, "transport"):
                runner("prompt", self.tmp.name)
            self.assertEqual(runner("prompt", self.tmp.name), "answer")
        rows = self._init().execute(
            "SELECT run_kind, success, cost_status, input_tokens FROM runs ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("weekly_review", 0, "unknown", 0), ("weekly_review", 1, "estimated", 100),
        ])


if __name__ == "__main__":
    unittest.main()
