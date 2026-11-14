#!/usr/bin/env python3
"""Compare local GGUF models on speed and ten fixed hunk replays (#690).

The script never keeps the winning model active. It restores the model that
was active at startup even when a candidate fails, an exception escapes, or
the process is interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
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
from modules import local_shadow_replay as lsr  # noqa: E402
from modules import local_shadow_replay_hunks as lsh  # noqa: E402
from modules.local_model_rates import LocalModelRates  # noqa: E402
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


def fixture_experiment_id(item: dict[str, Any], _db_path: str) -> str:
    """Return the immutable experiment identity stored in the fixture.

    In particular, never resolve an issue number to its newest retry: that made
    previous bake-offs silently compare different work over time.
    """
    value = item.get("experiment_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("fixture entry needs a non-empty fixed experiment_id")
    return value.strip()


class SeededReplayClient(lsr._UsageCountingClient):
    """Production replay client pinned to the candidate and bake-off seed."""

    def __init__(self, *, model: str, seed: int, timeout: float) -> None:
        super().__init__(model=model, timeout=timeout)
        self._bakeoff_seed = seed

    async def chat_completion(self, messages, temperature=0.0, max_tokens=1024, **kwargs):
        kwargs["seed"] = self._bakeoff_seed
        return await super().chat_completion(
            messages, temperature=temperature, max_tokens=max_tokens, **kwargs)


def _record_benchmark_rates(model: str, speed: dict[str, Any], rates_path: str | None) -> None:
    """Make hunk admission use this candidate's measured rates immediately."""
    rates = (speed.get("summary") or {}).get("all") or {}
    prompt = rates.get("prompt_tokens_per_second")
    output = rates.get("output_tokens_per_second")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
               for v in (prompt, output)):
        raise RuntimeError("speed benchmark did not produce positive prompt/output rates")
    # LocalModelRates consumes llama-server timing fields. One second of
    # synthetic timing exactly preserves each aggregate measured rate.
    store = LocalModelRates(rates_path, model=model)
    stored = store.record({
        "prompt_n": float(prompt), "prompt_ms": 1000.0,
        "predicted_n": float(output), "predicted_ms": 1000.0,
    })
    measured = store.medians()
    if not stored or any(value is None for value in measured):
        raise RuntimeError("could not store candidate-specific benchmark rates")


