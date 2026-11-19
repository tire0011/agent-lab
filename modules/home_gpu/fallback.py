"""Advance ready home GPU work from the offline replay phase of the tick (#743).

``advance_one`` does at most one unit of model or scoring work per tick:

1. deadlines and offline detection (local, see ``collect.advance_deadlines``);
2. checkers for answered generic jobs (no code is run);
3. one hunk/bake-off job: its answer is scored by the hunk replay, or - at
   night, as a VM fallback - the VM worker produces and scores it;
4. at night, one generic batch job through the VM's bounded local path.

No job can block the others. A job whose replay run or unit is already
finished is reconciled to that result. A job that could not make progress
for a reason that can heal gets a growing back-off and the next ready job is
tried; after ``MAX_CHECK_ATTEMPTS`` it is recorded as an error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from handlers.batch_job import MAX_PAYLOAD, llama_request
from modules import local_shadow_replay as lsr
from modules.home_gpu import collect
from modules.home_gpu.collect import record_check
from modules.home_gpu.jobs import enabled
from modules.redaction import contains_credential

logger = logging.getLogger(__name__)
MAX_CHECK_ATTEMPTS = 8
MAX_FALLBACK_ATTEMPTS = 3
MAX_CANDIDATES_PER_TICK = 5
ROLE_HOME_GPU_FALLBACK = "home_gpu_fallback"
# Failures of the shared local job service that can heal on a later night.
RETRYABLE_KINDS = frozenset({"timeout", "connection", "protocol", "interrupted", "stuck", "cancelled"})


def _backoff(attempts: int) -> float:
    return min(6 * 3600.0, 60.0 * 2 ** max(0, attempts))


def advance_one(repo_root: str, db_path: str, *, now: float | None = None) -> bool:
    """True when one unit of model or scoring work ran."""
    if not enabled():
        return False
    now = time.time() if now is None else now
    collect.advance_deadlines(db_path, now)
    collect.run_checks(Path(repo_root), db_path)
    if _advance_replay(repo_root, db_path, now):
        return True
    return collect.night_window(now) and _vm_fallback(repo_root, db_path, now)


# ---- hunk and bake-off jobs --------------------------------------------------

def _defer(db_path: str, job: dict, now: float, reason: str) -> None:
    attempts = int(job["check_attempts"] or 0) + 1
    with closing(lsr._connect(db_path)) as conn:
        if attempts >= MAX_CHECK_ATTEMPTS:
            record_check(conn, job["job_id"], "error", f"gave up after {attempts} attempts: {reason}", now)
            return
        conn.execute(
            "UPDATE home_gpu_jobs SET check_attempts=?, next_check_at=?, check_detail=? "
            "WHERE job_id=? AND checked_at IS NULL",
            (attempts, now + _backoff(attempts), reason[:500], job["job_id"]),
        )


def _reconcile(conn: sqlite3.Connection, job: dict, key: dict, now: float) -> bool:
    """Record the result the replay already has for this job; True when it was terminal."""
    from modules.local_shadow_replay_hunks import FINISHED_RUN_STATUSES, UNIT_RUNNING, UNIT_SCORED

    unit = None
    if key["file"] is not None:
        unit = conn.execute(
            "SELECT status, tests_passed, error FROM local_replay_units WHERE experiment_id=? "
            "AND file=? AND symbol=? AND prompt_variant=?",
            (key["experiment_id"], key["file"], key["symbol"], key["variant"]),
        ).fetchone()
    if unit is not None and unit["status"] != UNIT_RUNNING:
        status = "passed" if unit["tests_passed"] else (
            "failed" if unit["status"] == UNIT_SCORED else "error")
        record_check(conn, job["job_id"], status, unit["error"], now)
        return True
    run = conn.execute(
        "SELECT status, error FROM local_replay_unit_runs WHERE experiment_id=? AND prompt_variant=?",
        (key["experiment_id"], key["variant"]),
    ).fetchone()
    if run is not None and run["status"] in FINISHED_RUN_STATUSES:
        record_check(conn, job["job_id"], "error",
                     f"replay run {run['status']}: {run['error'] or 'unit not scored'}", now)
        return True
    return False


def _advance_replay(repo_root: str, db_path: str, now: float) -> bool:
    from modules.home_gpu.replay import parse_replay_key
    from modules.local_shadow_replay_hunks import replay_hunks

    with closing(lsr._connect(db_path)) as conn:
        jobs = [dict(row) for row in conn.execute(
            "SELECT * FROM home_gpu_jobs WHERE replay_key IS NOT NULL AND checked_at IS NULL "
            "AND (status IN ('answered', 'expired') OR (status='fallback' AND answer IS NULL AND ?)) "
            "AND COALESCE(next_check_at, 0) <= ? ORDER BY COALESCE(next_check_at, 0), created_at "
            "LIMIT ?", (collect.night_window(now), now, MAX_CANDIDATES_PER_TICK),
        )]
    for job in jobs:
        key = parse_replay_key(job["replay_key"])
        with closing(lsr._connect(db_path)) as conn:
            if _reconcile(conn, job, key, now):
                continue
        try:
            result = replay_hunks(
                key["experiment_id"], repo_root=repo_root, db_path=db_path, variant=key["variant"],
                via="home-gpu", seed=key["seed"], home_model=key["model"], home_kind=key["kind"],
                resume_only=True, max_units=1, home_job_id=job["job_id"],
            )
        except ValueError as exc:  # the experiment cannot be replayed at all
            with closing(lsr._connect(db_path)) as conn:
                record_check(conn, job["job_id"], "error", str(exc), now)
            continue
        except Exception as exc:  # noqa: BLE001 - one job never blocks the next
            _defer(db_path, job, now, f"{type(exc).__name__}: {exc}")
            continue
        if result is None:
            return False  # another replay owns the server; try again next tick
        if result.get("ran_units"):
            return True
        with closing(lsr._connect(db_path)) as conn:
            if _reconcile(conn, job, key, now):
                continue
        _defer(db_path, job, now, result.get("transient_error") or "no progress")
    return False


# ---- generic batch jobs ------------------------------------------------------

def _prompt(messages: list[dict]) -> tuple[str | None, str]:
    system = messages[0]["content"] if messages[0]["role"] == "system" else None
    rest = messages[1:] if system is not None else messages
    if len(rest) == 1 and rest[0]["role"] == "user":
        return system, rest[0]["content"]
    return system, "\n\n".join(f"{m['role']}: {m['content']}" for m in rest)


def admission(system: str | None, prompt: str, max_tokens: int) -> tuple[int | None, str]:
    """``(max_tokens, reason)`` from the VM's local call budget; ``None`` when unaffordable."""
    from modules.local_coding_worker import SAFE_BYTES_PER_TOKEN, time_budget
    from modules.local_file_localizer import measured_rates
    from modules.local_model_rates import LocalModelRates

    rates = measured_rates(LocalModelRates())
    budget = time_budget(lsr.offline_local_call_timeout_seconds(), max_tokens, rates)
    needed = -(-len(((system or "") + prompt).encode("utf-8")) // SAFE_BYTES_PER_TOKEN)
    if budget is None or needed > budget.prompt_tokens:
        allowance = budget.prompt_tokens if budget else 0
        return None, f"prompt needs ~{needed} tokens, budget {allowance}"
    return budget.output_tokens, "ok"


def _terminal(db_path: str, job_id: str, now: float, error: str) -> None:
    with closing(lsr._connect(db_path)) as conn:
        conn.execute("UPDATE home_gpu_jobs SET error=? WHERE job_id=?", (error[:500], job_id))
        record_check(conn, job_id, "error", error, now)


def _vm_fallback(repo_root: str, db_path: str, now: float) -> bool:
    from modules.local_llm_jobs import (
        LocalLlmJobError, acquire_local_llm_job_service, release_local_llm_job_service,
    )
    from modules.llama_cpp_client import json_schema_response_format

    with closing(lsr._connect(db_path)) as conn:
        found = conn.execute(
            "SELECT * FROM home_gpu_jobs WHERE status='fallback' AND replay_key IS NULL "
            "AND answer IS NULL AND checked_at IS NULL AND COALESCE(next_fallback_at, 0) <= ? "
            "ORDER BY created_at LIMIT 1", (now,),
        ).fetchone()
        if not found:
            return False
        job = dict(found)
        lease = lsr._acquire_global_lease(conn, lsr.lease_seconds())
        if not lease:
            return False
        try:
            if job["fallback_job_id"] is None:
                idle, _ = lsr.server_state(lsr.default_replay_client(), db_path)
                if not idle:
                    return False
            request = llama_request(job["kind"], json.loads(job["payload"])["request"])
            if "grammar" in request:
                _terminal(db_path, job["job_id"], now, "home_gpu_fallback_unsupported: grammar")
                return True
            system, prompt = _prompt(request["messages"])
            max_tokens, reason = admission(system, prompt, request.get("max_tokens", 1024))
            if max_tokens is None:
                _terminal(db_path, job["job_id"], now, f"home_gpu_fallback_unaffordable: {reason}")
                return True
            service = acquire_local_llm_job_service(db_path=db_path)
            try:
                local_id = job["fallback_job_id"]
                if local_id is None:
                    local_id = service.submit(
                        prompt, system=system, max_tokens=max_tokens, role=ROLE_HOME_GPU_FALLBACK,
                        response_format=(json_schema_response_format("home_gpu_job", request["json_schema"])
                                         if "json_schema" in request else None),
                    )
                    # Durable before waiting: a crash resumes this local job, never a second one.
                    conn.execute("UPDATE home_gpu_jobs SET fallback_job_id=? WHERE job_id=? "
                                 "AND fallback_job_id IS NULL", (local_id, job["job_id"]))
                started = time.monotonic()
                try:
                    answer = asyncio.run(service.wait(local_id))
                except LocalLlmJobError as exc:
                    attempts = int(job["fallback_attempts"] or 0) + 1
                    unknown_job = exc.status is None and exc.kind == "unknown"
                    if (exc.kind in RETRYABLE_KINDS or unknown_job) and attempts < MAX_FALLBACK_ATTEMPTS:
                        conn.execute(
                            "UPDATE home_gpu_jobs SET fallback_attempts=?, next_fallback_at=?, "
                            "fallback_job_id=NULL, error=? WHERE job_id=?",
                            (attempts, now + _backoff(attempts), f"home_gpu_fallback_retry: {exc.kind}",
                             job["job_id"]),
                        )
                    else:
                        conn.execute("UPDATE home_gpu_jobs SET fallback_attempts=? WHERE job_id=?",
                                     (attempts, job["job_id"]))
                        _terminal(db_path, job["job_id"], now, f"home_gpu_fallback_failed: {exc.kind}")
                    return True
                seconds = time.monotonic() - started
                model = str(getattr(service.llama, "model", "") or "vm-local")
            finally:
                release_local_llm_job_service(service)
            if (not isinstance(answer, str) or not answer.strip() or "\x00" in answer
                    or len(answer.encode("utf-8")) > MAX_PAYLOAD or contains_credential(answer)):
                _terminal(db_path, job["job_id"], now, "home_gpu_fallback_invalid_answer")
                return True
            conn.execute(
                "UPDATE home_gpu_jobs SET answer=?, answered_at=?, answer_host='vm', model=?, "
                "seconds=?, error=NULL WHERE job_id=? AND answer IS NULL",
                (answer, time.time(), model, seconds if math.isfinite(seconds) else None, job["job_id"]),
            )
        finally:
            lsr._release_global_lease(conn, lease.token)
    # Inference is done; the checker is a separate, recorded step.
    collect.run_checks(Path(repo_root), db_path)
    return True
