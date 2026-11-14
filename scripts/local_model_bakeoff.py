#!/usr/bin/env python3
"""Compare local GGUF models on speed and ten fixed hunk replays (#690).

The script never keeps the winning model active. It restores the model that
was active at startup even when a candidate fails, an exception escapes, or
the process is interrupted.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.local_model_bakeoff import (  # noqa: E402
    choose_winner,
    insert_bakeoff_row,
    passing_hunks_per_hour,
)
from modules.local_shadow_replay_hunks import replay_hunks  # noqa: E402
from modules.run_store import DEFAULT_DB_PATH, init_db  # noqa: E402

from scripts import bench_local_speed  # noqa: E402

MODEL_COMMAND = ("sudo", "-n", "/usr/local/sbin/nexus-llama-model")
DEFAULT_FIXTURE = ROOT / "benchmarks" / "bakeoff_hunks.json"


def command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=300)


def current_model(run_command: Callable[[list[str]], Any] = command) -> str:
    result = run_command([*MODEL_COMMAND, "list"])
    if int(getattr(result, "returncode", 1)) != 0:
        raise RuntimeError(getattr(result, "stderr", "model list failed").strip())
    match = re.search(r"^\s*MODEL=([^\s]+)\s*$", str(result.stdout), re.MULTILINE)
    if not match:
        raise RuntimeError("nexus-llama-model list did not report MODEL")
    return match.group(1)


def switch_model(model: str, run_command: Callable[[list[str]], Any] = command) -> None:
    result = run_command([*MODEL_COMMAND, "switch", f"MODEL={model}"])
    if int(getattr(result, "returncode", 1)) != 0:
        message = str(getattr(result, "stderr", "") or getattr(result, "stdout", "")).strip()
        raise RuntimeError(message or f"model switch exited {result.returncode}")


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")[:60]


def _units(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((summary or {}).get("units") or [])


def fixture_experiment_id(item: dict[str, Any], db_path: str) -> str:
    if item.get("experiment_id"):
        return str(item["experiment_id"])
    issue = item.get("issue_number")
    if not isinstance(issue, int):
        raise ValueError("fixture entry needs experiment_id or integer issue_number")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM experiments WHERE (source_type='issue' AND source_id=?) "
            "OR goal LIKE ? ORDER BY created_at DESC LIMIT 1",
            (str(issue), f"%GitHub Issue #{issue}%"),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"no experiment found for fixture issue #{issue}")
    return str(row[0])


def score_candidate(
    model: str,
    fixture: list[dict[str, Any]],
    *,
    repo_root: str,
    db_path: str,
    url: str,
    seed: int,
    timeout: float,
    run_id: str,
    bench_fn: Callable[..., dict[str, Any]] = bench_local_speed.bench,
    replay_fn: Callable[..., dict[str, Any] | None] = replay_hunks,
    client: Any | None = None,
) -> dict[str, Any]:
    speed = bench_fn(url, seed=seed, timeout=timeout)
    all_speed = (speed.get("summary") or {}).get("all") or {}
    outcomes: list[dict[str, Any]] = []
    started = time.monotonic()
    for index, item in enumerate(fixture):
        experiment_id = fixture_experiment_id(item, db_path)
        variant = f"bakeoff-{run_id[:8]}-{_slug(model)}-{index:02d}"
        summary = replay_fn(
            experiment_id, repo_root=repo_root, db_path=db_path, variant=variant,
            client=client, max_units=1,
        )
        units = _units(summary)
        if units:
            # A fresh variant and max_units=1 yield exactly one scored unit.
            outcomes.append(units[-1])
        else:
            outcomes.append({"status": (summary or {}).get("status", "error")})
    seconds = sum(float(u.get("seconds") or 0) for u in outcomes)
    if seconds <= 0:
        seconds = time.monotonic() - started
    row: dict[str, Any] = {
        "model": model,
        "status": "completed",
        "prompt_tokens_per_second": all_speed.get("prompt_tokens_per_second"),
        "output_tokens_per_second": all_speed.get("output_tokens_per_second"),
        "hunks_total": len(outcomes),
        "hunks_applied": sum(bool(u.get("applied")) for u in outcomes),
        "hunks_compiled": sum(bool(u.get("compiled")) for u in outcomes),
        "passing_hunks": sum(bool(u.get("tests_passed")) for u in outcomes),
        "local_too_slow": sum(u.get("status") == "local_too_slow" for u in outcomes),
        "seconds": round(seconds, 3),
        "details": {"speed": speed, "hunks": outcomes},
    }
    row["passing_hunks_per_hour"] = round(passing_hunks_per_hour(row), 3)
    return row


def run_bakeoff(
    candidates: list[str],
    *,
    repo_root: str = str(ROOT),
    db_path: str = DEFAULT_DB_PATH,
    fixture_path: Path = DEFAULT_FIXTURE,
    url: str = "http://127.0.0.1:8080",
    seed: int = 42,
    timeout: float = 600.0,
    run_command: Callable[[list[str]], Any] = command,
    bench_fn: Callable[..., dict[str, Any]] = bench_local_speed.bench,
    replay_fn: Callable[..., dict[str, Any] | None] = replay_hunks,
    client: Any | None = None,
) -> dict[str, Any]:
    if not candidates:
        raise ValueError("at least one candidate is required")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if len(fixture) != 10 or any(not ({"experiment_id", "issue_number"} & set(item)) for item in fixture):
        raise ValueError("bakeoff fixture must contain exactly 10 experiment or issue entries")
    original = current_model(run_command)
    run_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    conn = init_db(db_path)
    rows: list[dict[str, Any]] = []
    try:
        for model in candidates:
            base = {"run_id": run_id, "created_at": created_at, "model": model,
                    "is_current": int(model == original), "winner": 0,
                    "recommend_switch": 0}
            try:
                switch_model(model, run_command)
            except Exception as exc:  # one unavailable candidate does not invalidate the run
                row = {**base, "status": "switch_failed", "hunks_total": 0,
                       "hunks_applied": 0, "hunks_compiled": 0, "passing_hunks": 0,
                       "local_too_slow": 0, "seconds": 0, "passing_hunks_per_hour": 0,
                       "details_json": None, "error": f"{type(exc).__name__}: {exc}"[:1000]}
                insert_bakeoff_row(conn, row)
                rows.append(row)
                continue
            scored = score_candidate(
                model, fixture, repo_root=repo_root, db_path=db_path, url=url,
                seed=seed, timeout=timeout, run_id=run_id, bench_fn=bench_fn,
                replay_fn=replay_fn, client=client,
            )
            details = scored.pop("details")
            row = {**base, **scored, "details_json": json.dumps(details), "error": None}
            insert_bakeoff_row(conn, row)
            rows.append(row)
        selection = choose_winner(rows, original)
        winner = selection["winner"]
        if winner is not None:
            winner["winner"] = 1
            winner["recommend_switch"] = int(selection["recommend_switch"])
            conn.execute(
                "UPDATE local_model_bakeoffs SET winner=1, recommend_switch=? WHERE run_id=? AND model=?",
                (int(selection["recommend_switch"]), run_id, winner["model"]),
            )
            conn.commit()
        return {"run_id": run_id, "original_model": original, "rows": rows,
                "winner": winner["model"] if winner else None,
                "recommend_switch": bool(selection["recommend_switch"])}
    finally:
        try:
            switch_model(original, run_command)
        finally:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("candidates", nargs="+", help="downloaded GGUF filenames, in scoring order")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    report = run_bakeoff(args.candidates, repo_root=args.repo_root, db_path=args.db_path,
                         fixture_path=args.fixture, url=args.url, seed=args.seed,
                         timeout=args.timeout)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
