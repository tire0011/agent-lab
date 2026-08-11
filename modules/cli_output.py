"""CLI output formatting for the agent runner (agent.py)."""

from typing import Any


def print_cost_summary(cost: dict[str, Any]) -> None:
    before = cost["before"]
    after = cost["after"]
    if before.get("status") == "ok":
        print(
            f"Azure cost before run: {before['amount']} {before['currency']} "
            f"({before['period']})"
        )
    if after.get("status") == "ok":
        print(
            f"Azure cost after run: {after['amount']} {after['currency']} "
            f"({after['period']})"
        )
    if cost["status"] == "updated":
        print(
            f"Actual Azure cost delta: {cost['actual_cost_delta']} "
            f"{cost['currency']}"
        )
    elif cost["status"] == "pending":
        print("Actual Azure run cost: pending Azure Cost Management refresh")
    elif cost["status"] != "disabled":
        print("Actual Azure cost: unavailable; see run artifact")


def print_run_summary(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    estimated_cost: float | None,
    estimated_cost_currency: str | None,
    runtime_ms: int,
    run_id: int,
) -> None:
    print(f"Tokens: {prompt_tokens} input / {completion_tokens} output")
    if estimated_cost is None:
        print("Estimated cost: pricing unavailable")
    else:
        currency = estimated_cost_currency or "USD"
        print(f"Estimated cost: {currency} {estimated_cost:.6f}")
    print(f"Runtime: {runtime_ms} ms")
    print(f"Run ID: {run_id}")
