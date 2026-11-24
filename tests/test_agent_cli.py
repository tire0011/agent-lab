import subprocess
import sys
import unittest
from argparse import Namespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

from nexus_version import VERSION


REPO_ROOT = Path(__file__).resolve().parents[1]


class AgentCliTests(unittest.TestCase):
    def test_version_exits_without_loading_runtime_dependencies(self):
        result = subprocess.run(
            [sys.executable, "agent.py", "--version"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.stdout.strip(), VERSION)
        self.assertEqual(result.stderr, "")

    def test_router_accounting_stays_unknown_without_provider_usage(self):
        from agent import main

        for snapshot, failed in (
            ({"actual_cost_delta": 9.0, "currency": "EUR"}, False),
            ({"actual_cost_delta": 9.0, "currency": "EUR"}, True),
            (None, False),
        ):
            with (
                self.subTest(snapshot=snapshot, failed=failed),
                patch("agent.parse_args", return_value=Namespace(task=["x"], use_router=True)),
                patch("agent.run_router_pipeline", new_callable=AsyncMock) as run,
                patch("modules.run_store.init_db"),
                patch("modules.run_store.insert_run") as insert,
                patch("modules.cost_monitor.AzureCostMonitor") as monitor,
                patch("modules.run_log.log_run"),
                patch("modules.cli_output.print_cost_summary"),
            ):
                monitor.return_value.summarize.return_value = snapshot
                if failed:
                    run.side_effect = RuntimeError("provider failure")
                else:
                    run.return_value = "result"
                self.assertEqual(main(), int(failed))
                self.assertEqual(insert.call_count, 1)
                self.assertEqual(insert.call_args.kwargs["cost_status"], "unknown")
                self.assertIsNone(insert.call_args.kwargs["actual_cost"])
                self.assertIsNone(insert.call_args.kwargs["estimated_cost"])


if __name__ == "__main__":
    unittest.main()
