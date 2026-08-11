import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class AzureCostSnapshot:
    timestamp: str
    amount: float
    currency: str
    period: str = "MonthToDate"
    source: str = "Azure Cost Management PreTaxCost"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "amount": self.amount,
            "currency": self.currency,
            "period": self.period,
            "source": self.source,
        }


class AzureCostMonitor:
    API_VERSION = "2024-08-01"

    def __init__(self, enabled: bool | None = None, az_command: str = "az") -> None:
        if enabled is None:
            enabled = os.getenv("AGENT_COST_MONITOR", "1").lower() not in {
                "0",
                "false",
                "no",
            }
        self.enabled = enabled
        self.az_command = az_command
        self._subscription_id: str | None = None

    def _run_az(self, args: list[str]) -> str:
        completed = subprocess.run(
            [self.az_command, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return completed.stdout

    def _get_subscription_id(self) -> str:
        if self._subscription_id is None:
            self._subscription_id = self._run_az(
                ["account", "show", "--query", "id", "--output", "tsv"]
            ).strip()
            if not self._subscription_id:
                raise RuntimeError("Azure CLI returned an empty subscription ID")
        return self._subscription_id

    def capture(self) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled"}

        try:
            subscription_id = self._get_subscription_id()
            body = json.dumps(
                {
                    "type": "Usage",
                    "timeframe": "MonthToDate",
                    "dataset": {
                        "granularity": "None",
                        "aggregation": {
                            "totalCost": {
                                "name": "PreTaxCost",
                                "function": "Sum",
                            }
                        },
                    },
                }
            )
            output = self._run_az(
                [
                    "rest",
                    "--method",
                    "post",
                    "--url",
                    (
                        "https://management.azure.com/subscriptions/"
                        f"{subscription_id}/providers/Microsoft.CostManagement/"
                        f"query?api-version={self.API_VERSION}"
                    ),
                    "--headers",
                    "Content-Type=application/json",
                    "--body",
                    body,
                ]
            )
            result = json.loads(output)
            properties = result.get("properties", {})
            columns = [column["name"] for column in properties.get("columns", [])]
            rows = properties.get("rows", [])
            if not rows:
                raise RuntimeError("Azure Cost Management returned no cost row")

            snapshot = AzureCostSnapshot(
                timestamp=datetime.now(timezone.utc).isoformat(),
                amount=float(rows[0][columns.index("PreTaxCost")]),
                currency=str(rows[0][columns.index("Currency")]),
            )
            return {"status": "ok", **snapshot.to_dict()}
        except Exception as exc:
            return {
                "status": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def summarize(
        before: dict[str, Any], after: dict[str, Any]
    ) -> dict[str, Any]:
        summary = {
            "before": before,
            "after": after,
            "actual_cost_delta": None,
            "status": "unavailable",
        }
        if before.get("status") == "disabled" and after.get("status") == "disabled":
            summary["status"] = "disabled"
            return summary
        if before.get("status") != "ok" or after.get("status") != "ok":
            return summary
        if before["currency"] != after["currency"]:
            summary["status"] = "inconsistent_currency"
            return summary

        summary["currency"] = after["currency"]
        if after["amount"] > before["amount"]:
            summary["actual_cost_delta"] = round(
                after["amount"] - before["amount"], 10
            )
            summary["status"] = "updated"
        else:
            summary["status"] = "pending"
        return summary
