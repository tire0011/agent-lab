import json
import unittest
from unittest.mock import Mock

from agent import AzureCostMonitor


class AzureCostMonitorTests(unittest.TestCase):
    def test_capture_parses_actual_azure_cost(self):
        monitor = AzureCostMonitor(enabled=True)
        monitor._run_az = Mock(
            side_effect=[
                "subscription-id\n",
                json.dumps(
                    {
                        "properties": {
                            "columns": [
                                {"name": "PreTaxCost"},
                                {"name": "Currency"},
                            ],
                            "rows": [[2.79414, "EUR"]],
                        }
                    }
                ),
            ]
        )

        snapshot = monitor.capture()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["amount"], 2.79414)
        self.assertEqual(snapshot["currency"], "EUR")
        self.assertEqual(snapshot["source"], "Azure Cost Management PreTaxCost")
        self.assertEqual(monitor._run_az.call_count, 2)

    def test_capture_returns_unavailable_instead_of_raising(self):
        monitor = AzureCostMonitor(enabled=True)
        monitor._run_az = Mock(side_effect=RuntimeError("denied"))

        snapshot = monitor.capture()

        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn("RuntimeError: denied", snapshot["error"])

    def test_summarize_marks_unchanged_cost_as_pending(self):
        before = {"status": "ok", "amount": 2.7, "currency": "EUR"}
        after = {"status": "ok", "amount": 2.7, "currency": "EUR"}

        summary = AzureCostMonitor.summarize(before, after)

        self.assertEqual(summary["status"], "pending")
        self.assertIsNone(summary["actual_cost_delta"])
        self.assertEqual(summary["currency"], "EUR")

    def test_summarize_reports_only_actual_updated_delta(self):
        before = {"status": "ok", "amount": 2.7, "currency": "EUR"}
        after = {"status": "ok", "amount": 2.7041, "currency": "EUR"}

        summary = AzureCostMonitor.summarize(before, after)

        self.assertEqual(summary["status"], "updated")
        self.assertEqual(summary["actual_cost_delta"], 0.0041)
        self.assertEqual(summary["currency"], "EUR")

    def test_summarize_propagates_unavailable_status(self):
        before = {"status": "unavailable", "error": "denied"}
        after = {"status": "ok", "amount": 2.7, "currency": "EUR"}

        summary = AzureCostMonitor.summarize(before, after)

        self.assertEqual(summary["status"], "unavailable")
        self.assertIsNone(summary["actual_cost_delta"])

    def test_summarize_preserves_disabled_status(self):
        summary = AzureCostMonitor.summarize(
            {"status": "disabled"}, {"status": "disabled"}
        )

        self.assertEqual(summary["status"], "disabled")
        self.assertIsNone(summary["actual_cost_delta"])


if __name__ == "__main__":
    unittest.main()
