"""Small, read-only post-experiment reflection using Nexus's local model.

This module deliberately only records suggestions.  It does not create issues,
change experiment state, or otherwise act on model output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from modules import heartbeat
from modules.llama_cpp_client import LlamaCppClient
from modules.local_llm_jobs import (
    ROLE_EXPERIMENT_REFLECTION,
    acquire_local_llm_job_service,
    release_local_llm_job_service,
)
from modules.run_store import DEFAULT_DB_PATH, init_db

MAX_SUMMARY_CHARS = 2000
MAX_SUGGESTIONS = 3
MAX_TOKENS = 500
_FIELDS = ("title", "benefit", "cost", "risk", "measure")

# A pending claim states its owner's liveness through ``lease_expires_at``,
# refreshed while inference is in flight. A lease that has run out belonged to
# a caller that stopped (crash, kill, failed finalization) and may be taken
# over, so an interrupted reflection is retried instead of suppressed forever.
_LEASE_ENV_VAR = "NEXUS_REFLECTION_LEASE_SECONDS"
_DEFAULT_LEASE_SECONDS = 300
_BUSY_TIMEOUT_MS = 10_000

_LOGGER = logging.getLogger(__name__)


def _compact_json(value: Any) -> str:
    if not value:
        return "none"
    try:
        return json.dumps(json.loads(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, json.JSONDecodeError):
        return str(value)


def _changed_files(row: dict[str, Any]) -> str:
    names: list[str] = []
    for line in (row.get("git_status") or "").splitlines():
        text = line.strip()
        if not text:
            continue
        # Porcelain status has two status columns; renames use "old -> new".
        name = text[2:].strip() if len(text) > 2 else text
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        if name and name not in names:
            names.append(name)
    return ", ".join(names) or str(row.get("diff_stat") or "none").strip()


def _summary(row: dict[str, Any]) -> str:
    timings = {
        name: row.get(f"{name}_runtime_ms")
        for name in ("selection", "research", "reformat_retry", "policy_replan", "implementation")
        if row.get(f"{name}_runtime_ms") is not None
    }
    usage = {
        name: _compact_json(row.get(f"{name}_usage_json"))
        for name in ("selection", "research", "reformat_retry", "policy_replan", "implementation")
        if row.get(f"{name}_usage_json")
    }
    test_result = (
        "not recorded" if row.get("test_exit_code") is None
        else ("passed" if row["test_exit_code"] == 0 else f"failed (exit {row['test_exit_code']})")
    )
    parts = [
        f"Goal: {str(row.get('goal') or 'none')[:600]}",
        f"Plan: {_compact_json(row.get('plan_json'))[:400]}",
        f"Changed files: {_changed_files(row)[:300]}",
        f"Phase timings (ms): {json.dumps(timings, separators=(',', ':'))}",
        f"Token usage: {json.dumps(usage, separators=(',', ':'))}",
        f"Test result: {test_result}",
        f"Test output: {(row.get('test_output') or 'none')[:500]}",
        f"Error: {(row.get('error') or 'none')[:500]}",
    ]
    text = "\n".join(parts)
    return text if len(text) <= MAX_SUMMARY_CHARS else text[: MAX_SUMMARY_CHARS - 16] + "\n...[truncated]"


def _tokens(title: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", title.lower()))


def _duplicate(title: str, open_titles: Iterable[str]) -> str | None:
    candidate = _tokens(title)
    if not candidate:
        return None
    best: tuple[float, str] | None = None
    for open_title in open_titles:
        existing = _tokens(open_title)
        if not existing:
            continue
        # Containment overlap catches a concise suggestion matching a longer issue.
        score = len(candidate & existing) / min(len(candidate), len(existing))
        if score >= 0.6 and (best is None or score > best[0]):
            best = (score, open_title)
    return best[1] if best else None


def _parse(raw: str, open_titles: Iterable[str]) -> list[dict[str, str]]:
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"suggestions"}:
        raise ValueError("response must be an object containing only suggestions")
    suggestions = value["suggestions"]
    if not isinstance(suggestions, list):
        raise ValueError("suggestions must be an array")
    result: list[dict[str, str]] = []
    for item in suggestions[:MAX_SUGGESTIONS]:
        if not isinstance(item, dict) or set(item) != set(_FIELDS):
            raise ValueError("each suggestion must contain title, benefit, cost, risk and measure")
        if any(not isinstance(item[field], str) or not item[field].strip() for field in _FIELDS):
            raise ValueError("all suggestion fields must be non-empty strings")
        suggestion = {field: item[field].strip() for field in _FIELDS}
        duplicate_of = _duplicate(suggestion["title"], open_titles)
        if duplicate_of:
            suggestion["duplicate_of"] = duplicate_of
        result.append(suggestion)
    return result


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_seconds() -> int:
    return heartbeat.resolve_stale_timeout(
        os.environ.get(_LEASE_ENV_VAR, _DEFAULT_LEASE_SECONDS)
    )


def _lease_deadline(lease_seconds: float) -> str:
    return (_utcnow() + timedelta(seconds=lease_seconds)).isoformat()


def _lease_expired(value: str | None) -> bool:
    """A missing or unreadable lease counts as expired: it cannot prove liveness."""
    if not value:
        return True
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= _utcnow()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = init_db(db_path)
    # Autocommit, so the claim below is one explicit IMMEDIATE transaction.
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn


def _claim(
    conn: sqlite3.Connection, experiment_id: str, model: str, token: str, lease_seconds: float
) -> tuple[str, list[dict[str, str]]]:
    """Atomically decide who runs the reflection.

    Returns ``("claimed", [])`` when this caller now owns the row,
    ``("completed", suggestions)`` when it is already done, and
    ``("busy", [])`` while another live caller holds an unexpired claim.
    BEGIN IMMEDIATE takes the write lock before reading, so two callers can
    never both see "no row" or both take over the same stale claim.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT status, suggestions_json, lease_expires_at
               FROM experiment_reflections WHERE experiment_id=?""",
            (experiment_id,),
        ).fetchone()
        if row is not None and row["status"] == "completed":
            conn.execute("COMMIT")
            return "completed", json.loads(row["suggestions_json"])
        if row is not None and not _lease_expired(row["lease_expires_at"]):
            conn.execute("COMMIT")
            return "busy", []
        now = _utcnow().isoformat()
        deadline = _lease_deadline(lease_seconds)
        if row is None:
            conn.execute(
                """INSERT INTO experiment_reflections
                   (experiment_id, created_at, model, suggestions_json, error,
                    status, claim_token, lease_expires_at)
                   VALUES (?, ?, ?, '[]', NULL, 'pending', ?, ?)""",
                (experiment_id, now, model, token, deadline),
            )
        else:
            # Stale pending claim: its owner stopped. Take it over.
            _LOGGER.warning(
                "experiment_reflection reclaiming stale claim experiment_id=%s", experiment_id
            )
            conn.execute(
                """UPDATE experiment_reflections
                   SET created_at=?, model=?, suggestions_json='[]', error=NULL,
                       claim_token=?, lease_expires_at=?
                   WHERE experiment_id=? AND status='pending'""",
                (now, model, token, deadline, experiment_id),
            )
        conn.execute("COMMIT")
        return "claimed", []
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _finalize(
    conn: sqlite3.Connection,
    experiment_id: str,
    token: str,
    suggestions: list[dict[str, str]],
    error: str | None,
) -> bool:
    """Complete the claim. Owner-conditional: a caller whose stale claim was
    taken over cannot overwrite the new owner's row."""
    return conn.execute(
        """UPDATE experiment_reflections
           SET suggestions_json=?, error=?, status='completed',
               claim_token=NULL, lease_expires_at=NULL
           WHERE experiment_id=? AND claim_token=? AND status='pending'""",
        (json.dumps(suggestions, ensure_ascii=False, separators=(",", ":")),
         error, experiment_id, token),
    ).rowcount == 1


