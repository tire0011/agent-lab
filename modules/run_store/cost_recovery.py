"""Apply billing evidence that can be attributed to an exact historical run."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .records import resolve_run_cost


def recover_run_costs(conn: sqlite3.Connection, evidence: list[dict[str, Any]]) -> int:
    """Fill unknown costs in one transaction from separately verified records.

    Each entry names the run id, provider, timestamp and an evidence reference.
    A mismatch aborts the batch. Replaying a batch leaves recovered rows alone.
    """
    updated = 0
    with conn:
        for entry in evidence:
            run_id = entry["id"]
            source = entry["source"]
            if type(run_id) is not int or not isinstance(source, str) or not source.strip():
                raise ValueError("Each recovery entry needs a run id and evidence source")
            row = conn.execute(
                """SELECT provider, timestamp, model, cost_status, actual_cost, estimated_cost
                   FROM runs WHERE id = ?""",
                (run_id,),
            ).fetchone()
            if row is None or (row[0], row[1]) != (entry["provider"], entry["timestamp"]):
                raise ValueError(f"Evidence does not match run {run_id}")
            provider, timestamp, old_model, status, actual, estimated = row
            if status != "unknown" or actual is not None or estimated is not None:
                continue

            model = entry.get("model") or old_model
            mode = entry.get("billing_mode")
            if provider in {"claude", "vibe"} and mode not in {"api", "subscription"}:
                continue
            cost = resolve_run_cost(
                conn, provider=provider, timestamp=timestamp, model=model,
                usage=entry.get("usage"), model_usage=entry.get("model_usage"),
                subscription=provider in {"claude", "vibe"} and mode == "subscription",
                actual_cost=entry.get("actual_usd") if provider == "vibe" else None,
            )
            if cost["cost_status"] == "unknown":
                continue
            cursor = conn.execute(
                """UPDATE runs SET model=?, estimated_cost=?, actual_cost=?, cost_status=?,
                          cost_currency=?, billing_evidence_json=?
                   WHERE id=? AND cost_status='unknown' AND actual_cost IS NULL AND estimated_cost IS NULL""",
                (model, cost["estimated_cost"], cost["actual_cost"], cost["cost_status"],
                 cost["cost_currency"], json.dumps({key: entry[key] for key in (
                     "source", "model", "usage", "model_usage", "billing_mode", "actual_usd",
                 ) if key in entry}, allow_nan=False), run_id),
            )
            updated += cursor.rowcount
    return updated