@contextlib.contextmanager
def _replay_environment(timeout: float, rates_path: str | None):
    changes = {lsh.ENV_UNIT_TIMEOUT: str(timeout)}
    if rates_path:
        changes["LOCAL_MODEL_RATES_PATH"] = rates_path
    old = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _replay_when_available(replay_fn: Callable[..., dict[str, Any] | None],
                           experiment_id: str, *, lease_wait_seconds: float,
                           sleep_fn: Callable[[float], None] = time.sleep,
                           monotonic_fn: Callable[[], float] = time.monotonic,
                           **kwargs: Any) -> dict[str, Any]:
    deadline = monotonic_fn() + max(0.0, lease_wait_seconds)
    while True:
        result = replay_fn(experiment_id, **kwargs)
        if result is not None:
            return result
        if monotonic_fn() >= deadline:
            raise TimeoutError("shadow replay global lease remained busy")
        sleep_fn(min(1.0, max(0.0, deadline - monotonic_fn())))


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
    rates_path: str | None = None,
    lease_wait_seconds: float = 60.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    speed = bench_fn(url, seed=seed, timeout=timeout)
    _record_benchmark_rates(model, speed, rates_path)
    replay_client = client or SeededReplayClient(model=model, seed=seed, timeout=timeout)
    # Test doubles and injected transports are supported, but admission must
    # still key rates by the candidate that was actually switched in.
    replay_client.model = model
    all_speed = (speed.get("summary") or {}).get("all") or {}
    outcomes: list[dict[str, Any]] = []
    started = time.monotonic()
    for index, item in enumerate(fixture):
        variant = f"bakeoff-{run_id[:8]}-{_slug(model)}-{index:02d}"
        try:
            experiment_id = fixture_experiment_id(item, db_path)
            with _replay_environment(timeout, rates_path):
                summary = _replay_when_available(
                    replay_fn, experiment_id, lease_wait_seconds=lease_wait_seconds,
                    sleep_fn=sleep_fn, monotonic_fn=monotonic_fn,
                    repo_root=repo_root, db_path=db_path, variant=variant,
                    client=replay_client, max_units=1,
                )
        except TimeoutError as exc:
            # Contention is a run condition, never evidence that the model
            # failed a hunk. Preserve it distinctly and stop this candidate.
            outcomes.append({"status": "lease_busy", "error": str(exc)})
            break
        except (ValueError, sqlite3.Error) as exc:
            outcomes.append({"status": "fixture_error", "error": f"{type(exc).__name__}: {exc}"})
            continue
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
        "status": "lease_busy" if any(u.get("status") == "lease_busy" for u in outcomes)
                  else "completed",
        "prompt_tokens_per_second": all_speed.get("prompt_tokens_per_second"),
        "output_tokens_per_second": all_speed.get("output_tokens_per_second"),
        "hunks_total": len(outcomes),
        "hunks_applied": sum(bool(u.get("applied")) for u in outcomes),
        "hunks_compiled": sum(bool(u.get("compiled")) for u in outcomes),
        "passing_hunks": sum(bool(u.get("tests_passed")) for u in outcomes),
        "local_too_slow": sum(u.get("status") == "local_too_slow" for u in outcomes),
        "seconds": round(seconds, 3),
        "details": {"speed": speed, "hunks": outcomes, "seed": seed,
                    "timeout_seconds": timeout, "model": model},
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
    rates_path: str | None = None,
    lease_wait_seconds: float = 60.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    if not candidates:
        raise ValueError("at least one candidate is required")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if (not isinstance(fixture, list) or len(fixture) != 10
            or any(not isinstance(item, dict) or not isinstance(item.get("experiment_id"), str)
                   or not item["experiment_id"].strip() for item in fixture)):
        raise ValueError("bakeoff fixture must contain exactly 10 fixed experiment_id entries")
    original = current_model(run_command)
    run_id = uuid.uuid4().hex
    ephemeral_rates = rates_path is None
    rates_path = rates_path or str(Path(db_path).parent / f".bakeoff-rates-{run_id}.json")
    created_at = datetime.now(timezone.utc).isoformat()
    conn = init_db(db_path)
    rows: list[dict[str, Any]] = []
    try:
        for candidate_index, model in enumerate(candidates):
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
            try:
                scored = score_candidate(
                    model, fixture, repo_root=repo_root, db_path=db_path, url=url,
                    seed=seed, timeout=timeout, run_id=run_id, bench_fn=bench_fn,
                    replay_fn=replay_fn, client=client, rates_path=rates_path,
                    lease_wait_seconds=lease_wait_seconds, sleep_fn=sleep_fn,
                    monotonic_fn=monotonic_fn,
                )
            except BaseException as exc:
                # A durable partial row makes interruption/crash observable;
                # BaseException is immediately re-raised after persistence.
                row = {**base, "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                       "hunks_total": 0, "hunks_applied": 0, "hunks_compiled": 0,
                       "passing_hunks": 0, "local_too_slow": 0, "seconds": 0,
                       "passing_hunks_per_hour": 0, "details_json": json.dumps({
                           "seed": seed, "timeout_seconds": timeout, "model": model}),
                       "error": f"{type(exc).__name__}: {exc}"[:1000]}
                insert_bakeoff_row(conn, row)
                rows.append(row)
                for pending_model in candidates[candidate_index + 1:]:
                    pending = {
                        "run_id": run_id, "created_at": created_at, "model": pending_model,
                        "is_current": int(pending_model == original), "status": "not_run",
                        "hunks_total": 0, "hunks_applied": 0, "hunks_compiled": 0,
                        "passing_hunks": 0, "local_too_slow": 0, "seconds": 0,
                        "passing_hunks_per_hour": 0, "winner": 0, "recommend_switch": 0,
                        "details_json": json.dumps({"seed": seed,
                                                    "timeout_seconds": timeout,
                                                    "model": pending_model}),
                        "error": "an earlier candidate interrupted the run",
                    }
                    insert_bakeoff_row(conn, pending)
                    rows.append(pending)
                raise
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
            try:
                conn.close()
            finally:
                if ephemeral_rates:
                    try:
                        Path(rates_path).unlink()
                    except FileNotFoundError:
                        pass


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
