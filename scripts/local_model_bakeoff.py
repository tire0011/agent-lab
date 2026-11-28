#!/usr/bin/env python3
"""Compare local GGUF models on speed and ten fixed hunk replays (#690).

The script never keeps the winning model active. It restores the model that
was active at startup even when a candidate fails, an exception escapes, or
the process is interrupted.

Two hosts can produce bake-off rows, kept apart by the ``host`` column
(#857): ``vm`` (the default path below) and ``home-gpu``. ``--via home-gpu``
submits one ``bakeoff_prompt`` job per fixture case through
:func:`modules.home_gpu.submit_job` and returns at once without writing any
rows; ``--collect`` later reads the answered ``home_gpu_jobs`` rows, scores
each answer with the same checker the VM replay uses
(:func:`modules.local_shadow_replay_hunks.check_unit_text`) and writes the
normal bake-off rows with ``host='home-gpu'``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
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

from modules import home_gpu  # noqa: E402
from modules import local_shadow_replay as lsr  # noqa: E402
from modules import local_shadow_replay_hunks as lsh  # noqa: E402
from modules.home_gpu.collect import _ensure_schema, collect_replies  # noqa: E402
from modules.home_gpu.jobs import DEFAULT_DEADLINE_S as HOME_GPU_DEADLINE_S  # noqa: E402
from modules.local_model_bakeoff import (  # noqa: E402
    choose_winner,
    insert_bakeoff_row,
    passing_hunks_per_hour,
)
from modules.local_shadow_replay_hunks import replay_hunks  # noqa: E402
from modules.local_model_rates import LocalModelRates  # noqa: E402
from modules.mailbox import core as mailbox_core  # noqa: E402
from modules.run_store import DEFAULT_DB_PATH, init_db  # noqa: E402

from scripts import bench_local_speed  # noqa: E402

MODEL_COMMAND = ("sudo", "-n", "/usr/local/sbin/nexus-llama-model")
DEFAULT_FIXTURE = ROOT / "benchmarks" / "bakeoff_hunks.json"

#: Bake-off rows are tagged with the host that produced them (#857) so VM and
#: home-PC numbers are never mixed; rows written before the column existed
#: read as ``vm``.
HOST_VM = "vm"
HOST_HOME_GPU = "home-gpu"
#: The ``home_gpu_jobs`` kind one bake-off case is submitted as.
BAKEOFF_KIND = "bakeoff_prompt"


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
    # ``timeout`` becomes the hunk unit timeout in _replay_environment below.
    call_timeout = min(lsr.offline_local_call_timeout_seconds(), timeout)
    replay_client = client or SeededReplayClient(model=model, seed=seed, timeout=call_timeout)
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


def _load_fixture(fixture_path: Path, db_path: str) -> list[dict[str, Any]]:
    """The bake-off cases: exactly 10 fixed ``experiment_id`` fixture entries."""
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if (not isinstance(fixture, list) or len(fixture) != 10
            or any(not isinstance(item, dict) or not isinstance(item.get("experiment_id"), str)
                   or not item["experiment_id"].strip() for item in fixture)):
        raise ValueError("bakeoff fixture must contain exactly 10 fixed experiment_id entries")
    return fixture


def bakeoff_case_payload(item: dict[str, Any], index: int, db_path: str) -> dict[str, Any]:
    """The deterministic ``bakeoff_prompt`` payload for fixture case *index*.

    The payload identifies the case; the home PC resolves the experiment
    itself. Because :func:`modules.home_gpu.jobs.payload_body` serializes
    deterministically, the same fixture always yields the same
    ``input_sha256``, which is how ``--collect`` matches an answered
    ``home_gpu_jobs`` row back to its case.
    """
    return {"experiment_id": fixture_experiment_id(item, db_path), "index": index}


def submit_home_gpu_jobs(
    fixture: list[dict[str, Any]],
    *,
    db_path: str,
    mailbox_dir: Path | str,
    deadline_s: int = HOME_GPU_DEADLINE_S,
    commit: bool = True,
    run_id: str | None = None,
    now: datetime | None = None,
    submit_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Submit one ``bakeoff_prompt`` job per fixture case via the home GPU (#857).

    Every case goes through :func:`modules.home_gpu.submit_job` (the canonical
    entry point, which tracks the job in ``home_gpu_jobs`` and falls back to
    the VM path when the PC counts as off). The function returns at once with
    the job ids and writes **no** bake-off rows - those are written by
    :func:`collect_home_gpu_bakeoffs` once the PC answered. Raises
    ``HomeGpuDisabled`` when ``NEXUS_HOME_GPU`` is not ``1``; nothing is
    written in that case.
    """
    conn = init_db(db_path)
    try:
        submit = submit_fn or home_gpu.submit_job
        run_id = run_id or uuid.uuid4().hex
        jobs: list[dict[str, Any]] = []
        for index, item in enumerate(fixture):
            payload = bakeoff_case_payload(item, index, db_path)
            result = submit(conn, BAKEOFF_KIND, payload, deadline_s,
                            mailbox_dir=mailbox_dir, commit=commit, now=now)
            jobs.append({"index": index, "experiment_id": payload["experiment_id"],
                         "job_id": result})
        return {
            "run_id": run_id,
            "via": HOST_HOME_GPU,
            "jobs": jobs,
            "submitted": sum(1 for job in jobs if job["job_id"] != home_gpu.FALLBACK),
            "fallback": sum(1 for job in jobs if job["job_id"] == home_gpu.FALLBACK),
        }
    finally:
        conn.close()


