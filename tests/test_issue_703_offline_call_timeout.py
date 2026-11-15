"""#703: offline replay calls use their replay budget, not production's 300s."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from modules import local_shadow_replay as lsr
from modules import local_shadow_replay_hunks as lsh
from modules.local_coding_worker import LocalCodingWorker, LocalTooSlowError, build_prompt_parts
from tests.test_issue_637_local_shadow_replay import FakeWorker, FULL_CALC, ReplayFixture


class OfflineCallTimeoutTests(unittest.TestCase):
    def test_default_and_environment_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(lsr.default_replay_client().timeout, 900.0)
        with patch.dict(os.environ, {lsr.ENV_OFFLINE_LOCAL_CALL_TIMEOUT: "725"}, clear=False):
            self.assertEqual(lsr.default_replay_client().timeout, 725.0)

    def test_whole_replay_timeout_is_capped_by_worker(self):
        env = {
            lsr.ENV_OFFLINE_LOCAL_CALL_TIMEOUT: "900",
            lsr.ENV_WORKER_TIMEOUT: "600",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(lsr.default_replay_client().timeout, 600.0)

    def test_hunk_replay_timeout_is_capped_by_unit(self):
        env = {
            lsr.ENV_OFFLINE_LOCAL_CALL_TIMEOUT: "900",
            lsh.ENV_UNIT_TIMEOUT: "450",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(lsh.default_hunk_replay_client().timeout, 450.0)

    def test_admission_can_afford_a_patch_with_offline_timeout(self):
        class Counter:
            source = "test"

            async def __call__(self, _text: str) -> int:
                return 1

        async def plan(timeout: float):
            client = type("Client", (), {"timeout": timeout})()
            return await build_prompt_parts(
                "Implement the fix.", "", ".", client,
                max_output_tokens=4096, n_ctx=8192, n_ctx_source="test",
                counter=Counter(), rates=(7.08, 2.61),
            )

        result = asyncio.run(plan(900.0))
        self.assertGreaterEqual(result.output_tokens, 768)
        with self.assertRaises(LocalTooSlowError):
            asyncio.run(plan(300.0))

    def test_replay_records_injected_clients_call_timeout(self):
        fixture = ReplayFixture(self)
        client = type("FakeClient", (), {"model": "fake", "timeout": 432.0})()
        row = lsr.replay(
            "exp-1", repo_root=fixture.repo, db_path=fixture.db_path, variant="timeout-metric",
            client=client, worker_factory=lambda _client: FakeWorker(FULL_CALC),
        )
        self.assertIsNotNone(row)
        conn = sqlite3.connect(fixture.db_path)
        try:
            metrics = json.loads(conn.execute(
                "SELECT metrics_json FROM local_replays WHERE experiment_id=? AND prompt_variant=?",
                ("exp-1", "timeout-metric"),
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(metrics["call_timeout_seconds"], 432.0)

    def test_production_worker_default_remains_300_seconds(self):
        self.assertEqual(LocalCodingWorker().client.timeout, 300.0)

    def test_worker_log_records_call_timeout(self):
        class FinishedMetrics:
            def finish(self):
                return {}

        class Log:
            fields = None

            def info(self, _phase, event, _message, **fields):
                if event == "local_run_metrics":
                    self.fields = fields

        client = type("Client", (), {"timeout": 654.0})()
        worker = LocalCodingWorker(client=client)
        worker._metrics = FinishedMetrics()
        log = Log()
        worker._log_run_metrics(log, "test", True)
        self.assertEqual(log.fields["call_timeout_seconds"], 654.0)


if __name__ == "__main__":
    unittest.main()
