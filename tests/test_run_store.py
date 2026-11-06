import sqlite3
import tempfile
import unittest
import importlib
import os
from pathlib import Path

import modules.run_store as run_store


class RunStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "agent.db")
        self.connections: list[sqlite3.Connection] = []

    def tearDown(self) -> None:
        for conn in self.connections:
            conn.close()
        self.temp_dir.cleanup()

    def _init_db(self) -> sqlite3.Connection:
        conn = run_store.init_db(self.db_path)
        self.connections.append(conn)
        return conn

    def test_database_initialization_creates_required_tables(self):
        conn = self._init_db()

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        self.assertIn("runs", tables)
        self.assertIn("model_prices", tables)
        self.assertIn("tool_calls", tables)
        self.assertIn("knowledge", tables)

    def test_storing_run_persists_row(self):
        conn = self._init_db()

        run_id = run_store.insert_run(
            conn,
            timestamp="2026-08-10T00:00:00+00:00",
            provider="azure_openai",
            model="gpt-4o",
            input_description="test task",
            input_tokens=10,
            output_tokens=2,
            total_tokens=12,
            runtime_ms=123,
            estimated_cost=0.001,
            actual_cost=None,
            cost_status="estimated",
            success=True,
            error=None,
        )

        row = conn.execute(
            "SELECT id, provider, model, total_tokens, success FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], run_id)
        self.assertEqual(row[1], "azure_openai")
        self.assertEqual(row[2], "gpt-4o")
        self.assertEqual(row[3], 12)
        self.assertEqual(row[4], 1)

    def test_price_lookup_returns_latest_applicable_price(self):
        conn = self._init_db()

        conn.execute(
            """
            INSERT INTO model_prices (
                provider,
                model,
                input_price_per_million_tokens,
                output_price_per_million_tokens,
                currency,
                valid_from,
                source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "azure_openai",
                "gpt-4o",
                2.5,
                10.0,
                "EUR",
                "2026-01-01T00:00:00+00:00",
                "test-source",
            ),
        )
        conn.execute(
            """
            INSERT INTO model_prices (
                provider,
                model,
                input_price_per_million_tokens,
                output_price_per_million_tokens,
                currency,
                valid_from,
                source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "azure_openai",
                "gpt-4o",
                3.0,
                12.0,
                "EUR",
                "2026-07-01T00:00:00+00:00",
                "test-source-2",
            ),
        )
        conn.commit()

        price = run_store.get_applicable_price(
            conn,
            provider="azure_openai",
            model="gpt-4o",
            at_timestamp="2026-08-10T00:00:00+00:00",
        )

        self.assertIsNotNone(price)
        assert price is not None
        self.assertEqual(price["input_price_per_million_tokens"], 3.0)
        self.assertEqual(price["output_price_per_million_tokens"], 12.0)
        self.assertEqual(price["currency"], "EUR")

    def test_estimated_cost_calculation(self):
        cost = run_store.calculate_estimated_cost(
            input_tokens=1200,
            output_tokens=80,
            input_price_per_million_tokens=2.5,
            output_price_per_million_tokens=10.0,
        )

        expected = (1200 / 1_000_000) * 2.5 + (80 / 1_000_000) * 10.0
        self.assertAlmostEqual(cost, expected, places=12)

    def test_unknown_model_price_returns_none(self):
        conn = self._init_db()

        price = run_store.get_applicable_price(
            conn,
            provider="azure_openai",
            model="unknown-model",
            at_timestamp="2026-08-10T00:00:00+00:00",
        )

        self.assertIsNone(price)

    def test_tool_calls_are_stored(self):
        conn = self._init_db()
        run_id = run_store.insert_run(
            conn,
            timestamp="2026-08-10T00:00:00+00:00",
            provider="azure_openai",
            model="gpt-4o",
            input_description="test task",
            input_tokens=10,
            output_tokens=2,
            total_tokens=12,
            runtime_ms=123,
            estimated_cost=None,
            actual_cost=None,
            cost_status="estimated",
            success=True,
            error=None,
        )

        trace = [{"tool_name": "list_directory", "arguments": "{}", "result": "ok"}]
        run_store.insert_tool_calls(conn, run_id, "2026-08-10T00:00:00+00:00", trace)

        row = conn.execute(
            "SELECT run_id, tool_name, success FROM tool_calls WHERE run_id = ?",
            (run_id,),
        ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], run_id)
        self.assertEqual(row[1], "list_directory")
        self.assertEqual(row[2], 1)

    def test_init_db_uses_external_default_path_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom_db = Path(tmp) / "external" / "agent.db"
            old_env = os.environ.get("AGENT_DB_PATH")
            conn = None
            os.environ["AGENT_DB_PATH"] = str(custom_db)
            try:
                reloaded = importlib.reload(run_store)
                conn = reloaded.init_db()
            finally:
                if conn is not None:
                    conn.close()
                if old_env is None:
                    os.environ.pop("AGENT_DB_PATH", None)
                else:
                    os.environ["AGENT_DB_PATH"] = old_env
                importlib.reload(run_store)
            self.assertTrue(custom_db.exists())

    def test_legacy_db_is_migrated_to_external_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy_db = Path(tmp) / "legacy.db"
            target_db = Path(tmp) / "var" / "lib" / "nexus" / "agent.db"
            legacy_conn = sqlite3.connect(legacy_db)
            legacy_conn.execute("CREATE TABLE migrate_check (v TEXT NOT NULL)")
            legacy_conn.execute("INSERT INTO migrate_check (v) VALUES ('ok')")
            legacy_conn.commit()
            legacy_conn.close()

            original_legacy = run_store.LEGACY_DB_PATH
            original_default = run_store.DEFAULT_DB_PATH
            conn = None
            run_store.LEGACY_DB_PATH = str(legacy_db)
            run_store.DEFAULT_DB_PATH = str(target_db)
            try:
                conn = run_store.init_db(str(target_db))
                row = conn.execute("SELECT v FROM migrate_check").fetchone()
            finally:
                if conn is not None:
                    conn.close()
                run_store.LEGACY_DB_PATH = original_legacy
                run_store.DEFAULT_DB_PATH = original_default

            self.assertIsNotNone(row)
            self.assertEqual(row[0], "ok")


if __name__ == "__main__":
    unittest.main()
