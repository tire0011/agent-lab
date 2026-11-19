"""Advance ready home GPU work from the existing offline replay tick."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import closing

from handlers.batch_job import llama_request
from modules import local_shadow_replay as lsr
from modules.home_gpu.collect import night_window
from modules.home_gpu.jobs import enabled
from modules.mailbox.core import _has_secret
from modules.run_store import init_db

logger = logging.getLogger(__name__)


def advance_one(repo_root: str, db_path: str) -> bool:
    """Run one ready checker, or one nighttime VM fallback if the server is idle."""
    if not enabled():
        return False
    now = time.time()
    with closing(init_db(db_path)) as conn:
        replay = conn.execute(
            "SELECT replay_key FROM home_gpu_jobs WHERE replay_key IS NOT NULL "
            "AND checked_at IS NULL AND (status='answered' OR (status='fallback' AND ?)) "
            "ORDER BY created_at LIMIT 1", (night_window(now),),
        ).fetchone()
    if replay:
        from modules.local_shadow_replay_hunks import replay_hunks

        experiment, variant, seed, model, kind = json.loads(replay[0])
        result = replay_hunks(experiment, repo_root=repo_root, db_path=db_path, variant=variant,
                              via="home-gpu", seed=seed, home_model=model, home_kind=kind,
                              resume_only=True, max_units=1)
        if result and result["ran_units"]:
            return True
    return night_window(now) and _vm_completion(db_path, now)


def _vm_completion(db_path: str, now: float) -> bool:
    with closing(lsr._connect(db_path)) as conn:
        row = conn.execute(
            "SELECT job_id, kind, payload FROM home_gpu_jobs "
            "WHERE status='fallback' AND replay_key IS NULL AND answer IS NULL "
            "AND checked_at IS NULL AND COALESCE(fallback_lease_until, 0) <= ? "
            "ORDER BY created_at LIMIT 1", (now,),
        ).fetchone()
        if not row:
            return False
        lease = lsr._acquire_global_lease(conn, lsr.lease_seconds())
        if not lease:
            return False
        try:
            client = lsr.default_replay_client()
            idle, _ = lsr.server_state(client, db_path)
            if not idle:
                return False
            token = now + lsr.lease_seconds()
            changed = conn.execute(
                "UPDATE home_gpu_jobs SET fallback_lease_until=? WHERE job_id=? "
                "AND checked_at IS NULL AND COALESCE(fallback_lease_until, 0) <= ?",
                (token, row["job_id"], now),
            ).rowcount
            if not changed:
                return False
            started = time.monotonic()
            try:
                payload = llama_request(row["kind"], json.loads(row["payload"])["request"])
                # The VM client retains its own transport and timeout settings.
                payload.pop("stream")
                response = asyncio.run(client.chat_completion(**payload))
                answer = response["choices"][0]["message"]["content"]
                usage = response["usage"]
                prompt_count, completion_count = usage["prompt_tokens"], usage["completion_tokens"]
                if (not isinstance(answer, str) or not answer.strip() or _has_secret(answer)
                        or len(answer.encode()) > 65536 or "\x00" in answer
                        or any(type(count) is not int or count < 0
                               for count in (prompt_count, completion_count))):
                    raise ValueError("invalid fallback response")
                seconds = time.monotonic() - started
                rate = completion_count / seconds if seconds else 0
                if not math.isfinite(rate):
                    raise ValueError("invalid fallback timing")
                conn.execute(
                    "UPDATE home_gpu_jobs SET answer=?, model=?, prompt_tokens=?, completion_tokens=?, "
                    "seconds=?, tokens_per_second=?, checked_at=?, fallback_lease_until=NULL "
                    "WHERE job_id=? AND fallback_lease_until=?",
                    (answer, client.model, prompt_count, completion_count, seconds, rate,
                     time.time(), row["job_id"], token),
                )
            except Exception as exc:
                logger.warning("Home GPU VM fallback failed for %s: %s", row["job_id"], type(exc).__name__)
                conn.execute(
                    "UPDATE home_gpu_jobs SET error='home_gpu_fallback_failed', checked_at=?, "
                    "fallback_lease_until=NULL WHERE job_id=? AND fallback_lease_until=?",
                    (time.time(), row["job_id"], token),
                )
            return True
        finally:
            lsr._release_global_lease(conn, lease.token)