def _release(conn: sqlite3.Connection, experiment_id: str, token: str) -> None:
    """Give an unfinished claim back so the next call can retry immediately."""
    conn.execute(
        """DELETE FROM experiment_reflections
           WHERE experiment_id=? AND claim_token=? AND status='pending'""",
        (experiment_id, token),
    )


class _LeaseKeeper:
    """Refreshes a claim's lease while its owner is alive and working."""

    def __init__(
        self, db_path: str, experiment_id: str, token: str, lease_seconds: float,
        *, interval: float | None = None,
    ) -> None:
        self._db_path = db_path
        self._experiment_id = experiment_id
        self._token = token
        self._lease_seconds = lease_seconds
        self._interval = interval if interval is not None else max(1.0, lease_seconds / 3)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"reflection-lease-{experiment_id}", daemon=True
        )

    def start(self) -> "_LeaseKeeper":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
                try:
                    with conn:
                        owned = conn.execute(
                            """UPDATE experiment_reflections SET lease_expires_at=?
                               WHERE experiment_id=? AND claim_token=? AND status='pending'""",
                            (_lease_deadline(self._lease_seconds), self._experiment_id, self._token),
                        ).rowcount
                finally:
                    conn.close()
            except Exception:  # a missed refresh only shortens the lease
                _LOGGER.exception(
                    "experiment_reflection lease refresh failed experiment_id=%s",
                    self._experiment_id,
                )
                continue
            if not owned:
                return  # finalized, released or taken over: nothing left to keep alive


