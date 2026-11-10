"""Small, read-only post-experiment reflection using Nexus's local model.

This module deliberately only records suggestions.  It does not create issues,
change experiment state, or otherwise act on model output.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

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


def reflect(
    experiment_id: str,
    open_issue_titles: Iterable[str] = (),
    *,
    llm: Any | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, str]]:
    """Reflect once on an experiment and persist at most three suggestions."""
    conn = init_db(db_path)
    try:
        existing = conn.execute(
            "SELECT suggestions_json FROM experiment_reflections WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if existing is not None:
            return json.loads(existing[0])

        conn.row_factory = __import__("sqlite3").Row
        record = conn.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if record is None:
            raise ValueError(f"unknown experiment: {experiment_id}")
        row = dict(record)
        client = llm or LlamaCppClient()
        model = str(getattr(client, "model", client.__class__.__name__))
        prompt = (
            "Review this completed experiment and propose at most three concrete improvements. "
            "No useful improvement is valid: return {\"suggestions\":[]}. Otherwise return "
            "{\"suggestions\":[{\"title\":\"...\",\"benefit\":\"...\",\"cost\":\"...\","
            "\"risk\":\"...\",\"measure\":\"...\"}]}.\n\n" + _summary(row)
        )
        error: str | None = None
        suggestions: list[dict[str, str]] = []
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
            suggestions = _parse(raw, tuple(open_issue_titles))
        except Exception as exc:  # model and malformed output are persisted, never lifecycle failures
            error = f"{type(exc).__name__}: {exc}"[:1000]
        finally:
            if jobs is not None:
                release_local_llm_job_service(jobs)
        conn.execute(
            """INSERT OR IGNORE INTO experiment_reflections
               (experiment_id, created_at, model, suggestions_json, error)
               VALUES (?, ?, ?, ?, ?)""",
            (experiment_id, datetime.now(timezone.utc).isoformat(), model,
             json.dumps(suggestions, ensure_ascii=False, separators=(",", ":")), error),
        )
        conn.commit()
        stored = conn.execute(
            "SELECT suggestions_json FROM experiment_reflections WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        return json.loads(stored[0])
    finally:
        conn.close()