def _case_from_mailbox(mailbox_dir: Path | str | None, job_id: str) -> dict[str, Any] | None:
    """The bake-off case of a job, read back from its ``inbox/<job_id>.md`` payload.

    Used when the fixture no longer reproduces the job's ``input_sha256``;
    ``None`` when the mailbox file is gone or not a bakeoff_prompt payload.
    """
    if mailbox_dir is None:
        return None
    path = Path(mailbox_dir) / "inbox" / f"{job_id}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        meta, body = mailbox_core.parse_front_matter(text)
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if meta.get("kind") != BAKEOFF_KIND or not isinstance(payload, dict):
        return None
    experiment_id = payload.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        return None
    return {"index": payload.get("index"), "experiment_id": experiment_id.strip()}


def score_home_gpu_answer(
    case: dict[str, Any],
    answer: str,
    *,
    repo_root: str,
    db_path: str,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one home-GPU answer with the same checker the VM path uses (#857).

    Resolves the case's experiment to its first hunk unit - the unit the VM
    bake-off also replays per case (``max_units=1`` on a fresh variant) - and
    runs the answer text through
    :func:`modules.local_shadow_replay_hunks.check_unit_text`, the exact
    function the VM replay applies to its worker's file, so both hosts are
    scored identically.
    """
    experiment_id = str(case["experiment_id"])
    conn = lsr._connect(db_path)
    try:
        row, adoption = lsr._load(conn, experiment_id)
        goal = str(row["goal"] or "")
        base = str(row["base_commit_sha"]).strip()
        integrated = adoption["integrated_commit_sha"]
        experiment_sha = adoption["experiment_commit_sha"]
    finally:
        conn.close()
    old, new = lsr.merged_range(repo_root, integrated, experiment_sha, base)
    units = lsh.hunk_units(repo_root, old, new)
    if not units:
        raise ValueError("the merged diff changes no non-test Python code")
    unit = units[0]
    test_changes = lsr.merged_test_changes(repo_root, old, new)
    tests = lsh.unit_test_files(repo_root, new, test_changes, unit.file)
    module = lsh.module_name(unit.file)
    worktree = lsr._create_worktree(repo_root, new, db_path=db_path)
    try:
        outcome = lsh.check_unit_text(
            worktree, unit, answer, tests=tests,
            test_runner=lambda wt, t, timeout: lsh.run_unit_tests(wt, t, module, timeout),
            baseline={}, notes=[],
        )
    finally:
        lsr.remove_worktree(repo_root, worktree)
    return {
        "status": outcome.status,
        "applied": bool(outcome.applied),
        "compiled": bool(outcome.compiled),
        "tests_passed": bool(outcome.tests_passed),
        "has_tests": bool(outcome.has_tests),
        "tests_total": outcome.tests_total,
        "tests_ok": outcome.tests_ok,
        "overlap": outcome.overlap,
        "error": outcome.error,
        "file": unit.file,
        "symbol": unit.symbol,
        "title": lsh.issue_title(goal),
    }


def collect_home_gpu_bakeoffs(
    *,
    db_path: str,
    repo_root: str = str(ROOT),
    fixture_path: Path = DEFAULT_FIXTURE,
    mailbox_dir: Path | str | None = None,
    run_id: str | None = None,
    collect_fn: Callable[..., int] | None = None,
    score_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score answered home-GPU bake-off jobs into bake-off rows (``host='home-gpu'``).

    When *mailbox_dir* is given, pending outbox replies are collected first
    (:func:`modules.home_gpu.collect.collect_replies`, the same call the
    mailbox poll makes), so the answered ``home_gpu_jobs`` rows and their
    answer files under ``<repo_root>/state/home_gpu/`` are up to date. Each
    answered ``bakeoff_prompt`` job is matched back to its fixture case (by
    ``input_sha256``, falling back to the job's mailbox payload), its answer
    is scored with the VM checker and one normal bake-off row per model is
    written with ``host='home-gpu'``. Jobs that are still queued or expired
    are skipped - never scored as failures - and so are jobs an earlier
    ``--collect`` already recorded.
    """
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        if mailbox_dir is not None:
            collect = collect_fn or collect_replies
            collect(mailbox_dir, conn, Path(repo_root) / "state")
            conn.commit()
        fixture = _load_fixture(fixture_path, db_path)
        case_by_digest: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(fixture):
            payload = bakeoff_case_payload(item, index, db_path)
            digest = hashlib.sha256(
                home_gpu.payload_body(payload).encode("utf-8")).hexdigest()
            case_by_digest[digest] = {"index": index, **payload}

        already_collected: set[str] = set()
        for stored in conn.execute(
            "SELECT details_json FROM local_model_bakeoffs WHERE host = ?", (HOST_HOME_GPU,)
        ):
            try:
                details = json.loads(stored[0]) if stored[0] else None
            except (TypeError, ValueError):
                details = None
            if isinstance(details, dict) and isinstance(details.get("jobs"), list):
                for job in details["jobs"]:
                    if isinstance(job, dict) and job.get("job_id"):
                        already_collected.add(str(job["job_id"]))

        score = score_fn or (lambda case, answer, job=None:
                             score_home_gpu_answer(case, answer, repo_root=repo_root,
                                                   db_path=db_path, job=job))
        scored: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in conn.execute(
            """
            SELECT * FROM home_gpu_jobs
            WHERE kind = ? AND status = 'answered' AND answer_path IS NOT NULL
            ORDER BY submitted_at, job_id
            """,
            (BAKEOFF_KIND,),
        ):
            job = dict(row)
            job_id = str(job["job_id"])
            case = case_by_digest.get(str(job["input_sha256"]))
            if case is None:
                case = _case_from_mailbox(mailbox_dir, job_id)
            if case is None:
                skipped.append({"job_id": job_id, "reason": "no matching bake-off case"})
                continue
            if job_id in already_collected:
                skipped.append({"job_id": job_id, "reason": "already collected"})
                continue
            try:
                answer = Path(str(job["answer_path"])).read_text(encoding="utf-8")
            except OSError as exc:
                skipped.append({"job_id": job_id, "reason": f"answer file unreadable: {exc}"})
                continue
            try:
                outcome = score(case, answer, job=job)
            except Exception as exc:  # one bad case must not stop the others
                outcome = {"status": "error", "applied": False, "compiled": False,
                           "tests_passed": False, "error": f"{type(exc).__name__}: {exc}"}
            scored.append({"job_id": job_id, "case": case, "job": job, "outcome": outcome})

        run_id = run_id or uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        groups: dict[str, list[dict[str, Any]]] = {}
        for entry in scored:
            model = str(entry["job"].get("model") or HOST_HOME_GPU)
            groups.setdefault(model, []).append(entry)
        written: list[dict[str, Any]] = []
        for model, entries in sorted(groups.items()):
            seconds = sum(float(e["job"].get("seconds") or 0) for e in entries)
            prompt_tokens = sum(int(e["job"].get("prompt_tokens") or 0) for e in entries)
            completion_tokens = sum(int(e["job"].get("completion_tokens") or 0) for e in entries)
            row: dict[str, Any] = {
                "run_id": run_id,
                "created_at": created_at,
                "model": model,
                "is_current": 0,
                "status": "completed",
                "prompt_tokens_per_second": round(prompt_tokens / seconds, 3) if seconds > 0 else None,
                "output_tokens_per_second": round(completion_tokens / seconds, 3) if seconds > 0 else None,
                "hunks_total": len(entries),
                "hunks_applied": sum(bool(e["outcome"].get("applied")) for e in entries),
                "hunks_compiled": sum(bool(e["outcome"].get("compiled")) for e in entries),
                "passing_hunks": sum(bool(e["outcome"].get("tests_passed")) for e in entries),
                "local_too_slow": 0,
                "seconds": round(seconds, 3),
                "winner": 0,
                "recommend_switch": 0,
                "details_json": json.dumps({
                    "host": HOST_HOME_GPU,
                    "jobs": [
                        {
                            "job_id": e["job_id"],
                            "experiment_id": e["case"]["experiment_id"],
                            "index": e["case"]["index"],
                            "answer_sha256": e["job"].get("answer_sha256"),
                            "prompt_tokens": e["job"].get("prompt_tokens"),
                            "completion_tokens": e["job"].get("completion_tokens"),
                            "seconds": e["job"].get("seconds"),
                            "outcome": e["outcome"],
                        }
                        for e in entries
                    ],
                }),
                "error": "; ".join(sorted({str(e["outcome"]["error"]) for e in entries
                                            if e["outcome"].get("error")})) or None,
                "host": HOST_HOME_GPU,
            }
            row["passing_hunks_per_hour"] = round(passing_hunks_per_hour(row), 3)
            insert_bakeoff_row(conn, row)
            written.append(row)
        return {"run_id": run_id, "via": HOST_HOME_GPU, "scored": len(scored),
                "written": len(written), "skipped": skipped, "rows": written}
    finally:
        conn.close()


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
    fixture = _load_fixture(fixture_path, db_path)
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
    parser.add_argument("candidates", nargs="*",
                        help="downloaded GGUF filenames, in scoring order (VM path only)")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=600.0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--via", choices=("vm", HOST_HOME_GPU), default=HOST_VM,
                      help="where to run the bake-off: 'vm' (default) switches models locally; "
                           "'home-gpu' submits one bakeoff_prompt job per case through "
                           "modules.home_gpu.submit_job and returns at once (no rows yet)")
    mode.add_argument("--collect", action="store_true",
                      help="score the answered home-GPU bakeoff_prompt jobs and write bake-off "
                           "rows with host='home-gpu'")
    parser.add_argument("--mailbox-dir",
                        default=os.environ.get("AGENT_MAILBOX_REPO_PATH"),
                        help="mailbox working copy (default AGENT_MAILBOX_REPO_PATH); "
                             "required for --via home-gpu")
    parser.add_argument("--deadline", type=int, default=HOME_GPU_DEADLINE_S,
                        help="home-GPU job deadline in seconds (--via home-gpu)")
    args = parser.parse_args(argv)

    if args.collect:
        if args.candidates:
            parser.error("candidates are not used with --collect")
        report = collect_home_gpu_bakeoffs(
            db_path=args.db_path, repo_root=args.repo_root, fixture_path=args.fixture,
            mailbox_dir=args.mailbox_dir,
        )
        print(json.dumps(report, indent=2))
        return 0

    if args.via == HOST_HOME_GPU:
        if args.candidates:
            parser.error("candidates are not used with --via home-gpu; "
                         "the home PC runs its own model")
        if not home_gpu.home_gpu_enabled():
            print(f"error: --via home-gpu requires {home_gpu.jobs.ENV_FLAG}=1 "
                  "(home-GPU jobs are off)", file=sys.stderr)
            return 2
        if not args.mailbox_dir:
            print("error: --via home-gpu needs the mailbox working copy "
                  "(--mailbox-dir or AGENT_MAILBOX_REPO_PATH)", file=sys.stderr)
            return 2
        try:
            fixture = _load_fixture(args.fixture, args.db_path)
            report = submit_home_gpu_jobs(
                fixture, db_path=args.db_path, mailbox_dir=args.mailbox_dir,
                deadline_s=args.deadline, commit=True,
            )
        except home_gpu.HomeGpuDisabled as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2))
        return 0

    if not args.candidates:
        parser.error("candidates are required unless --via home-gpu or --collect is given")
    report = run_bakeoff(args.candidates, repo_root=args.repo_root, db_path=args.db_path,
                         fixture_path=args.fixture, url=args.url, seed=args.seed,
                         timeout=args.timeout)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
