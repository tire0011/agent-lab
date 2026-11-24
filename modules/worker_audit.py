"""Durable accounting for one provider invocation."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import logging
import sqlite3
import time

from modules.run_store import _connect, init_db, insert_run, resolve_run_cost


class RunAudit:
    def __init__(self, db_path, run_id, provider, timestamp):
        self.db_path = db_path
        self.run_id = run_id
        self.provider = provider
        self.timestamp = timestamp

    def finish(self, *, model, success, error, runtime_ms, usage=None,
               model_usage=None, actual_cost=None, subscription=False, price_usage=True,
               thread_id=None):
        if self.run_id is None:
            return
        for attempt in range(2):
            try:
                with closing(_connect(self.db_path)) as conn, conn:
                    cost = resolve_run_cost(
                        conn, provider=self.provider, model=model, timestamp=self.timestamp,
                        usage=usage, model_usage=model_usage if price_usage else {}, actual_cost=actual_cost,
                        subscription=subscription,
                    )
                    usage = usage or {}
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    evidence = json.dumps({
                        "usage": usage, "model_usage": model_usage, "actual_usd": actual_cost,
                        "subscription": subscription, "price_usage": price_usage, "thread_id": thread_id,
                    }, allow_nan=False)
                    conn.execute(
                        """UPDATE runs SET model=?, success=?, error=?, runtime_ms=?,
                           input_tokens=?, output_tokens=?, total_tokens=?, estimated_cost=?,
                           actual_cost=?, cost_status=?, cost_currency=?, billing_evidence_json=? WHERE id=?""",
                        (model, success, error, runtime_ms, input_tokens, output_tokens,
                         input_tokens + output_tokens, cost["estimated_cost"], cost["actual_cost"],
                         cost["cost_status"], cost["cost_currency"], evidence, self.run_id),
                    )
            except (ValueError, OverflowError, TypeError):
                logging.getLogger(__name__).warning("Invalid billing evidence for run %s", self.run_id)
                return
            except sqlite3.Error:
                if attempt:
                    logging.getLogger(__name__).warning("Unable to finalize run audit %s", self.run_id)
            else:
                self.run_id = None
                return

    def worker_result(self, result, *, subscription=False, price_usage=True):
        self.finish(
            model=result.billing_model or self.provider, success=result.success,
            error=result.error, runtime_ms=result.runtime_ms, usage=result.usage,
            model_usage=result.model_usage, actual_cost=result.cost_usd, thread_id=result.thread_id,
            subscription=subscription or result.subscription, price_usage=price_usage,
        )


@contextmanager
def audit_run(db_path, provider, phase, task):
    started = time.monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()
    run_id = None
    if db_path is not None:
        try:
            with closing(init_db(db_path)) as conn:
                run_id = insert_run(
                    conn, timestamp=timestamp, provider=provider, model=provider,
                    input_description=task[:500], input_tokens=0, output_tokens=0,
                    total_tokens=0, runtime_ms=0, estimated_cost=None, actual_cost=None,
                    cost_status="unknown", run_kind=phase, success=False,
                    error="Provider attempt did not finalize",
                )
        except Exception:
            logging.getLogger(__name__).warning("Unable to start %s run audit", provider)
    audit = RunAudit(db_path, run_id, provider, timestamp)
    try:
        yield audit
    except BaseException as exc:
        audit.finish(
            model=provider, success=False, error=type(exc).__name__,
            runtime_ms=round((time.monotonic() - started) * 1000),
        )
        raise