def _run_model(
    client: Any, db_path: str, experiment_id: str, prompt: str, open_titles: tuple[str, ...]
) -> tuple[list[dict[str, str]], str | None]:
    jobs = None
    try:
        jobs = acquire_local_llm_job_service(db_path=db_path, llama_client=client)
        job_id = jobs.submit(
            prompt,
            system="Return strict JSON only. Do not include markdown or commentary.",
            temperature=0.0,
            max_tokens=MAX_TOKENS,
            session_id=experiment_id,
            role=ROLE_EXPERIMENT_REFLECTION,
        )
        raw = asyncio.run(jobs.wait(job_id))
        return _parse(raw, open_titles), None
    except Exception as exc:  # model and malformed output are persisted, never lifecycle failures
        return [], f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        if jobs is not None:
            release_local_llm_job_service(jobs)


def reflect(
    experiment_id: str,
    open_issue_titles: Iterable[str] = (),
    *,
    llm: Any | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, str]]:
    """Reflect once on an experiment and persist at most three suggestions.

    Exactly one caller runs the model: the one holding the claim. A completed
    reflection is returned as stored; while another live caller holds the
    claim, ``[]`` is returned without running the model. A claim left behind
    by a caller that stopped (its lease ran out) is taken over and retried.
    Never changes the experiment row and never acts on a suggestion.
    """
    conn = _connect(db_path)
    try:
        record = conn.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if record is None:
            raise ValueError(f"unknown experiment: {experiment_id}")
        row = dict(record)
        client = llm or LlamaCppClient()
        model = str(getattr(client, "model", client.__class__.__name__))
        token = uuid.uuid4().hex
        lease_seconds = _lease_seconds()

        state, stored = _claim(conn, experiment_id, model, token, lease_seconds)
        if state != "claimed":
            return stored

        prompt = (
            "Review this completed experiment and propose at most three concrete improvements. "
            "No useful improvement is valid: return {\"suggestions\":[]}. Otherwise return "
            "{\"suggestions\":[{\"title\":\"...\",\"benefit\":\"...\",\"cost\":\"...\","
            "\"risk\":\"...\",\"measure\":\"...\"}]}.\n\n" + _summary(row)
        )
        keeper = _LeaseKeeper(db_path, experiment_id, token, lease_seconds).start()
        finalized = False
        try:
            suggestions, error = _run_model(
                client, db_path, experiment_id, prompt, tuple(open_issue_titles)
            )
            finalized = _finalize(conn, experiment_id, token, suggestions, error)
            if not finalized:
                _LOGGER.warning(
                    "experiment_reflection claim lost before finalization experiment_id=%s",
                    experiment_id,
                )
        finally:
            keeper.stop()
            if not finalized:
                # Interrupted or failed finalization: hand the claim back. If
                # even this fails (or the process dies), the lease expires.
                try:
                    _release(conn, experiment_id, token)
                except Exception:
                    _LOGGER.exception(
                        "experiment_reflection release failed experiment_id=%s; "
                        "claim recovers when its lease expires", experiment_id,
                    )
        stored_row = conn.execute(
            "SELECT status, suggestions_json FROM experiment_reflections WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if stored_row is None or stored_row["status"] != "completed":
            return []
        return json.loads(stored_row["suggestions_json"])
    finally:
        conn.close()
